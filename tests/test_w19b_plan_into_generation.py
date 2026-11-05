"""W19-B: the editorial plan's booked decision must survive the handoff into S4.

`tgfarm/editorial_plan.py` decides *what* to write, *for which channel*, *in
which format* and *at which slot*; `tgfarm/plan_execution.py` is that plan's
consumer. At base the two never met on the generation path: a `produce` entry
was reported as `no_generation_entry_point` and S4 re-derived the destination
from S3 routing and the format from S10 evidence, so the editorial decision was
lost exactly where it mattered — the farm wrote something else.

These tests drive the real stage entry, `tgfarm.s4_generation.run()`, with a
`produce` entry booked in the ACTIVE plan, and read the answer off the database
row the stage wrote. Nothing here calls a helper directly: routing and video
discovery are the only stages stubbed, generation and persistence are the real
offline path.

Database hygiene: the suite shares one session database
(`tests/conftest.py:242`), so every channel, cluster, post and plan snapshot is
`w19bgen_`-prefixed, counted as a delta against its own baseline and deleted
again by the autouse teardown. `HOLDOUT_SHARE=0` is pinned for the duration of
each test so two runs of the same corpus are comparable at all.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select

from tgfarm import plan_execution, s3_router, s4_generation, s11_video
from tgfarm.config import Persona
from tgfarm.core.settings import get_settings, reset_settings_cache
from tgfarm.db.models import Channel, EditorialPlan, EditorialPlanEntry, Post, RawItem
from tgfarm.db.models import Cluster as ClusterRow
from tgfarm.db.session import session_scope
from tgfarm.s3_router import ChannelPlan
from tgfarm.s10_format import LENGTH_BUDGET

#: A source text long enough that the offline composer has real material to
#: write from in Russian, which `generate()` requires before it writes anything.
_ITEMS = (
    (
        "ЦБ сохранил ставку на уровне 19 процентов",
        "Банк России оставил ключевую ставку на уровне 19 процентов, "
        "аналитики ожидали повышения до 20 процентов. Регулятор сообщил, что "
        "инфляция замедляется четвёртую неделю подряд, а рынок труда остаётся "
        "в напряжённом состоянии. Следующее заседание запланировано на декабрь.",
    ),
    (
        "Ставка 19 процентов: что это значит для депозитов",
        "Экономисты подсчитали, что при ставке 19 процентов средняя ставка по "
        "вкладам держится около 17 процентов, а реальная доходность зависит от "
        "инфляционных ожиданий. Несколько банков уже объявили о повышении "
        "процентных ставок по коротким депозитам.",
    ),
)


class _Owned:
    """Every row this file creates, so teardown deletes exactly these."""

    def __init__(self) -> None:
        self.channel_ids: list[str] = []
        self.cluster_ids: list[str] = []
        self.post_public_ids: list[str] = []
        self.plan_public_ids: list[str] = []


@pytest.fixture()
def owned():
    holder = _Owned()
    yield holder
    with session_scope() as session:
        plan_ids = (
            list(
                session.execute(
                    select(EditorialPlan.id).where(
                        EditorialPlan.public_id.in_(holder.plan_public_ids)
                    )
                ).scalars().all()
            )
            if holder.plan_public_ids
            else []
        )
        if plan_ids:
            session.execute(delete(EditorialPlanEntry).where(EditorialPlanEntry.plan_id.in_(plan_ids)))
            session.execute(delete(EditorialPlan).where(EditorialPlan.id.in_(plan_ids)))
        if holder.post_public_ids:
            session.execute(delete(Post).where(Post.public_id.in_(holder.post_public_ids)))
        if holder.cluster_ids:
            session.execute(delete(RawItem).where(
                RawItem.cluster_id.in_(
                    select(ClusterRow.id).where(ClusterRow.public_id.in_(holder.cluster_ids))
                )
            ))
            session.execute(delete(ClusterRow).where(ClusterRow.public_id.in_(holder.cluster_ids)))
        if holder.channel_ids:
            session.execute(delete(Channel).where(Channel.id.in_(holder.channel_ids)))


@pytest.fixture()
def flag_on(monkeypatch):
    monkeypatch.setenv("PLAN_EXECUTION_ENABLED", "true")
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture(autouse=True)
def deterministic_generation(monkeypatch):
    """No holdout coin-flip, no foreign active plan, no dzen provenance hold.

    `HOLDOUT_SHARE` makes one in ten drafts skip the few-shot examples, which
    would make two runs of the same corpus incomparable; a foreign active plan
    could win a same-second `created_at` tie in the shared database; and with
    `DZEN_REQUIRES_LLM` on (the default) an offline Dzen draft is dropped before
    it is ever written — none of those three is the fact under test.
    """
    monkeypatch.setenv("HOLDOUT_SHARE", "0")
    monkeypatch.setenv("DZEN_REQUIRES_LLM", "false")
    reset_settings_cache()
    with session_scope() as session:
        foreign = list(
            session.execute(
                select(EditorialPlan.id).where(EditorialPlan.status == "active")
            ).scalars().all()
        )
        for plan_id in foreign:
            session.get(EditorialPlan, plan_id).status = "superseded"
    yield
    with session_scope() as session:
        for plan_id in foreign:
            row = session.get(EditorialPlan, plan_id)
            if row is not None:
                row.status = "active"
    reset_settings_cache()


def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _channel(holder: _Owned, *, platform: str = "telegram", title: str = "Канал") -> str:
    channel_id = f"w19bgen_ch_{_uid()}"
    holder.channel_ids.append(channel_id)
    with session_scope() as session:
        session.add(
            Channel(
                id=channel_id,
                platform=platform,
                title=title,
                niche="business",
                enabled=True,
                posts_per_day=5,
            )
        )
    return channel_id


def _persona(channel_id: str, platform: str) -> Persona:
    return Persona(
        id=channel_id,
        title="Канал",
        niche="business",
        platform=platform,
        min_chars=150,
        max_chars=600,
        posts_per_day=5,
        audience="читатели канала",
        tone=["factual"],
    )


def _plan_obj(channel_id: str, platform: str, cluster_id: str) -> ChannelPlan:
    """One routing decision, shaped exactly as S3 hands it to S4."""
    return ChannelPlan(
        cluster_public_id=cluster_id,
        channel_id=channel_id,
        persona=_persona(channel_id, platform),
        angle="news",
        score=0.8,
        reason="тест",
        platform=platform,
    )


def _cluster(holder: _Owned) -> str:
    public_id = f"w19bgen_cl_{_uid()}"
    holder.cluster_ids.append(public_id)
    with session_scope() as session:
        cluster = ClusterRow(
            public_id=public_id,
            label=_ITEMS[0][0],
            niche="business",
            status="ready",
            heat=0.7,
            last_seen_at=datetime.now(UTC) - timedelta(hours=1),
        )
        session.add(cluster)
        session.flush()
        for index, (title, text) in enumerate(_ITEMS):
            session.add(
                RawItem(
                    source_id=f"w19bgen_src_{_uid()}",
                    external_id=f"w19bgen_ext_{_uid()}",
                    url=f"https://w19bgen.invalid/{index}-{_uid()}",
                    title=title,
                    text=text,
                    lang="ru",
                    niche_hint=["business"],
                    source_weight=0.6,
                    cluster_id=cluster.id,
                )
            )
    return public_id


def _produce_entries(holder: _Owned, specs: list[dict]) -> tuple[str, list[int]]:
    """One ACTIVE snapshot holding exactly these `produce` bookings."""
    plan_public_id = f"w19bgen_plan_{_uid()}"
    holder.plan_public_ids.append(plan_public_id)
    entry_ids: list[int] = []
    with session_scope() as session:
        plan = EditorialPlan(
            public_id=plan_public_id,
            created_at=datetime.now(UTC),
            horizon_days=7,
            status="active",
            summary={"total": len(specs), "produce": len(specs)},
        )
        session.add(plan)
        session.flush()
        for spec in specs:
            entry = EditorialPlanEntry(
                plan_id=plan.id,
                cluster_public_id=spec["cluster_id"],
                title=_ITEMS[0][0],
                niche="business",
                action="produce",
                platform=spec["platform"],
                channel_id=spec["channel_id"],
                format=spec["fmt"],
                priority=1,
                confidence=0.7,
                reasons=["тест"],
                slot_at=spec["slot_at"],
                payload={},
            )
            session.add(entry)
            session.flush()
            entry_ids.append(int(entry.id))
    return plan_public_id, entry_ids


def _produce_plan(holder: _Owned, *, cluster_id: str, channel_id: str, platform: str,
                  fmt: str | None, slot_at: datetime | None) -> tuple[str, int]:
    """An ACTIVE snapshot with one `produce` entry, as the plan would book it."""
    plan_public_id, entry_ids = _produce_entries(holder, [
        {"cluster_id": cluster_id, "channel_id": channel_id, "platform": platform,
         "fmt": fmt, "slot_at": slot_at},
    ])
    return plan_public_id, entry_ids[0]


def _drive(monkeypatch, cluster_id: str, plans: list[ChannelPlan]) -> list[str]:
    """The real stage entry: only routing and video discovery are stubbed.

    Generation and persistence are production code, so what the plan booked is
    read back off the `posts` row the stage wrote rather than off a mock.
    """
    from tgfarm import s2_trends

    async def fake_plan_cluster(_cid):
        return list(plans)

    async def fake_fetch_video(_item):
        return None

    monkeypatch.setattr(s3_router, "plan_cluster", fake_plan_cluster)
    monkeypatch.setattr(s11_video, "fetch_video_for", fake_fetch_video)
    monkeypatch.setattr(s2_trends, "mark_consumed", lambda _cid: None)
    return asyncio.run(s4_generation.run(cluster_id))


def _drive_single(monkeypatch, holder: _Owned, cluster_id: str, channel_id: str,
                  platform: str = "telegram") -> list[str]:
    """One routed destination, tracked for teardown."""
    return _track_posts(
        holder,
        _drive(monkeypatch, cluster_id, [_plan_obj(channel_id, platform, cluster_id)]),
    )


def _post_row(public_id: str) -> dict:
    with session_scope() as session:
        row = session.execute(
            select(Post).where(Post.public_id == public_id)
        ).scalar_one()
        values = {column.name: getattr(row, column.name) for column in Post.__mapper__.columns}
    return values


def _generated_texts(monkeypatch) -> list[dict]:
    """Wrap the real `generate`, keeping its result, to see what it was handed."""
    seen: list[dict] = []
    real_generate = s4_generation.generate

    async def spy(plan, raw_items, format_plan=None, **kwargs):
        seen.append(
            {
                "channel_id": plan.channel_id,
                "cluster_public_id": plan.cluster_public_id,
                "fmt": getattr(format_plan, "fmt", None),
                "min_chars": getattr(format_plan, "min_chars", None),
                "reason": getattr(format_plan, "reason", None),
            }
        )
        return await real_generate(plan, raw_items, format_plan, **kwargs)

    monkeypatch.setattr(s4_generation, "generate", spy)
    return seen


def _track_posts(holder: _Owned, created: list[str]) -> list[str]:
    holder.post_public_ids.extend(created)
    return created


# ------------------------------------------------------------------ deliverable 1
def test_a_booked_produce_entry_is_generated_for_the_booked_channel(
    monkeypatch, flag_on, owned
):
    """The plan says which destination; S4 must not re-decide it.

    Two destinations were routed for this cluster and the plan booked one of
    them. The generation call is the one the plan named, and the other channel
    gets no draft — otherwise the plan is a queue again.
    """
    booked = _channel(owned, platform="dzen", title="Всякая всячина")
    other = _channel(owned, platform="vk", title="Другой")
    cluster_id = _cluster(owned)
    slot = datetime.now(UTC) + timedelta(hours=3)
    _produce_plan(
        owned,
        cluster_id=cluster_id,
        channel_id=booked,
        platform="dzen",
        fmt="article",
        slot_at=slot,
    )
    seen = _generated_texts(monkeypatch)

    created = _track_posts(owned, _drive(monkeypatch, cluster_id, [
        _plan_obj(other, "vk", cluster_id),
        _plan_obj(booked, "dzen", cluster_id),
    ]))

    assert len(created) == 1, f"план забронировал один канал: {created}"
    assert [call["channel_id"] for call in seen] == [booked]
    with session_scope() as session:
        row = session.execute(
            select(Post.channel_id, Post.cluster_public_id).where(
                Post.public_id == created[0]
            )
        ).one()
    assert str(row[0]) == booked
    assert row[1] == cluster_id


def test_a_booked_format_reaches_the_generation_call_and_the_written_post(
    monkeypatch, flag_on, owned
):
    """Booked as a long-form article ⇒ generated as a long-form article.

    Proved three times over on the real path: the `generate()` call receives
    the booked `fmt` and the booked length budget from S10's own table, the text
    the offline composer actually wrote has the article's opening move, and the
    stored row carries the booked format.
    """
    booked = _channel(owned, platform="dzen")
    cluster_id = _cluster(owned)
    _produce_plan(
        owned,
        cluster_id=cluster_id,
        channel_id=booked,
        platform="dzen",
        fmt="article",
        slot_at=datetime.now(UTC) + timedelta(hours=2),
    )
    seen = _generated_texts(monkeypatch)

    created = _drive_single(monkeypatch, owned, cluster_id, booked, "dzen")

    assert len(created) == 1
    call = seen[0]
    assert call["fmt"] == "article"
    assert (call["min_chars"], LENGTH_BUDGET["article"][1]) == (
        LENGTH_BUDGET["article"][0],
        LENGTH_BUDGET["article"][1],
    ), "забронированный формат обязан нести и бюджет длины S10"
    assert s4_generation.BOOKED_FORMAT_MARK in str(call["reason"]), (
        "причина обязана называть владельца выбора формата"
    )

    values = _post_row(created[0])
    assert values["post_format"] == "article"
    assert "Разбираемся по порядку." in str(values["hook"]), (
        "текст должен быть написан как забронированный формат, а не только помечен им"
    )


def test_a_booked_format_s10_does_not_know_falls_back_and_does_not_stop_generation(
    monkeypatch, flag_on, owned
):
    """`editorial_plan_entries.format` is a free-text column, so a booking the
    format planner has no length budget for cannot be allowed to raise: the
    cluster would silently stop producing. The planner's own choice stands."""
    booked = _channel(owned)
    cluster_id = _cluster(owned)
    _produce_plan(
        owned,
        cluster_id=cluster_id,
        channel_id=booked,
        platform="telegram",
        fmt="thread",
        slot_at=None,
    )
    seen = _generated_texts(monkeypatch)

    created = _drive_single(monkeypatch, owned, cluster_id, booked)

    assert len(created) == 1
    assert seen[0]["fmt"] in LENGTH_BUDGET
    assert _post_row(created[0])["post_format"] == seen[0]["fmt"]


