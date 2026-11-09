"""Read-only Telegram session, channel reconciliation, and new source collectors."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from tgfarm.config import SourceSpec
from tgfarm.tg_sync import entity_id


# ------------------------------------------------------------------ read-only
class _FakeClient:
    """Stands in for a Telethon client."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def get_me(self):
        return "me"

    async def get_dialogs(self):
        return []

    async def send_message(self, *a, **k):  # pragma: no cover - must never run
        self.calls.append("send_message")
        return "sent"

    async def disconnect(self):
        self.calls.append("disconnect")


def test_read_allows_reads():
    import asyncio

    from tgfarm.tguser import ReadOnlyClient

    guard = ReadOnlyClient(_FakeClient())
    assert asyncio.run(guard.get_me()) == "me"


@pytest.mark.parametrize(
    "method",
    [
        "send_message", "send_file", "send_photo", "forward_messages",
        "delete_messages", "edit_message", "join_channel", "leave_channel",
        "create_channel", "invite_to_group", "add_contact", "delete_account",
        "log_out",
    ],
)
def test_read_only_blocks_writes(method):
    from tgfarm.tguser import ReadOnlyClient, ReadOnlyViolation

    guard = ReadOnlyClient(_FakeClient())
    with pytest.raises(ReadOnlyViolation):
        getattr(guard, method)


def test_read_only_blocks_attribute_assignment():
    from tgfarm.tguser import ReadOnlyClient, ReadOnlyViolation

    guard = ReadOnlyClient(_FakeClient())
    with pytest.raises(ReadOnlyViolation):
        guard.anything = 1  # noqa: B018


def test_read_only_closes_the_underlying_client():
    import asyncio

    from tgfarm.tguser import ReadOnlyClient

    fake = _FakeClient()
    guard = ReadOnlyClient(fake)

    async def go():
        async with guard:
            pass

    asyncio.run(go())
    assert "disconnect" in fake.calls


def test_session_lives_outside_the_repository():
    """The session file is the account; it must not sit where git can see it.

    Asserted as the property, not as a directory name. The root is
    `Settings.data_root()`, which `tests/conftest.py`'s session isolation points at
    a per-run temporary directory, so the leaf is no longer literally `data`. The
    old `"data" in path.parts` check named the fallback constant's spelling and
    went red the moment isolation started working: it was testing a name rather
    than the account's safety. What has to hold is that the session sits inside
    the data root the operator configures and outside the repository git tracks.
    """
    from tgfarm.core.settings import get_settings
    from tgfarm.tguser import session_path

    repo_root = Path(__file__).resolve().parents[1]
    data_root = get_settings().data_root().resolve()
    path = session_path().resolve()

    assert path.is_relative_to(data_root), "the session left the data root the operator configures"
    assert not path.is_relative_to(repo_root), "the account session sits where git can see it"
    assert path.suffix != ".py"


# ------------------------------------------------------------- id translation
@pytest.mark.parametrize(
    ("api_chat_id", "entity"),
    [
        (-1001234567890, 1234567890),  # channel
        (-1009876543210, 9876543210),  # another channel
        (-987654321, 987654321),  # group
    ],
)
def test_entity_id_translation(api_chat_id, entity):
    """The Bot API and MTProto spell the same chat differently."""
    assert entity_id(api_chat_id) == entity


def test_channel_matching_prefers_username(monkeypatch):
    from tgfarm.tg_sync import _match

    class Chat:
        def __init__(self, id, title, username=None, kind="channel"):
            self.id, self.title, self.username, self.kind = id, title, username, kind
            self.linked_title = None

    chats = [
        Chat(1, "AI & Tech — разбор без хайпа", "other_channel"),
        Chat(2, "Канал технологий", "my_channel"),
    ]
    found = _match("ai_tech_ru", "AI & Tech — разбор без хайпа", "my_channel", chats)
    assert found is not None and found.id == 2


def test_channel_matching_falls_back_to_title():
    from tgfarm.tg_sync import _match

    class Chat:
        def __init__(self, id, title, username=None, kind="channel"):
            self.id, self.title, self.username, self.kind = id, title, username, kind
            self.linked_title = None

    chats = [Chat(7, "Наука — коротко и по делу")]
    found = _match("science_ru", "Наука - коротко и по делу", None, chats)
    assert found is not None and found.id == 7


# ------------------------------------------------------------------ collectors
def spec(kind: str, **params) -> SourceSpec:
    return SourceSpec(id=f"t_{kind}", name=kind, kind=kind, url="", niches=["tech"], params=params)


