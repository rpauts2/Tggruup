"""W26-TG: a Telegram publication records the message id the platform returned.

The measurement loop has one join key. `tg_sync.collect_post_stats` builds its
request list from `(chat id, posts.tg_message_id)` (tg_sync.py:464-480) and writes
a `post_stats` row only for a pair Telegram answers about; `s8_analytics.attribution`
therefore calls every published post without a `tg_message_id` `unknowable`. Measured
read-only on the live base on 2026-11-06: `status='published'` 528 rows, of which
`tg_message_id IS NOT NULL` — **0**, and `post_stats` 22 rows since 2026-10-29. The
528 are 456 VK + 57 Dzen (whose identifiers live on the crosspost journal, not on the
post) + 15 Telegram rows from 2026-10-26 that each carry `tg_chat_id = -1` and no
message id: the cohort `maintenance.publication_evidence_repair` was written for.

So the fix has to be provable, not merely plausible, and it has four edges. This file
pins all four offline with a faked transport:

1. a send that returns an id stores that id, in the same transaction that marks the
   row `published`, and the collector's own predicate then selects the row;
2. a send that returns no id — or a placeholder that is not an id (`0`, `-1`, `""`,
   `"dry-7"`, `True`) — is labelled `publish_unknown` with a stated reason, and is
   never reachable by "measured";
3. a second publication cannot blank an id the row already carried;
4. a dry run stores nothing at all, because a dry run counted as a publication is how
   this project was burned before (the 15 rows above).

Offline by construction: the sender is a recorder, the bot token is a plainly-labelled
sentinel, the only collector call runs against a faked `fetch_post_stats`, and no cell
reads `.env` or the live database.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from tgfarm import tg_sync, tguser
from tgfarm.db.models import Channel, CrosspostLog, Post, PostStat
from tgfarm.db.session import session_scope
from tgfarm.s7_publish import (
    STATUS_PUBLISH_UNKNOWN,
    STATUS_PUBLISHED,
    STATUS_PUBLISHING,
    STATUS_SCHEDULED,
    _recordable_message_id,
)

#: A supergroup really is negative, so the sign proves nothing; this one is simply
#: the shape `real_tg_chat_id` accepts and the collector pairs with.
REAL_TG_CHAT = -1002003004005
#: Names "a token is configured" without being one.
TOKEN_SET = "w26tg-bot-token-configured"
MEASURABLE_ID = 4242
ANCIENT = datetime(2000, 1, 1, tzinfo=UTC)

_seeded_channels: list[str] = []
_seeded_public_ids: list[int] = []
_seeded_post_ids: list[int] = []


@pytest.fixture(autouse=True)
def _leave_no_residue():
    """The suite shares one session database; this file's rows must not outlive it.

    A leftover `published` row here lands in another file's evidence denominator,
    and a leftover `post_stats` row lands in a coverage one.
    """
    yield
    with session_scope() as session:
        if _seeded_post_ids:
            session.execute(
                delete(PostStat).where(PostStat.post_id.in_(_seeded_post_ids))
            )
            session.execute(delete(Post).where(Post.id.in_(_seeded_post_ids)))
        if _seeded_public_ids:
            session.execute(
                delete(CrosspostLog).where(
                    CrosspostLog.post_public_id.in_(_seeded_public_ids)
                )
            )
        if _seeded_channels:
            session.execute(delete(Channel).where(Channel.id.in_(_seeded_channels)))
    _seeded_channels.clear()
    _seeded_public_ids.clear()
    _seeded_post_ids.clear()


def _slug() -> str:
    return uuid4().hex[:8]


def _seed_channel(platform: str = "telegram") -> str:
    channel_id = f"tg_{platform}_{_slug()}"
    with session_scope() as session:
        session.merge(
            Channel(
                id=channel_id,
                platform=platform,
                tg_chat_id=REAL_TG_CHAT,
                title="w26 message id",
                niche="general",
                enabled=True,
            )
        )
    _seeded_channels.append(channel_id)
    return channel_id


def _seed_post(
    state: str,
    *,
    platform: str = "telegram",
    message_id: int | None = None,
    journal_posted_platform: str | None = None,
) -> tuple[int, str]:
    channel_id = _seed_channel(platform)
    public_id = f"w26tg{_slug()}"
    with session_scope() as session:
        row = Post(
            public_id=public_id,
            channel_id=channel_id,
            hook="Хук, который ушёл в Telegram.",
            body="Тело поста для проверки идентификатора сообщения.",
            status=state,
            scheduled_at=ANCIENT if state in (STATUS_SCHEDULED, STATUS_PUBLISHING) else None,
            published_at=ANCIENT if state == STATUS_PUBLISHED else None,
            created_at=ANCIENT,
            tg_message_id=message_id,
            tg_chat_id=REAL_TG_CHAT if message_id is not None else None,
        )
        session.add(row)
        session.flush()
        post_id = int(row.id)
    if journal_posted_platform is not None:
        with session_scope() as session:
            session.add(
                CrosspostLog(
                    post_public_id=public_id,
                    platform=journal_posted_platform,
                    status="posted",
                    remote_id=None,
                    url=None,
                )
            )
        _seeded_public_ids.append(public_id)
    _seeded_post_ids.append(post_id)
    return post_id, public_id


def _read(post_id: int) -> dict:
    with session_scope() as session:
        row = session.get(Post, post_id)
        assert row is not None
        return {
            "status": row.status,
            "published_at": row.published_at,
            "tg_message_id": row.tg_message_id,
            "tg_chat_id": row.tg_chat_id,
            "publish_error": row.publish_error,
            "scheduled_at": row.scheduled_at,
        }


def _world(monkeypatch, cfg, *, live: bool = True) -> None:
    """Pin the two facts `_publish_one` asks about, and stub the persona lookup.

    The cadence comes off the real `cfg` object so the tick's scheduling branch is
    inherited, not invented here — same reason `test_publishing_state_machine` gives.
    """
    from tgfarm import s7_publish

    settings = SimpleNamespace(
        live=live,
        telegram_bot_token=TOKEN_SET if live else "",
        approval_required=False,
        dzen_requires_llm=True,
        primary_publish_max_attempts=5,
        dzen_min_gap_s=0,
        vk_prime_hours="",
        vk_prime_share=0.0,
    )
    monkeypatch.setattr(s7_publish, "get_settings", lambda: settings)
    monkeypatch.setattr("tgfarm.platforms.is_platform_enabled", lambda _platform: True)
    monkeypatch.setattr(
        s7_publish,
        "get_config",
        lambda: SimpleNamespace(
            persona=lambda _channel_id: SimpleNamespace(enabled=True, posts_per_day=6),
            schedule=cfg.schedule,
            personas=cfg.personas,
        ),
    )

    async def _noop_heartbeat(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(s7_publish, "_publish_heartbeat_loop", _noop_heartbeat)


def _sender(monkeypatch, *, returns: object, calls: list) -> None:
    """The transport, faked. Records what it was asked to send, returns `returns`."""
    from tgfarm import s7_publish

    async def _send(chat_id, text, media=None):
        calls.append({"chat_id": chat_id, "text": text, "media": media})
        return returns

    monkeypatch.setattr(s7_publish, "PUBLISHER", SimpleNamespace(send=_send))


def _publish(post_id: int, platform: str = "telegram", target=REAL_TG_CHAT) -> dict:
    from tgfarm import s7_publish

    return asyncio.run(
        s7_publish._publish_one(post_id, target, "Текст для проверки.", None, platform)
    )


# ---------------------------------------------------------------- 1. id recorded
def test_a_successful_send_stores_the_id_the_platform_returned(monkeypatch, cfg):
    """The headline: the Bot API's id reaches `posts.tg_message_id`.

    `TelegramPublisher.send` is the only Telegram sender in the farm, and it used to
    hand back `message.message_id` unvalidated; `_publish_one` then assigned whatever
    came back to the column. Both halves of that are pinned here: a real id is
    persisted, and it is persisted with the `published` status that depends on it.
    """
    post_id, _public = _seed_post(STATUS_SCHEDULED)
    calls: list = []
    _world(monkeypatch, cfg)
    _sender(monkeypatch, returns=MEASURABLE_ID, calls=calls)

    result = _publish(post_id)

    assert result["ok"] is True, result
    assert calls == [{"chat_id": REAL_TG_CHAT, "text": "Текст для проверки.", "media": None}]
    after = _read(post_id)
    assert after["status"] == STATUS_PUBLISHED, after
    assert after["tg_message_id"] == MEASURABLE_ID, after
    assert after["tg_chat_id"] == REAL_TG_CHAT, after
    assert after["published_at"] is not None, after
    assert result["message_id"] == MEASURABLE_ID, result


def test_the_collectors_own_predicate_selects_the_published_row(monkeypatch, cfg):
    """The consumer's key, not a key of our invention.

    `tg_sync.collect_post_stats` (tg_sync.py:462-480) is not in this package's
    ownership, so it is *called* rather than re-implemented: with the account layer
    faked, the report and the pairs it asked for are the collector's own answer to
    "is this publication measurable yet". The id it asks about must be the id the
    send returned, and the snapshot must reach `post_stats`.
    """
    post_id, _public = _seed_post(STATUS_SCHEDULED)
    calls: list = []
    _world(monkeypatch, cfg)
    _sender(monkeypatch, returns=MEASURABLE_ID, calls=calls)
    assert _publish(post_id)["ok"] is True

    asked: list = []

    async def _fake_fetch(pairs):
        asked.extend(pairs)
        # Answer *only* this file's id. The suite shares one database, so a spy that
        # answered every pair would write `post_stats` rows into another file's
        # denominators — the same residue this module keeps getting told about.
        return {
            (chat_id, mid): {"views": 611, "reactions": 12, "forwards": 3}
            for chat_id, mid in pairs
            if mid == MEASURABLE_ID and chat_id == REAL_TG_CHAT
        }

    monkeypatch.setattr(tguser, "fetch_post_stats", _fake_fetch)
    report = asyncio.run(tg_sync.collect_post_stats(limit=1000))

    assert (REAL_TG_CHAT, MEASURABLE_ID) in asked, asked
    assert report["visible"] == 1, report
    with session_scope() as session:
        stats = session.execute(
            select(PostStat).where(PostStat.post_id == post_id)
        ).scalars().all()
    assert len(stats) == 1, stats
    assert stats[0].views == 611, stats[0].views


# ------------------------------------------------- 2. no id is a stated outcome
@pytest.mark.parametrize(
    "returned",
    [None, 0, -1, "", "dry-7", True, {"status": "ok"}, object()],
    ids=[
        "none",
        "zero",
        "negative",
        "empty_string",
        "dry_placeholder",
        "bool",
        "body_without_id",
        "object_without_id",
    ],
)
def test_a_send_with_no_usable_id_is_labelled_not_measured(monkeypatch, cfg, returned):
    """`ok` with nothing to point at must never be a silent `published`.

    The 15 legacy rows are exactly this shape: `status='published'`,
    `tg_message_id IS NULL`, `tg_chat_id = -1`. A placeholder stored *as* an id is
    the same lie as a missing one, because the collector would then ask Telegram
    about a message that never existed and record the answer as evidence.
    """
    post_id, _public = _seed_post(STATUS_SCHEDULED)
    calls: list = []
    _world(monkeypatch, cfg)
    _sender(monkeypatch, returns=returned, calls=calls)

    result = _publish(post_id)

    assert calls, "the transport was meant to be reached, then refused"
    assert result["ok"] is False, result
    after = _read(post_id)
    assert after["status"] == STATUS_PUBLISH_UNKNOWN, after
    assert after["tg_message_id"] is None, after
    assert after["published_at"] is None, after
    assert "no remote identifier" in str(after["publish_error"]), after
    # the outcome is *named*, not merely left blank: the reason says the post is
    # unmeasurable for the stats collector, which is the fact `attribution` reads.
    assert "unmeasurable" in str(after["publish_error"]), after

    asked: list = []

    async def _spy(pairs):
        asked.extend(pairs)
        return {}

    monkeypatch.setattr(tguser, "fetch_post_stats", _spy)
    asyncio.run(tg_sync.collect_post_stats(limit=1000))
    with session_scope() as session:
        selected = session.execute(
            select(Post.id).where(
                Post.status == "published", Post.tg_message_id.isnot(None)
            )
        ).scalars().all()
    assert post_id not in selected, "an unlabelled row must not join as measured"


def test_a_journal_row_alone_is_not_evidence_for_a_telegram_publication(
    monkeypatch, cfg
):
    """Telegram is held to its own id, because only that id can be measured.

    `has_publication_evidence` accepts a journalled `posted` row for Dzen and VK —
    they hand their identifier there and 403 of 403 live VK publications use it. It
    must not accept one for Telegram: `s9_growth.adapters.ADAPTERS` has no `telegram`
    key, so such a row is legacy or hand-written, and it cannot give the collector
    the `(chat, message)` pair it asks about.
    """
    post_id, _public = _seed_post(
        STATUS_SCHEDULED, journal_posted_platform="telegram"
    )
    calls: list = []
    _world(monkeypatch, cfg)
    _sender(monkeypatch, returns=None, calls=calls)

    result = _publish(post_id)

    assert result["ok"] is False, result
    after = _read(post_id)
    assert after["status"] == STATUS_PUBLISH_UNKNOWN, after
    assert after["tg_message_id"] is None, after


# ---------------------------------------------------- 3. no clobbering of an id
def test_a_second_publication_does_not_blank_an_id_the_row_already_carried(
    monkeypatch, cfg
):
    """The clobber this package has to survive: `row.tg_message_id = message_id`.

    Dzen and VK return `{"message_id": None, "remote_id": ...}` — their identifier
    never lives on the post — so an unconditional assignment wrote NULL over a real
    Telegram id and turned a measurable publication back into an unknowable one on
    the second send. The write is now conditional, and the row keeps what the
    platform once gave it.

    The from-state is a *claimable* row that already carries an id, which is a shape
    this farm can actually reach: an ambiguous send keeps the id it stored (§80
    parks the row as `publish_unknown` and clears nothing), an operator's
    `retry_after_check` puts it back in the queue without clearing it either
    (tgfarm/api.py:2198-2201), and `tgfarm/api.py:778` already treats
    `tg_message_id is not None` as "this was sent" whatever the status says. A
    `publishing` row would not do: `_claim_post` only takes `scheduled` ones, so the
    second publish would skip and the assertion would prove nothing.
    """
    from tgfarm import s7_publish

    post_id, _public = _seed_post(
        STATUS_SCHEDULED, platform="vk", message_id=MEASURABLE_ID
    )
    before = _read(post_id)
    assert before["tg_message_id"] == MEASURABLE_ID, before

    async def _vk_publish(_post_id, _text):
        return {"ok": True, "message_id": None, "remote_id": "vk-77"}

    monkeypatch.setattr(s7_publish, "_publish_vk", _vk_publish)
    _world(monkeypatch, cfg)

    result = asyncio.run(
        s7_publish._publish_one(post_id, "vk", "Текст.", None, "vk")
    )

    assert result["ok"] is True, result
    after = _read(post_id)
    assert after["status"] == STATUS_PUBLISHED, after
    assert after["tg_message_id"] == MEASURABLE_ID, (
        "a publication without an id may not erase one the row already had"
    )


def test_a_telegram_republish_of_an_already_published_row_keeps_its_id(monkeypatch, cfg):
    """The dedupe path returns the stored id and writes nothing."""
    post_id, _public = _seed_post(STATUS_PUBLISHED, message_id=MEASURABLE_ID)
    calls: list = []
    _world(monkeypatch, cfg)
    _sender(monkeypatch, returns=999, calls=calls)

    result = _publish(post_id)

    assert result == {"ok": True, "skipped": True, "message_id": MEASURABLE_ID}, result
    assert calls == [], "a row already published must not be sent again"
    after = _read(post_id)
    assert after["tg_message_id"] == MEASURABLE_ID, after
    assert after["status"] == STATUS_PUBLISHED, after


# ------------------------------------------------------------------- 4. dry runs
def test_a_dry_run_stores_nothing_and_sends_nothing(monkeypatch, cfg):
    """A dry run is not a publication, so it may not leave an id behind either.

    The farm-not-live branch returns before the post is claimed, which is the only
    reason the off-mode adapters' `dry-*` placeholders never reached this column.
    Pinned here because a `tg_message_id` written by a run that never sent anything
    would be a *worse* row than the NULL one: it would look measured.
    """
    post_id, _public = _seed_post(STATUS_SCHEDULED)
    calls: list = []
    _world(monkeypatch, cfg, live=False)
    _sender(monkeypatch, returns=MEASURABLE_ID, calls=calls)

    result = _publish(post_id)

    assert result["ok"] is True and result["dry_run"] is True, result
    assert calls == [], "nothing may reach the transport in a dry run"
    after = _read(post_id)
    assert after["status"] == STATUS_SCHEDULED, after
    assert after["tg_message_id"] is None, after
    assert after["published_at"] is None, after
    assert after["tg_chat_id"] is None, after


def test_a_dry_run_does_not_disturb_a_row_that_already_has_an_id(monkeypatch, cfg):
    """Same guarantee, on the row that is already measurable."""
    post_id, _public = _seed_post(STATUS_PUBLISHING, message_id=MEASURABLE_ID)
    calls: list = []
    _world(monkeypatch, cfg, live=False)
    _sender(monkeypatch, returns=None, calls=calls)

    result = _publish(post_id)

    assert result.get("dry_run") is True, result
    assert calls == []
    after = _read(post_id)
    assert after["tg_message_id"] == MEASURABLE_ID, after
    assert after["status"] == STATUS_PUBLISHING, after


# ------------------------------------------------------------- the publisher itself
class _FakeSession:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _FakeBot:
    """The slice of `aiogram.Bot` `TelegramPublisher.send` touches, and nothing else."""

    def __init__(self, message: object) -> None:
        self._message = message
        self.session = _FakeSession()
        self.calls: list[tuple] = []

    async def send_message(self, *, chat_id=None, text=None):
        self.calls.append((chat_id, text))
        return self._message


async def _noop_take() -> None:
    return None


def _publisher_world(monkeypatch, message: object) -> _FakeBot:
    from tgfarm import s7_publish

    bot = _FakeBot(message)
    monkeypatch.setattr(s7_publish.PUBLISHER, "_bot", bot)
    monkeypatch.setattr(
        s7_publish, "RATE", SimpleNamespace(bot_api=SimpleNamespace(take=_noop_take))
    )
    return bot


def test_the_publisher_hands_back_the_id_off_the_platform_message(monkeypatch):
    """`TelegramPublisher.send` is where the id is read, so it is tested there.

    `_publish_one` cannot see past this method: an implementation that returned
    `message.message_id` raw would put `None`, `0` or an aiogram field of some other
    type into `posts.tg_message_id`, and an implementation that assumed the reply
    always carries an object would raise `AttributeError` on a `None` message — a
    crash the retry classifier reads as a transport failure, which is how a send
    the platform may already have accepted gets sent a second time.
    """
    from aiogram.types import Chat, Message

    from tgfarm import s7_publish

    def _message(message_id: object) -> Message:
        return Message.model_construct(
            message_id=message_id,
            date=datetime.now(UTC),
            chat=Chat.model_construct(id=REAL_TG_CHAT, type="channel"),
        )

    # 1. The documented answer: a `Message` with a real id.
    bot = _publisher_world(monkeypatch, _message(MEASURABLE_ID))
    assert asyncio.run(s7_publish.PUBLISHER.send(REAL_TG_CHAT, "Текст.", None)) == MEASURABLE_ID
    assert bot.calls == [(REAL_TG_CHAT, "Текст.")], bot.calls
    assert bot.session.closed is True, "the session is closed on every exit path"

    # 2. An id the platform left as a placeholder is no id.
    bot = _publisher_world(monkeypatch, _message(0))
    assert asyncio.run(s7_publish.PUBLISHER.send(REAL_TG_CHAT, "Текст.", None)) is None

    # 3. A reply with no message object at all returns `None` rather than raising.
    bot = _publisher_world(monkeypatch, None)
    assert asyncio.run(s7_publish.PUBLISHER.send(REAL_TG_CHAT, "Текст.", None)) is None


def test_a_placeholder_id_reaching_the_outcome_write_is_stated_not_stored(monkeypatch, cfg):
    """The end-to-end shape of the live 15 rows, driven through the real writer.

    `tg_chat_id = -1` and `tg_message_id IS NULL` on a `published` row is the
    evidence-free cohort `maintenance.publication_evidence_repair` exists to clean
    up. A sender returning `-1` — a placeholder, not a message — must not be able to
    produce it: the id is refused, so the row never reaches `published` at all.
    """
    post_id, _public = _seed_post(STATUS_SCHEDULED)
    calls: list = []
    _world(monkeypatch, cfg)
    _sender(monkeypatch, returns=-1, calls=calls)

    result = _publish(post_id)

    assert result["ok"] is False, result
    after = _read(post_id)
    assert after["status"] == STATUS_PUBLISH_UNKNOWN, after
    assert after["tg_message_id"] is None, after
    assert after["tg_chat_id"] is None, "the invented chat must not come back either"


# ------------------------------------------------------------------- the normalizer
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (MEASURABLE_ID, MEASURABLE_ID),
        ("4242", MEASURABLE_ID),
        (SimpleNamespace(message_id=MEASURABLE_ID), MEASURABLE_ID),
        ({"message_id": MEASURABLE_ID}, MEASURABLE_ID),
        (None, None),
        (0, None),
        (-1, None),
        (True, None),
        ("", None),
        ("dry-4242", None),
        ({"ok": True}, None),
        (object(), None),
        (SimpleNamespace(message_id=0), None),
    ],
    ids=[
        "int",
        "numeric_string",
        "message_object",
        "dict_body",
        "none",
        "zero",
        "minus_one",
        "bool",
        "empty",
        "placeholder",
        "dict_without_id",
        "opaque_object",
        "message_object_without_id",
    ],
)
def test_the_id_normalizer_accepts_only_a_number_telegram_can_answer_about(
    value, expected
):
    """One place turns a platform reply into `posts.tg_message_id`'s value.

    Everything untrusted is reduced to an inert positive integer or to `None`; no
    text is kept, interpreted, or interpolated anywhere near a query. A 40 KB payload
    must not raise either — the normalizer runs on data that came back from outside.
    """
    assert _recordable_message_id(value) == expected
    assert _recordable_message_id("x" * 40000) is None