def test_a_produce_entry_without_a_booked_format_keeps_the_planner_choice(
    monkeypatch, flag_on, owned
):
    """What the real planner writes today.

    `editorial_plan.py` fills `format` from `row["post_format"]`, which
    `tgfarm/editorial.py::queue()` never emits, so a live `produce` entry has
    `format = NULL`. The brief must then steer nothing but the destination.
    """
    booked = _channel(owned)
    other = _channel(owned)
    cluster_id = _cluster(owned)
    _produce_plan(
        owned,
        cluster_id=cluster_id,
        channel_id=booked,
        platform="telegram",
        fmt=None,
        slot_at=None,
    )
    seen = _generated_texts(monkeypatch)

    created = _track_posts(owned, _drive(monkeypatch, cluster_id, [
        _plan_obj(other, "telegram", cluster_id),
        _plan_obj(booked, "telegram", cluster_id),
    ]))

    assert len(created) == 1
    assert seen[0]["channel_id"] == booked
    assert seen[0]["fmt"] in LENGTH_BUDGET, "без брони формат остаётся за S10"


def test_a_booked_destination_the_router_refused_writes_nothing(
    monkeypatch, flag_on, owned
):
    """If routing cannot reach the booked channel, S4 does not invent another.

    The topic stays unconsumed so a later tick or a rebuilt plan can still
    serve it; quietly publishing the plan's topic somewhere else is the exact
    failure this wave is closing.
    """
    booked = _channel(owned)
    absent = _channel(owned)
    cluster_id = _cluster(owned)
    _produce_plan(
        owned,
        cluster_id=cluster_id,
        channel_id=booked,
        platform="telegram",
        fmt="news",
        slot_at=None,
    )
    seen = _generated_texts(monkeypatch)

    created = _drive_single(monkeypatch, owned, cluster_id, absent)

    assert created == []
    assert seen == [], "несогласованная бронь не имеет права генерировать другой канал"