@pytest.mark.asyncio
async def test_collect_all_detailed_preserves_success_and_failure_per_source():
    from tgfarm.core.schemas import RawItem
    from tgfarm.s1_sources.base import collect_all_detailed

    good = SourceSpec(
        id="collector_good",
        name="good",
        kind="good",
        url="https://example.invalid/feed",
        niches=["tech"],
    )
    bad = SourceSpec(
        id="collector_bad",
        name="bad",
        kind="bad",
        url="https://example.invalid/bad",
        niches=["tech"],
    )

    class GoodCollector:
        async def fetch(self, source, client):
            return [
                RawItem(
                    source_id=source.id,
                    external_id="item-1",
                    url="https://example.invalid/story",
                    title="A grounded source headline",
                    text="Source body",
                    niche_hint=["tech"],
                )
            ]

    class BadCollector:
        async def fetch(self, source, client):
            raise RuntimeError("feed is down")

    results = await collect_all_detailed(
        {"good": GoodCollector(), "bad": BadCollector()},
        [good, bad],
    )
    by_id = {result.source_id: result for result in results}

    assert by_id["collector_good"].error is None
    assert len(by_id["collector_good"].items) == 1
    assert by_id["collector_bad"].items == []
    assert "feed is down" in by_id["collector_bad"].error


def test_source_circuit_breaker_parks_and_retries_after_cooldown(monkeypatch):
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from tgfarm import s1_sources
    from tgfarm.config import SourceSpec
    from tgfarm.db.models import Source
    from tgfarm.db.session import session_scope

    source_id = "source_circuit_test"
    spec = SourceSpec(
        id=source_id,
        name="Circuit test",
        kind="rss",
        url="https://example.invalid/feed",
        niches=["tech"],
        enabled=True,
        min_interval_s=600,
    )
    settings = SimpleNamespace(
        source_auto_cooldown_after_failures=3,
        source_auto_cooldown_hours=24,
        source_adaptive_cadence=True,
        source_idle_backoff_max=6,
        source_failure_backoff_max=4,
    )
    monkeypatch.setattr(s1_sources, "get_settings", lambda: settings)
    monkeypatch.setattr(
        s1_sources,
        "get_config",
        lambda: SimpleNamespace(sources=[spec]),
    )

    with session_scope() as session:
        session.merge(
            Source(
                id=source_id,
                name=spec.name,
                kind=spec.kind,
                url=spec.url,
                niches=spec.niches,
                enabled=True,
                min_interval_s=spec.min_interval_s,
            )
        )

    try:
        for _ in range(3):
            s1_sources._mark_source(source_id, False, "temporary timeout")

        with session_scope() as session:
            row = session.get(Source, source_id)
            assert row is not None
            assert row.failures == 3
            assert row.last_status == "cooldown"
            assert row.cooldown_until is not None
            cooldown_until = row.cooldown_until
            if cooldown_until.tzinfo is None:
                cooldown_until = cooldown_until.replace(tzinfo=UTC)
            assert cooldown_until > datetime.now(UTC) + timedelta(hours=23)

        assert s1_sources.due_sources() == []
        due_again = s1_sources.due_sources(cooldown_until + timedelta(minutes=1))
        assert [s.id for s in due_again] == [source_id]

        s1_sources._mark_source(source_id, True, items_new=1)
        with session_scope() as session:
            row = session.get(Source, source_id)
            assert row is not None
            assert row.failures == 0
            assert row.cooldown_until is None
            assert row.last_status == "ok"
    finally:
        with session_scope() as session:
            row = session.get(Source, source_id)
            if row is not None:
                session.delete(row)


def test_reddit_url_for_a_subreddit():
    from tgfarm.s1_sources.extra_sources import RedditCollector

    url = RedditCollector().render_url(spec("reddit", subreddit="LocalLLaMA"))
    assert url.startswith("https://www.reddit.com/r/LocalLLaMA/.rss")


def test_reddit_url_for_search():
    from tgfarm.s1_sources.extra_sources import RedditCollector

    url = RedditCollector().render_url(spec("reddit", search="open weights", sort="top"))
    assert "search.rss?q=open%20weights" in url


def test_hackernews_url():
    from tgfarm.s1_sources.extra_sources import HackerNewsCollector

    assert "hnrss.org/frontpage" in HackerNewsCollector().render_url(spec("hackernews", min_points=150))
    query_url = HackerNewsCollector().render_url(spec("hackernews", query="LLM"))
    assert "hnrss.org/newest?q=LLM" in query_url


def test_youtube_url_requires_a_channel():
    from tgfarm.s1_sources.extra_sources import YouTubeCollector

    with pytest.raises(ValueError):
        YouTubeCollector().render_url(spec("youtube"))
    url = YouTubeCollector().render_url(spec("youtube", channel_id="UC123"))
    assert url == "https://www.youtube.com/feeds/videos.xml?channel_id=UC123"


def test_github_url_requires_owner_and_repo():
    from tgfarm.s1_sources.extra_sources import GitHubCollector

    with pytest.raises(ValueError):
        GitHubCollector().render_url(spec("github", owner="vllm-project"))
    url = GitHubCollector().render_url(spec("github", owner="vllm-project", repo="vllm"))
    assert url == "https://github.com/vllm-project/vllm/releases.atom"


def test_telegram_source_refuses_invite_links():
    """An invite link is not a public channel and would need the account to join."""
    from tgfarm.s1_sources.telegram_source import target_of

    invite = SourceSpec(id="x", name="x", kind="telegram", url="https://t.me/+secret")
    with pytest.raises(ValueError):
        target_of(invite)


def test_telegram_source_accepts_public_links():
    from tgfarm.s1_sources.telegram_source import target_of

    public = SourceSpec(id="x", name="x", kind="telegram", url="https://t.me/habr")
    assert target_of(public) == "habr"


def test_source_registry_accepts_t_me_dark2weboriginal(cfg):
    sources = {source.id: source for source in cfg.sources}
    source = sources["tg_dark2weboriginal"]
    assert source.kind == "telegram"
    assert source.url == "https://t.me/Dark2weboriginal"
    assert source.params.get("channel") == "Dark2weboriginal"
    assert source.params.get("handle") == "Dark2weboriginal"


def test_title_is_derived_from_the_first_line():
    from tgfarm.s1_sources.telegram_source import _title_from

    assert _title_from("Первая строка\n\nВторая часть текста") == "Первая строка"
    assert _title_from("Короткая новость. И продолжение с деталями.").startswith("Короткая новость")


# ------------------------------------------------------------------ host pacing
@pytest.mark.asyncio
async def test_same_host_is_paced_and_different_hosts_are_not():
    from tgfarm.s1_sources.base import HostPacer

    pacer = HostPacer(interval=0.4)
    started = time.monotonic()
    await asyncio.gather(
        pacer.wait("https://a.invalid/1"),
        pacer.wait("https://a.invalid/2"),
        pacer.wait("https://b.invalid/1"),
    )
    elapsed = time.monotonic() - started
    # one gap between the two same-host requests, none for the other host
    assert elapsed >= 0.35


def test_host_pacer_uses_a_per_host_override():
    from tgfarm.s1_sources.base import HostPacer

    pacer = HostPacer(interval=2.0)
    assert pacer.interval_for("www.reddit.com") == 10.0
    assert pacer.interval_for("example.invalid") == 2.0


# ------------------------------------------------------------------- markdown
def test_preview_markdown_does_not_italicise_identifiers():
    from tgfarm.api import _telegram_html

    out = _telegram_html("источник gnews_business_ru")
    assert "<i>" not in out
    assert "gnews_business_ru" in out


def test_preview_markdown_still_bolds_and_links():
    from tgfarm.api import _telegram_html

    out = _telegram_html("**жирно** и [источник](https://x.invalid)")
    assert "<b>жирно</b>" in out
    assert '<a href="https://x.invalid">источник</a>' in out


def test_preview_escapes_html():
    from tgfarm.api import _telegram_html

    assert "<script>" not in _telegram_html("<script>alert(1)</script>")

@pytest.mark.parametrize("target", [
    "javascript:alert(document.cookie)",
    "JaVaScRiPt:alert(1)",
    "data:text/html;base64,AAAA",
    "vbscript:x",
    "&#106;avascript:alert(1)",
])
def test_preview_never_turns_an_unsafe_target_into_a_live_link(target):
    from tgfarm.api import _telegram_html

    out = _telegram_html(f"[жми]({target})")
    assert "<a " not in out
    assert "жми" in out


def test_preview_keeps_http_and_tg_links():
    from tgfarm.api import _telegram_html

    assert '<a href="tg://user?id=1">' in _telegram_html("[x](tg://user?id=1)")
    assert '<a href="https://a.invalid/?b=1&amp;c=2">' in _telegram_html("[x](https://a.invalid/?b=1&c=2)")