# ------------------------------------------------------------------ deliverable 3
def test_the_produced_post_names_the_plan_entry_that_ordered_it(
    monkeypatch, flag_on, owned
):
    """Provenance is a stored link, not an inference from the topic.

    `cluster_public_id` cannot answer the panel's question — the same cluster is
    planned again by every snapshot — so the row carries the id of the entry
    that ordered it and the slot that entry booked.
    """
    booked = _channel(owned, platform="dzen")
    cluster_id = _cluster(owned)
    slot = datetime.now(UTC) + timedelta(hours=5, minutes=30)
    plan_public_id, entry_id = _produce_plan(
        owned,
        cluster_id=cluster_id,
        channel_id=booked,
        platform="dzen",
        fmt="article",
        slot_at=slot,
    )

    created = _drive_single(monkeypatch, owned, cluster_id, booked, "dzen")

    values = _post_row(created[0])
    assert values["plan_entry_id"] == entry_id
    # SQLite hands back naive timestamps and the pipeline normalises at the
    # boundary (`core.ensure_utc`), so the stored instant is compared as written.
    assert values["plan_slot_at"].replace(tzinfo=None) == slot.replace(tzinfo=None)
    assert values["status"] == "draft", "провенанс не отменяет очередь: черновик идёт через S5"
    with session_scope() as session:
        stored = session.execute(
            select(EditorialPlanEntry.plan_id, EditorialPlan.public_id)
            .join(EditorialPlan, EditorialPlanEntry.plan_id == EditorialPlan.id)
            .where(EditorialPlanEntry.id == values["plan_entry_id"])
        ).one()
    assert stored[1] == plan_public_id, "ссылка обязана вести в тот же снимок плана"


def test_each_produced_post_names_its_own_entry_not_the_newest_one(
    monkeypatch, flag_on, owned
):
    """The link is per topic and it is written when the row is created.

    One snapshot books two topics on two channels, and the second booking is the
    newer row. A run for the first topic must still name the first entry: were the
    reader to take the snapshot's newest booking instead of this cluster's, or a
    later run to rewrite a row it did not create, the panel would be shown an
    entry that never ordered the post.
    """
    channel_a = _channel(owned)
    channel_b = _channel(owned)
    cluster_a = _cluster(owned)
    cluster_b = _cluster(owned)
    _, entry_ids = _produce_entries(owned, [
        {"cluster_id": cluster_a, "channel_id": channel_a, "platform": "telegram",
         "fmt": "news", "slot_at": None},
        {"cluster_id": cluster_b, "channel_id": channel_b, "platform": "telegram",
         "fmt": "news", "slot_at": None},
    ])

    first = _drive_single(monkeypatch, owned, cluster_a, channel_a)
    second = _drive_single(monkeypatch, owned, cluster_b, channel_b)

    assert len(first) == 1 and len(second) == 1
    assert _post_row(first[0])["plan_entry_id"] == entry_ids[0]
    assert _post_row(second[0])["plan_entry_id"] == entry_ids[1]
    assert entry_ids[1] > entry_ids[0], "второй заказ обязан быть новее по id"


# -------------------------------------------------------------------- flag off
@pytest.fixture()
def flag_off(monkeypatch):
    """The shipped default, asserted rather than assumed."""
    monkeypatch.delenv("PLAN_EXECUTION_ENABLED", raising=False)
    reset_settings_cache()
    assert get_settings().plan_execution_enabled is False
    yield
    reset_settings_cache()


def test_the_reader_opens_no_session_while_the_flag_is_off(monkeypatch, flag_off):
    """Off the flag, base behaviour means "no plan consumer at all".

    A database read the old path never performed is already a difference, so the
    reader is tested against the session it must not open, not only against the
    `None` it returns.
    """

    def _no_session(*_args, **_kwargs):
        raise AssertionError("off the flag the plan reader must not touch the database")

    monkeypatch.setattr(plan_execution, "session_scope", _no_session)
    assert plan_execution.generation_brief("w19bgen_cl_absent") is None


def test_the_flag_off_path_writes_what_a_planless_run_writes(monkeypatch, flag_off, owned):
    """Byte-for-byte: an ACTIVE booking changes nothing while the flag is off.

    Two runs of the same corpus, identical personas and angles — one with a
    `produce` entry booked for the first channel only, one with no plan at all.
    Every column the booking could have steered is compared: the destination set,
    the format, the text, and the two provenance columns. They agree, and both
    channels are written, so the plan filtered nothing.
    """
    booked = _channel(owned)
    other = _channel(owned)
    cluster_id = _cluster(owned)
    _produce_plan(
        owned,
        cluster_id=cluster_id,
        channel_id=booked,
        platform="telegram",
        fmt="article",
        slot_at=datetime.now(UTC) + timedelta(hours=4),
    )
    with_plan = _track_posts(owned, _drive(monkeypatch, cluster_id, [
        _plan_obj(booked, "telegram", cluster_id),
        _plan_obj(other, "telegram", cluster_id),
    ]))

    plain_a = _channel(owned)
    plain_b = _channel(owned)
    plain_cluster = _cluster(owned)
    without_plan = _track_posts(owned, _drive(monkeypatch, plain_cluster, [
        _plan_obj(plain_a, "telegram", plain_cluster),
        _plan_obj(plain_b, "telegram", plain_cluster),
    ]))

    assert len(with_plan) == 2, "без плана S4 пишет все маршрутизированные каналы"
    assert len(without_plan) == 2
    #: Columns a booking could steer; identity-bearing ones (ids, URLs, the
    #: quality blob's provider stamp) are not part of the comparison.
    compared = (
        "post_format", "format_reason", "hook", "body", "cta", "hashtags", "angle",
        "language", "char_len", "status", "variant", "hook_style",
        "generation_provider", "plan_entry_id", "plan_slot_at",
    )
    for produced, control in zip(with_plan, without_plan, strict=True):
        mine = _post_row(produced)
        theirs = _post_row(control)
        assert {c: mine[c] for c in compared} == {c: theirs[c] for c in compared}, (
            f"флаг выключен — строка {produced} не совпадает с {control}"
        )
    assert _post_row(with_plan[0])["plan_entry_id"] is None
    assert _post_row(with_plan[0])["plan_slot_at"] is None
