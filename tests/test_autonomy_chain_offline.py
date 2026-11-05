"""W17-C: offline end-to-end harness for the autonomous chain.

The chain under test is the one `docs/AUTONOMY_ACCEPTANCE.md` names:

    source -> candidate -> ranked topic -> draft -> quality gate -> media
    -> schedule -> publish -> metrics -> next recommendation

Design rules, all of them enforced by code below:

* One throwaway SQLite database per module. The session DB of
  `tests/conftest.py` is never touched: the fixture repoints `DATABASE_URL`
  and `TGFARM_MEDIA_DIR` at a private temp dir, resets every runtime cache,
  and restores the environment at teardown. All row assertions are deltas
  against this fixture's own (empty) baseline, never absolute suite counts.
* Every transport that can leave the machine (httpx sync/async, requests,
  aiohttp, raw sockets) is wrapped by `OutboundRecorder`: it appends the URL
  and raises. The main run asserts the recorded call list stayed empty while
  the full chain walked from a seeded item to a publish decision.
* Each link is asserted on its OWN persisted effect (a stored item, a scored
  cluster, a journalled publish decision...), not on "the stage raised
  nothing", and each chain stage additionally leaves a real `pipeline_runs`
  row — status, timestamps, counters with the three-state honesty meta —
  written through `pipeline._stage`, the production journaling wrapper.
* The terminal state is classified explicitly and must be a member of
  `ALLOWED_TERMINAL_STATES`; a bare "ran ok" fails this file.
* Links that cannot be driven offline are NOT stubbed. They carry
  `xfail(strict=True)` tests whose reason names the exact entry point that
  is unreachable without network, so the day someone adds one, the suite
  errors on the unexpected pass and the marker comes off.

Markers: `w17chain`. No P-numbers in this file, its ids, or its markers.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import socket
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytestmark = pytest.mark.w17chain

#: The only chain endings this harness accepts as *stated* outcomes. Anything
#: else — including "the run finished and nobody says what happened" — fails.
ALLOWED_TERMINAL_STATES = frozenset(
    {
        "published_journalled",
        "held",
        "gated",
        "rejected",
        "not_enough_evidence",
    }
)

#: Journal statuses that name a decision, from `tgfarm/crosspost_journal.py`.
#: `publishing` is in-flight, not an outcome, and is deliberately absent.
JOURNAL_OUTCOME_STATUSES = frozenset(
    {
        "posted",
        "dry_run",
        "failed",
        "skipped",
        "retry_authorized",
        "permanent",
        "unknown",
        "scheduled",
    }
)

#: The chain links a full offline walk must journal (`source` is exercised by
#: the deterministic seeder, whose effect is asserted on the rows themselves).
JOURNALED_STAGES = (
    "s2_trends",
    "s4_generate",
    "s5_quality",
    "s6_visual",
    "s7_publish",
    "s7_publish_due",
    "s9_crosspost",
    "s8_analytics",
    "s12_learning",
)

#: The harness's own synthetic corpus. Three stories, each covered by several
#: independent outlets with paraphrased bodies: identical reprints would be
#: collapsed by S2's dedup into one survivor per cluster and then the heat
#: could never cross the promotion threshold — which is exactly what the
#: shipped `tgfarm demo` seed does at this base commit. Every item here is
#: written the way S1 writes them (embedding, simhash, fresh timestamps), so
#: the chain downstream is driven by production logic, not by stubs.
CHAIN_SEED_STORIES: list[tuple[str, list[tuple[str, str, str, float]]]] = [
    (
        "tech",
        [
            (
                "quantumlab_news",
                "Квантовый процессор K2 принес 256 кубитов и ранний доступ разработчикам",
                "Лаборатория QuantumLab представила чип K2 на 256 кубитов. Команда "
                "сообщает, что ошибки на длинных последовательностях снизились вдвое "
                "по сравнению с прошлым поколением. Первые наборы для разработчиков "
                "обещают отправить осенью, а коммерческое время машины продадут позже.",
                0.78,
            ),
            (
                "siliconwire",
                "Процессор K2: 256 кубитов, меньше ошибок, ранний доступ разработчикам",
                "По данным Siliconwire, процессор K2 приносит 256 кубитов и заметно "
                "более чистые длинные вычисления: доля сбоев упала примерно в два раза. "
                "Ранний доступ к оборудованию откроют узкому кругу лабораторий, а "
                "публичную очередь продаж компания обещает открыть в следующем квартале.",
                0.74,
            ),
            (
                "gnews_tech_ru",
                "Квантовый процессор K2 вышел на 256 кубитов раньше срока",
                "Русскоязычная редакция сообщает: устройство K2 с 256 кубитами прошло "
                "внутренние приёмо-сдаточные испытания, уровень шума уменьшили вдвое. "
                "Первая волна внешних тестов стартует через шесть недель, детали тарифов "
                "пока не раскрыты, наблюдатели ждут независимых замеров.",
                0.66,
            ),
        ],
    ),
    (
        "finance",
        [
            (
                "bankwatch_daily",
                "Банк России поднял ключевую ставку до 21 процента",
                "Совет директоров Банка России увеличил ключевую ставку до 21 процента "
                "впервые за шесть лет. Регулятор объяснил шаг ускорением роста цен и "
                "ожиданиями бизнеса. Следующее рассмотрение запланировано на декабрь.",
                0.8,
            ),
            (
                "marketledger",
                "Ключевая ставка 21 процент: Банк России ужесточил риторику",
                "Marketledger пишет: решение о повышении до 21 процента приняли "
                "единогласно, сигнал жёстче, чем ожидали аналитики. Депозитные ставки "
                "банков подтянутся в течение месяца, кредитование малого бизнеса "
                "замедлится, прогнозируют участники рынка.",
                0.75,
            ),
        ],
    ),
    (
        "health",
        [
            (
                "medpulse_int",
                "Наблюдение за 15 тысячами пациентов: семь тысяч шагов снижают риск",
                "Международная группа Medpulse опубликовала наблюдение за 15 тысячами "
                "участников: ежедневная дистанция около семи тысяч шагов ассоциируется "
                "с уменьшением сердечно-сосудистых событий на 17 процентов. Эффект "
                "сохранялся после поправки на возраст и давление.",
                0.77,
            ),
            (
                "clinicdigest",
                "Семь тысяч шагов снижают сердечный риск: наблюдение за 15 тысячами пациентов",
                "Clinicdigest пересказывает когорту из 15 тысяч человек: у тех, кто "
                "проходил примерно семь тысяч шагов ежедневно, сердечные приступы и "
                "инсульты случались реже почти на пятую часть. Авторы подчёркивают: "
                "это наблюдение, а не доказательство причинности.",
                0.7,
            ),
        ],
    ),
]

EXPECTED_SEED_ITEMS = sum(len(outlets) for _niche, outlets in CHAIN_SEED_STORIES)


class OutboundRecorder:
    """Wrap every outbound transport so an attempt to leave the machine is a
    recorded fact and a hard error — the zero-outbound invariant is enforced
    by raising code, not by a comment."""

    LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "0.0.0.0"})

    #: Transports the app cannot even import (`pyproject.toml` has httpx, not
    #: these two). Patched when present, named in `missing` when absent.
    OPTIONAL_TRANSPORTS = ("requests", "aiohttp")

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.guarded: list[str] = []
        self.missing: list[str] = []
        self._undo: list[tuple[object, str, object]] = []

    def _hit(self, transport: str, target: object) -> None:
        text = str(target)[:300]
        self.calls.append(f"{transport} {text}")
        raise RuntimeError(f"w17chain offline guard refused outbound {transport} call: {text}")

    def _patch(self, owner: object, name: str, value: object) -> None:
        self._undo.append((owner, name, getattr(owner, name)))
        self.guarded.append(
            f"{getattr(owner, '__module__', '?')}.{getattr(owner, '__name__', '?')}.{name}"
        )
        setattr(owner, name, value)

    def install(self) -> None:
        import httpx

        recorder = self

        def httpx_send(client_self, request, *args, **kwargs):
            recorder._hit("httpx", getattr(request, "url", request))

        async def httpx_asend(client_self, request, *args, **kwargs):
            recorder._hit("httpx.async", getattr(request, "url", request))

        def httpx_transport(transport_self, request):
            recorder._hit("httpx.transport", getattr(request, "url", request))

        async def httpx_atransport(transport_self, request):
            recorder._hit("httpx.atransport", getattr(request, "url", request))

        self._patch(httpx.Client, "send", httpx_send)
        self._patch(httpx.AsyncClient, "send", httpx_asend)
        self._patch(httpx.HTTPTransport, "handle_request", httpx_transport)
        self._patch(httpx.AsyncHTTPTransport, "handle_async_request", httpx_atransport)

        for module_name, owner_name, method in (
            ("requests", "Session", "send"),
            ("aiohttp", "ClientSession", "_request"),
        ):
            try:
                module = importlib.import_module(module_name)
            except ImportError:
                self.missing.append(module_name)
                continue
            self._patch(getattr(module, owner_name), method, self._handler(module_name))

        real_connect = socket.socket.connect

        def guarded_connect(sock_self, address):
            host = address[0] if isinstance(address, tuple) else address
            if isinstance(host, str) and host not in recorder.LOCAL_HOSTS:
                recorder._hit("socket", host)
            return real_connect(sock_self, address)

        # Last and unconditional: whatever an absent package would have hidden,
        # a real socket to a non-local host still trips.
        self._patch(socket.socket, "connect", guarded_connect)

    def _handler(self, transport: str):
        """One wrapper per transport, keeping the same signatures the patches replace."""
        if transport == "requests":

            def send(session_self, request, *args, **kwargs):
                self._hit("requests", getattr(request, "url", request))

            return send

        async def request(session_self, method, url, *args, **kwargs):
            self._hit("aiohttp", f"{method} {url}")

        return request

    def uninstall(self) -> None:
        while self._undo:
            owner, name, original = self._undo.pop()
            setattr(owner, name, original)


def _reset_runtime_caches() -> None:
    """Same handshake `tests/conftest.py` uses between database swaps."""
    from tgfarm.core import settings as settings_mod
    from tgfarm.db import session as session_mod
    from tgfarm.llm import client as client_mod
    from tgfarm.llm import embedding as embedding_mod
    from tgfarm.llm import images as images_mod

    settings_mod.reset_settings_cache()
    session_mod.dispose_engine()
    client_mod.reset_llm()
    embedding_mod.reset_embedder()
    images_mod._PROVIDER = None  # noqa: SLF001 - the cache is module state, as in conftest


def _table_counts() -> dict[str, int]:
    from sqlalchemy import func, select

    from tgfarm.db.models import (
        Channel,
        Cluster,
        CrosspostLog,
        LearningMemory,
        Media,
        PipelineRun,
        Post,
        PostStat,
        RawItem,
    )
    from tgfarm.db.session import session_scope

    models = {
        "raw_items": RawItem,
        "clusters": Cluster,
        "posts": Post,
        "media": Media,
        "crosspost_logs": CrosspostLog,
        "pipeline_runs": PipelineRun,
        "post_stats": PostStat,
        "learning_memory": LearningMemory,
        "channels": Channel,
    }
    with session_scope() as session:
        return {
            name: int(session.execute(select(func.count(model.id))).scalar_one())
            for name, model in models.items()
        }


def _seed_chain_corpus() -> int:
    """Write the harness's synthetic sources exactly the way S1 writes real ones."""
    from tgfarm.core.text import simhash
    from tgfarm.db.models import RawItem
    from tgfarm.db.session import session_scope
    from tgfarm.db.vector import get_index, pack
    from tgfarm.llm.embedding import get_embedder

    embedder = get_embedder()
    index = get_index(embedder.dim)
    now = datetime.now(UTC)
    written = 0
    with session_scope() as session:
        for niche, outlets in CHAIN_SEED_STORIES:
            for j, (source_id, title, body, weight) in enumerate(outlets):
                vector = embedder.embed(f"{title}\n{body}")
                row = RawItem(
                    source_id=source_id,
                    external_id=f"w17chain-{source_id}-{j}",
                    url=f"https://w17chain.invalid/{niche}/{source_id}/{j}",
                    title=title,
                    text=body,
                    lang="ru",
                    published_at=now - timedelta(minutes=5 + 10 * j),
                    niche_hint=[niche],
                    source_weight=weight,
                    embedding=pack(vector),
                    simhash=simhash(f"{title} {body}"),
                )
                session.add(row)
                session.flush()
                index.add(row.id, vector)
                written += 1
    return written


def _backdate_scheduled() -> int:
    """Make every scheduled post due, so one run can reach the publish decision.

    The shipped demo does the same trick; doing it here keeps the harness free
    of `tgfarm.demo` internals, whose own seed no longer survives S2 promotion.
    """
    from sqlalchemy import select

    from tgfarm.db.models import Post
    from tgfarm.db.session import session_scope

    with session_scope() as session:
        rows = session.execute(select(Post).where(Post.status == "scheduled")).scalars().all()
        for row in rows:
            row.scheduled_at = datetime.now(UTC) - timedelta(minutes=1)
        return len(rows)


def _journal_row(run_id: str, stage_name: str) -> dict | None:
    from sqlalchemy import select

    from tgfarm.db.models import PipelineRun
    from tgfarm.db.session import session_scope

    with session_scope() as session:
        row = session.execute(
            select(PipelineRun).where(
                PipelineRun.run_id == run_id, PipelineRun.stage == stage_name
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return {
            "status": row.status,
            "items_in": row.items_in,
            "items_out": row.items_out,
            "duration_ms": row.duration_ms,
            "error": row.error,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "meta": dict(row.meta or {}),
        }


#: The three actions `tgfarm.editorial_plan` may write for one entry. A fourth
#: value means the planner and this harness disagree about what a decision is.
PLAN_ACTIONS = frozenset({"produce", "schedule", "hold"})


def _journal_by_stage(stage_name: str) -> list[dict]:
    """Every `pipeline_runs` row written under one stage name, oldest first.

    Read from the journal instead of the harness's own dict because the plan
    ticks journal under their own `plan-…` run ids: they are driven through the
    production scheduler entry points, not through the cycle's
    `pipeline._stage`, so a run-scoped lookup would not see them at all.
    """
    from sqlalchemy import select

    from tgfarm.db.models import PipelineRun
    from tgfarm.db.session import session_scope

    with session_scope() as session:
        rows = session.execute(
            select(PipelineRun).where(PipelineRun.stage == stage_name).order_by(PipelineRun.id)
        ).scalars().all()
        return [
            {
                "id": row.id,
                "run_id": row.run_id,
                "status": row.status,
                "items_in": row.items_in,
                "items_out": row.items_out,
                "error": row.error,
                "created_at": row.created_at,
                "meta": dict(row.meta or {}),
            }
            for row in rows
        ]


def _journal_stage_id(stage_name: str) -> int | None:
    """Journal sequence of a stage in this chain, None when it never ran."""
    rows = _journal_by_stage(stage_name)
    return rows[0]["id"] if rows else None


def _persisted_plan(public_id: str | None) -> dict:
    """The plan as the `editorial_plans` table holds it, with its entry rows."""
    from sqlalchemy import select

    from tgfarm.db.models import EditorialPlan, EditorialPlanEntry
    from tgfarm.db.session import session_scope

    if not public_id:
        return {}
    with session_scope() as session:
        plan = session.execute(
            select(EditorialPlan).where(EditorialPlan.public_id == public_id)
        ).scalar_one_or_none()
        if plan is None:
            return {}
        entries = session.execute(
            select(EditorialPlanEntry)
            .where(EditorialPlanEntry.plan_id == plan.id)
            .order_by(EditorialPlanEntry.priority, EditorialPlanEntry.id)
        ).scalars().all()
        return {
            "public_id": plan.public_id,
            "status": plan.status,
            "summary": dict(plan.summary or {}),
            "entries": [
                {
                    "cluster_public_id": entry.cluster_public_id,
                    "action": entry.action,
                    "channel_id": entry.channel_id,
                    "slot_at": entry.slot_at,
                    "reasons": list(entry.reasons or []),
                }
                for entry in entries
            ],
        }


def _plan_provenance_rows() -> list[dict]:
    """Every post's plan columns as they stand right now, read before the booking.

    The booking link's honest claim is narrow: it fills `plan_slot_at` and touches
    nothing else. Proving that needs the row as it was *before* the writer ran —
    reading both sides after it would compare a value with itself.
    """
    from sqlalchemy import select

    from tgfarm.db.models import Post
    from tgfarm.db.session import session_scope

    with session_scope() as session:
        rows = session.execute(select(Post).order_by(Post.id)).scalars().all()
        return [
            {
                "public_id": post.public_id,
                "status": post.status,
                "plan_entry_id": post.plan_entry_id,
                "plan_slot_at": post.plan_slot_at,
                "post_format": post.post_format,
                "format_reason": post.format_reason,
            }
            for post in rows
        ]


def _booked_slot_rows() -> list[dict]:
    """Every post the plan gave an instant to, with the instant it got shipped at.

    Read live (not from `_chain_snapshot`) because the harness backdates scheduled
    posts right after this, and the whole point of the link is the equality of
    `posts.plan_slot_at` and `posts.scheduled_at`: a row read after
    `_backdate_scheduled` can no longer prove the scheduler obeyed the booking.
    """
    from sqlalchemy import select

    from tgfarm.db.models import Post
    from tgfarm.db.session import session_scope

    with session_scope() as session:
        rows = session.execute(
            select(Post)
            .where(Post.plan_slot_at.is_not(None))
            .order_by(Post.id)
        ).scalars().all()
        return [
            {
                "public_id": post.public_id,
                "cluster_public_id": post.cluster_public_id,
                "status": post.status,
                "plan_entry_id": post.plan_entry_id,
                "plan_slot_at": post.plan_slot_at,
                "scheduled_at": post.scheduled_at,
                "post_format": post.post_format,
                "format_reason": post.format_reason,
            }
            for post in rows
        ]


def _chain_snapshot() -> dict:
    """Everything the per-link tests assert on, captured at build time.

    Taken inside the fixture so a later xfail test that runs a stage live
    cannot mutate what the main assertions were founded on.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from tgfarm.db.models import Cluster, CrosspostLog, Media, Post, RawItem
    from tgfarm.db.session import session_scope

    with session_scope() as session:
        posts = session.execute(
            select(Post).options(selectinload(Post.channel))
        ).scalars().all()
        clusters = session.execute(select(Cluster)).scalars().all()
        logs = session.execute(select(CrosspostLog)).scalars().all()
        media_rows = session.execute(select(Media)).scalars().all()
        items = session.execute(select(RawItem)).scalars().all()
        return {
            "posts": [
                {
                    "public_id": p.public_id,
                    "status": p.status,
                    "channel_id": p.channel_id,
                    "platform": (p.channel.platform if p.channel else "telegram"),
                    "cluster_public_id": p.cluster_public_id,
                    "sources": list(p.sources or []),
                    "claims": list(p.claims or []),
                    "quality": dict(p.quality or {}),
                    "reject_reason": p.reject_reason,
                    "rewrites_used": int(p.rewrites_used or 0),
                    "attempts": int(p.attempts or 0),
                    "scheduled_at": p.scheduled_at,
                    "published_at": p.published_at,
                    "tg_message_id": p.tg_message_id,
                    # The plan's own two columns. Without them this snapshot could
                    # only ever say "a post got a slot", never "it got the slot the
                    # plan reserved", which is the difference the booking link below
                    # is asserted on.
                    "plan_entry_id": p.plan_entry_id,
                    "plan_slot_at": p.plan_slot_at,
                    "post_format": p.post_format,
                    "format_reason": p.format_reason,
                }
                for p in posts
            ],
            "clusters": [
                {
                    "public_id": c.public_id,
                    "heat": c.heat,
                    "status": c.status,
                    "item_count": c.item_count,
                    "trend_phase": c.trend_phase,
                    "acceleration": c.acceleration,
                }
                for c in clusters
            ],
            "logs": [
                {
                    "post_public_id": lg.post_public_id,
                    "platform": lg.platform,
                    "status": lg.status,
                    "error": lg.error,
                    "attempt": lg.attempt,
                }
                for lg in logs
            ],
            "media": [
                {
                    "post_id": m.post_id,
                    "kind": m.kind,
                    "path": m.path,
                    "is_primary": bool(m.is_primary),
                    "exists": Path(m.path).exists(),
                }
                for m in media_rows
            ],
            "items": [
                {
                    "cluster_id": item.cluster_id,
                    "duplicate_of": item.duplicate_of,
                    "has_embedding": item.embedding is not None,
                    "simhash": item.simhash,
                    "url": item.url,
                }
                for item in items
            ],
        }


def _classify_terminal(snapshot: dict) -> str:
    """Name the chain's ending instead of printing 'ran ok'."""
    statuses = Counter(post["status"] for post in snapshot["posts"])
    published = [
        post
        for post in snapshot["posts"]
        if post["status"] == "published"
    ]
    journalled = [
        log for log in snapshot["logs"] if log["status"] in JOURNAL_OUTCOME_STATUSES
    ]
    if published and all(p["tg_message_id"] is not None for p in published):
        return "published_journalled"
    if statuses.get("scheduled", 0) and journalled:
        return "held"
    if statuses.get("gated", 0):
        return "gated"
    if statuses.get("rejected", 0):
        return "rejected"
    return "not_enough_evidence"


async def _build_chain(recorder: OutboundRecorder) -> dict:
    """Walk the whole chain once, journalling every stage through the
    production `pipeline._stage` wrapper, on a freshly bootstrapped empty DB."""
    from tgfarm import (
        pipeline,
        plan_execution,
        s2_trends,
        s4_generation,
        s5_quality,
        s6_visual,
        s7_publish,
        s8_analytics,
        s9_growth,
        s12_learning,
    )
    from tgfarm.core.run import to_thread
    from tgfarm.s8_analytics import portfolio

    baseline = _table_counts()
    seeded = await to_thread(_seed_chain_corpus)
    expected_seeds = EXPECTED_SEED_ITEMS

    result = pipeline.CycleResult(run_id=uuid.uuid4().hex[:10])
    journal: dict[str, dict | None] = {}

    async def stage(name: str, coro_fn) -> object:
        # The production stage wrapper: it times the stage, isolates it and
        # writes the `pipeline_runs` row with the three-state counters meta.
        value = await pipeline._stage(result, name, coro_fn)
        journal[name] = _journal_row(result.run_id, name)
        return value

    await stage("s2_trends", s2_trends.run)
    clusters = await to_thread(s2_trends.ready_clusters, 2)

    # --- the editorial plan, between the ranked topic and the draft.
    # Driven through the scheduler's own entry points, so the journal rows, the
    # flag check and the reason codes are the ones a running farm writes; the
    # harness adds no plan logic of its own. One look before drafting (what to
    # make of the ranked queue) and one after the quality gate (what to do with
    # the drafts that survived), which is the rhythm the two jobs have in
    # production: build every 6 h, apply every 10 min.
    plan_moments: list[datetime] = []

    async def plan_look(moment: datetime) -> None:
        plan_moments.append(moment)
        await plan_execution.build_plan_tick(now=moment)
        # The dry run is the judgement the operator can read before the writes
        # are allowed, and it journals nothing of its own — the verdicts are
        # asserted from the plan rows plus the apply journal below.
        await to_thread(plan_execution.evaluate_plan, now=moment)
        await plan_execution.apply_plan_tick(now=moment)

    await plan_look(datetime.now(UTC).replace(microsecond=0))

    async def _generate() -> dict:
        created: list[str] = []
        for public_id, _label, _heat, _members in clusters:
            created.extend(await s4_generation.run(public_id))
        return {"clusters": len(clusters), "drafts": len(created)}

    await stage("s4_generate", _generate)
    await stage("s5_quality", lambda: s5_quality.run())

    await plan_look(datetime.now(UTC).replace(microsecond=0))

    # --- the plan's reserved instant written on the row it reserved it for.
    # `apply_plan` deliberately holds a future slot (`slot_not_due`) and hands a
    # due one to `s7_publish.unpublish`, which stamps `scheduled_at = now`; neither
    # path ever puts the plan's own instant on `posts.plan_slot_at`, which is the
    # column `schedule_ready_posts` has obeyed since `0d8fd6f`. The offline entry
    # that closes the gap is `s4_generation.book_plan_slots` — no HTTP, no human,
    # one column of `posts` — and it is driven through the production stage
    # wrapper so the walk leaves a journal row like every other link.
    provenance_before = await to_thread(_plan_provenance_rows)
    booking = await stage("s4_plan_booking", lambda: to_thread(s4_generation.book_plan_slots))

    passed_ids = await to_thread(pipeline._passed_post_ids)
    if passed_ids:
        await stage("s6_visual", lambda: s6_visual.run(passed_ids))

    await stage("s7_publish", s7_publish.run)
    # Captured before the backdating below: this is the chain's proof that the
    # scheduler shipped the post at the instant the plan booked, not at its own.
    booked_rows = await to_thread(_booked_slot_rows)
    # Fresh slots are in the future; backdate them so one run reaches the
    # publish decision without waiting on a clock (the shipped demo's trick).
    await to_thread(_backdate_scheduled)
    await stage("s7_publish_due", lambda: s7_publish.publish_due(limit=50))

    await stage("s9_crosspost", lambda: s9_growth.run_crossposts(limit=20))
    await stage("s8_analytics", s8_analytics.run)
    await stage("s12_learning", s12_learning.run)

    portfolio_report = await to_thread(portfolio.portfolio_report, 30)
    counts = _table_counts()
    snapshot = await to_thread(_chain_snapshot)
    terminal = _classify_terminal(snapshot)

    return {
        "run_id": result.run_id,
        "baseline": baseline,
        "counts": counts,
        "seeded": seeded,
        "expected_seeds": expected_seeds,
        "journal": journal,
        "stages": dict(result.stages),
        "errors": list(result.errors),
        "ready_clusters": len(clusters),
        "passed_ids": list(passed_ids),
        "plan_moments": list(plan_moments),
        "booking": dict(booking or {}),
        "provenance_before": list(provenance_before),
        "booked_rows": list(booked_rows),
        "portfolio": portfolio_report,
        "snapshot": snapshot,
        "terminal": terminal,
        "calls_during_chain": list(recorder.calls),
    }


@pytest.fixture(scope="module")
def chain(tmp_path_factory):
    """One isolated DB + one full offline chain walk, shared by the file."""
    root = tmp_path_factory.mktemp("w17chain")
    db_path = root / "chain.db"
    media_dir = root / "media"

    watched_env = (
        "DATABASE_URL",
        "TGFARM_MEDIA_DIR",
        "TGFARM_MODE",
        # The plan's two ticks refuse to write anything while this is off, and
        # they journal nothing at all: the chain asserts on their journal rows,
        # so the switch is part of the harness's own contract, not an accident
        # of whichever `.env` sits next to the checkout.
        "PLAN_EXECUTION_ENABLED",
    )
    saved = {name: os.environ.get(name) for name in watched_env}
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    os.environ["TGFARM_MEDIA_DIR"] = str(media_dir)
    os.environ["TGFARM_MODE"] = "off"
    os.environ["PLAN_EXECUTION_ENABLED"] = "true"

    from tgfarm.db.bootstrap import ensure_bootstrapped
    from tgfarm.db.vector import get_index

    recorder = OutboundRecorder()

    try:
        recorder.install()
        _reset_runtime_caches()
        get_index().clear()
        ensure_bootstrapped()
        run = asyncio.run(_build_chain(recorder))
    except BaseException:
        recorder.uninstall()
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        _reset_runtime_caches()
        get_index().clear()
        raise

    run["recorder"] = recorder
    yield run

    recorder.uninstall()
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    _reset_runtime_caches()
    get_index().clear()


# ------------------------------------------------------------------ per-link


def test_w17chain_source_link_stores_items(chain):
    delta = chain["counts"]["raw_items"] - chain["baseline"]["raw_items"]
    assert delta >= 1, "the source link stored nothing"
    assert delta == chain["expected_seeds"], (
        f"seeder wrote {delta}, expected every outlet of every seed story "
        f"({chain['expected_seeds']})"
    )
    items = chain["snapshot"]["items"]
    assert all(item["has_embedding"] for item in items), "a source row without an embedding"
    assert all(int(item["simhash"] or 0) != 0 for item in items), "a source row without a simhash"


def test_w17chain_candidate_link_clusters_items(chain):
    delta = chain["counts"]["clusters"] - chain["baseline"]["clusters"]
    assert delta >= 1, "no cluster was created from the seeded items"
    clustered = [item for item in chain["snapshot"]["items"] if item["cluster_id"] is not None]
    assert clustered, "no raw item was assigned to a candidate cluster"
    multi = [cluster for cluster in chain["snapshot"]["clusters"] if cluster["item_count"] >= 2]
    assert multi, "every cluster holds one item — dedup/cluster collapsed to nothing"


def test_w17chain_ranked_topic_link_scores_and_phases(chain):
    ranked = [
        cluster
        for cluster in chain["snapshot"]["clusters"]
        if cluster["heat"] > 0 and cluster["status"] in {"ready", "consumed"}
    ]
    assert ranked, "no cluster was ranked (heat>0 and status ready/consumed)"
    assert any(cluster["trend_phase"] is not None for cluster in ranked), (
        "ranked topics carry no trend_phase"
    )
    assert chain["ready_clusters"] >= 1, "S2 promoted nothing that S4 may consume"


def test_w17chain_plan_is_a_stage_between_ranking_and_drafting(chain):
    """The decision link, proven from the journal and the plan table.

    Before wave 19 nothing offline called `build_editorial_plan` at all, so a
    green chain proved a running pipeline and not one editorial decision. These
    assertions read `pipeline_runs` and `editorial_plans` directly, never a dict
    the harness hands back about itself: the plan ticks are journalled under
    their own `plan-…` run ids (they are driven through the production scheduler
    entry points, not through the cycle's `pipeline._stage`), so a run-scoped
    lookup would miss them and a harness that stopped running the plan could not
    fake these rows either.
    """
    from tgfarm import plan_execution

    builds = _journal_by_stage(plan_execution.STAGE_BUILD)
    assert builds, (
        f"no pipeline_runs row for stage {plan_execution.STAGE_BUILD!r}: the offline chain "
        "never built an editorial plan, so it exercised the pipeline without making an "
        "editorial decision"
    )
    for row in builds:
        assert row["status"] == "ok", (
            f"the plan build journalled status={row['status']}: {row['error']}"
        )
        assert row["error"] is None, f"the plan build journalled an error: {row['error']}"
        assert row["created_at"] is not None, "the plan build journalled without a timestamp"
        plan = _persisted_plan(row["meta"].get("plan_id"))
        assert plan, (
            f"the plan build journalled plan_id={row['meta'].get('plan_id')!r}, which is not "
            "in editorial_plans: the journal claims a plan that was never persisted"
        )
        assert row["items_out"] == len(plan["entries"]), (
            f"the plan build journalled items_out={row['items_out']} for "
            f"{len(plan['entries'])} persisted entries"
        )

    # The stage sits where the acceptance chain puts it: after the topics were
    # ranked, before any draft was written. Journal sequence, not wall clock.
    ranked = _journal_stage_id("s2_trends")
    drafted = _journal_stage_id("s4_generate")
    assert ranked is not None and drafted is not None, "ranking or drafting left no journal row"
    assert ranked < builds[0]["id"] < drafted, (
        f"the plan stage is not between ranking and drafting: s2_trends id={ranked}, "
        f"plan build id={builds[0]['id']}, s4_generate id={drafted}"
    )


def test_w17chain_plan_entries_are_recorded_and_every_one_judged(chain):
    """Every decision the plan wrote down got an answer, and none was dropped.

    `apply_plan` counts one reason code per entry it refused to book and books
    the rest, so `sum(reasons) + booked == entries` is the arithmetic that says
    the judgement actually read the plan: `applied: 0` with an empty `reasons`
    is indistinguishable from a stage that never ran.
    """
    from tgfarm import plan_execution

    applies = _journal_by_stage(plan_execution.STAGE_APPLY)
    assert applies, (
        f"no pipeline_runs row for stage {plan_execution.STAGE_APPLY!r}: the plan was "
        "written down and never judged"
    )
    judged = 0
    for row in applies:
        assert row["status"] in {"ok", "skipped"}, f"apply journalled {row['status']}"
        plan = _persisted_plan(row["meta"].get("plan_id"))
        if not plan:
            assert row["status"] == "skipped", "a run against no plan must not read as ok"
            continue
        entries = plan["entries"]
        reasons = {str(code): int(n) for code, n in (row["meta"].get("reasons") or {}).items()}
        booked = int(row["items_out"] or 0)
        assert sum(reasons.values()) + booked == len(entries), (
            f"the plan judgement accounted for {sum(reasons.values()) + booked} of "
            f"{len(entries)} entries (reasons={reasons}, booked={booked})"
        )
        judged += len(entries)
        for entry in entries:
            assert entry["action"] in PLAN_ACTIONS, (
                f"entry {entry['cluster_public_id']} carries action {entry['action']!r}, "
                f"outside {sorted(PLAN_ACTIONS)}"
            )
            assert entry["reasons"], (
                f"entry {entry['cluster_public_id']} decided {entry['action']} with no "
                "stated reason"
            )
    assert judged, "no apply run judged a single plan entry"
    # A fresh chain has gated nothing yet when the plan is built, so its
    # produce/hold verdicts are the honest outcome — but a plan with no entry at
    # all would mean the stage judged nothing, which is what this file forbids.
    assert any(
        e["action"] in {"produce", "hold"} for row in applies for e in _entries_of(row)
    ), "the chain's plan recorded no produce/hold decision for the topics it ranked"


def _entries_of(journal_row: dict) -> list[dict]:
    """The persisted entries of the plan one apply row judged."""
    return _persisted_plan(journal_row["meta"].get("plan_id")).get("entries") or []


def test_w17chain_plan_bookings_are_slots_in_the_db_and_never_deliveries(chain):
    """What the plan says it booked is a slot the DB reads, and nothing left."""
    from tgfarm import plan_execution

    applies = _journal_by_stage(plan_execution.STAGE_APPLY)
    assert applies, "the plan was never applied, so no slot could be booked"
    booked_clusters: list[str] = []
    for row in applies:
        plan = _persisted_plan(row["meta"].get("plan_id"))
        if not plan:
            continue
        schedule_entries = [e for e in plan["entries"] if e["action"] == "schedule"]
        booked = int(row["items_out"] or 0)
        assert booked <= len(schedule_entries), (
            f"the plan booked {booked} slots from a plan holding {len(schedule_entries)} "
            "schedule decisions"
        )
        booked_clusters.extend(e["cluster_public_id"] for e in schedule_entries[:booked])
        if not booked and plan["entries"]:
            # The zero must stay explainable — the rule the shipped executor
            # follows for its own report, asserted from the journal here. A plan
            # with no entry at all is explained by its own emptiness.
            assert row["meta"].get("reasons"), (
                f"apply booked nothing and named no reason for plan {plan['public_id']}"
            )

    for public_id in booked_clusters:
        post = next(
            (p for p in chain["snapshot"]["posts"] if p["cluster_public_id"] == public_id), None
        )
        assert post is not None, f"the plan booked cluster {public_id}, which has no post"
        assert post["status"] == "scheduled", (
            f"the plan booked cluster {public_id} and its post reads {post['status']}"
        )
        assert post["scheduled_at"] is not None, f"booked post {post['public_id']} has no slot time"

    # The invariant the whole harness exists for, restated at the link that can
    # break it: an editorial decision that books a slot is not a publication.
    assert chain["counts"]["post_stats"] == chain["baseline"]["post_stats"], (
        "post_stats grew while the plan ran"
    )
    for post in chain["snapshot"]["posts"]:
        assert post["status"] != "published", (
            f"post {post['public_id']} became published around the plan stage"
        )
        assert post["published_at"] is None, (
            f"post {post['public_id']} carries a publish stamp in the offline chain"
        )
    for log in chain["snapshot"]["logs"]:
        assert log["status"] != "posted", f"a crosspost row says posted: {log}"
    assert chain["calls_during_chain"] == [], (
        f"the plan stage attempted {len(chain['calls_during_chain'])} outbound calls: "
        f"{chain['calls_during_chain'][:5]}"
    )


def test_w17chain_draft_link_records_provenance(chain):
    delta = chain["counts"]["posts"] - chain["baseline"]["posts"]
    assert delta >= 1, "no draft post row was persisted"
    seeded_urls = {item["url"] for item in chain["snapshot"]["items"]}
    drafts_with_cluster = [
        post for post in chain["snapshot"]["posts"] if post["cluster_public_id"]
    ]
    assert drafts_with_cluster, "drafts exist without a traceable cluster"
    for post in chain["snapshot"]["posts"]:
        assert post["sources"], f"post {post['public_id']} cites no source at all"
        for source in post["sources"]:
            assert source.get("url"), f"post {post['public_id']} has a source without a url"
        assert {source["url"] for source in post["sources"]} <= seeded_urls, (
            f"post {post['public_id']} cites urls the chain never ingested"
        )


def test_w17chain_quality_gate_produces_verdicts(chain):
    from tgfarm.s5_quality import MAX_REWRITES

    posts = chain["snapshot"]["posts"]
    verdicts = [post for post in posts if post["status"] in {"gated", "rejected", "scheduled"}]
    assert verdicts, "the quality gate left every post undecided"
    # Every post the gate touched must sit in a state the gate itself declares;
    # a draft silently reverting to some other status is a regression.
    decided = {"gated", "rejected", "scheduled", "publishing", "published", "publish_unknown"}
    escaped = [post["public_id"] for post in posts if post["status"] not in decided]
    assert not escaped, f"posts outside the gate's declared states: {escaped}"
    for post in chain["snapshot"]["posts"]:
        assert post["rewrites_used"] <= MAX_REWRITES, (
            f"post {post['public_id']} rewrote {post['rewrites_used']} times, "
            f"past the bound {MAX_REWRITES}"
        )
        if post["status"] == "rejected" or post["quality"].get("gate"):
            assert post["quality"].get("reason"), (
                f"post {post['public_id']} carries a verdict with no reason"
            )
        if post["status"] == "rejected":
            assert post["reject_reason"], f"post {post['public_id']} rejected without a named reason"
    gated = [post for post in posts if post["status"] == "gated"]
    assert gated, "nothing passed the gate — the chain would end here, check the stage payloads"


def test_w17chain_media_link_records_assets(chain):
    delta = chain["counts"]["media"] - chain["baseline"]["media"]
    assert delta >= 1, "the media stage persisted nothing"
    recorded = [asset for asset in chain["snapshot"]["media"] if asset["is_primary"]]
    assert recorded, "no primary media row"
    assert any(asset["exists"] for asset in recorded), (
        "media rows point at files that were never written"
    )


def test_w17chain_schedule_link_decides_slot_or_hold(chain):
    scheduled = [
        post
        for post in chain["snapshot"]["posts"]
        if post["status"] == "scheduled" and post["scheduled_at"] is not None
    ]
    schedule_stage = chain["stages"].get("s7_publish", {})
    gates = dict(schedule_stage.get("gates") or {})
    assert scheduled or gates, (
        "schedule decided neither a slot for anybody nor a hold reason for anybody"
    )
    assert scheduled, f"every post was held with gates={gates}; at least one slot is expected"


def test_w17chain_publish_link_journals_an_outcome(chain):
    delta = chain["counts"]["crosspost_logs"] - chain["baseline"]["crosspost_logs"]
    assert delta >= 1, "the publish decision journalled nothing"
    logs = chain["snapshot"]["logs"]
    outcomes = [log for log in logs if log["status"] in JOURNAL_OUTCOME_STATUSES]
    assert outcomes, f"journal rows exist but none is in an outcome state: {logs}"
    publish_stage = chain["stages"].get("s7_publish_due", {})
    assert publish_stage.get("published", 0) == 0, "an offline chain must not report deliveries"
    assert publish_stage.get("dry_run", 0) >= 1, (
        f"publish tick held nothing as a dry run: {publish_stage}"
    )
    for post in chain["snapshot"]["posts"]:
        assert post["status"] != "published", (
            f"post {post['public_id']} became published while offline"
        )
        assert post["attempts"] == 0, (
            f"post {post['public_id']} spent attempts on a delivery that never went out"
        )


def test_w17chain_metrics_link_attempts_and_names_absence(chain):
    stage = chain["stages"].get("s8_analytics", {})
    assert "dzen_status" in stage and "vk_status" in stage, (
        f"the analytics tick answered without naming the platform states: {sorted(stage)}"
    )
    assert stage["dzen_status"] == "skipped" and stage["vk_status"] == "skipped", (
        "offline analytics must say `skipped`, not invent measurements"
    )
    assert chain["counts"]["post_stats"] == chain["baseline"]["post_stats"], (
        "post_stats grew without a real collector — a fabricated measurement"
    )


def test_w17chain_recommendation_link_states_sample_size(chain):
    report = chain["portfolio"]
    assert "measured_posts" in report and isinstance(report["measured_posts"], int), (
        "the recommendation output does not state how many posts it measured"
    )
    assert report["measured_posts"] == 0, (
        "an offline chain measured nothing; the report must say so"
    )
    for channel in report["channels"]:
        for recommendation in channel.get("recommendations") or []:
            assert isinstance(recommendation.get("sample_size"), int), (
                "a recommendation without a sample size"
            )
            assert recommendation["actionable"] is False, (
                "an offline chain may not hand out actionable policy changes"
            )


def test_w17chain_every_link_journalled_status_and_time(chain):
    assert chain["errors"] == [], f"a chain stage errored: {chain['errors']}"
    for name in JOURNALED_STAGES:
        row = chain["journal"].get(name)
        assert row is not None, f"stage {name} left no pipeline_runs row"
        assert row["status"] == "ok", f"stage {name} journalled status={row['status']}"
        assert row["created_at"], f"stage {name} journalled without a timestamp"
        assert row["error"] is None, f"stage {name} journalled an error: {row['error']}"
        assert "counters" in row["meta"], (
            f"stage {name} journalled counters without the measured/unmeasured meta"
        )
    # Non-vacuity: the walk must actually move items somewhere, and the
    # journal must carry counters it measured (never a placeholder zero
    # presented as a measurement).
    assert any(
        chain["journal"][name]["items_in"] > 0 or chain["journal"][name]["items_out"] > 0
        for name in JOURNALED_STAGES
    ), "no stage journalled a measured non-zero counter — the chain did nothing"


def test_w17chain_terminal_state_is_an_named_outcome(chain):
    assert chain["terminal"] in ALLOWED_TERMINAL_STATES, (
        f"terminal state {chain['terminal']!r} is not one of {sorted(ALLOWED_TERMINAL_STATES)}"
    )
    # This exact seed set is deterministic: it must reach a held publish decision.
    assert chain["terminal"] == "held", (
        f"expected the chain to end `held` (scheduled posts + dry-run journal), got "
        f"{chain['terminal']!r}; stage errors: {chain['errors']}"
    )


def test_w17chain_zero_outbound_calls_during_the_full_chain(chain):
    assert chain["calls_during_chain"] == [], (
        f"the offline chain attempted {len(chain['calls_during_chain'])} outbound calls: "
        f"{chain['calls_during_chain'][:5]}"
    )
    assert chain["recorder"].calls == chain["calls_during_chain"], (
        "a test after the build appended to the recorder before the invariant check"
    )
    guarded = " ".join(chain["recorder"].guarded)
    assert "httpx" in guarded and "socket" in guarded, guarded
    for name in chain["recorder"].OPTIONAL_TRANSPORTS:
        if name in chain["recorder"].missing:
            assert name not in guarded, f"{name} reported absent yet patched"
        else:
            assert name in guarded, f"{name} is installed here and the guard must say so"


# --------------------------------------------------- links NOT drivable offline
# These xfail-strict tests are the deliverable: each names the exact entry
# point that cannot run without network. If one day an offline entry appears
# and the test passes, pytest errors on the unexpected pass and this list
# shrinks honestly.


@pytest.mark.xfail(
    strict=True,
    reason=(
        "no offline entry point for real collection: tgfarm.s1_sources.run() short-circuits "
        "before tgfarm.s1_sources.collectors.collect_detailed whenever settings.online is "
        "False; only tgfarm.demo._seed_items writes source rows without network"
    ),
)
async def test_w17chain_real_source_collection_is_unreachable_offline(chain):
    from tgfarm import s1_sources

    written = await s1_sources.run()
    assert written > 0


@pytest.mark.xfail(
    strict=True,
    reason=(
        "real delivery is unreachable by construction offline: tgfarm.s7_publish._publish_one "
        "raises before the send branch when settings.live is False, and "
        "tgfarm.platforms.PUBLISHER.send needs a live-mode Telegram bot token"
    ),
)
async def test_w17chain_real_delivery_is_unreachable_offline(chain):
    from sqlalchemy import func, select

    from tgfarm.db.models import Post
    from tgfarm.db.session import session_scope

    with session_scope() as session:
        published = int(
            session.execute(
                select(func.count(Post.id)).where(Post.status == "published")
            ).scalar_one()
        )
    assert published > 0


@pytest.mark.xfail(
    strict=True,
    reason=(
        "real metric collection is unreachable offline: tgfarm.tg_sync.collect_post_stats "
        "ends in tgfarm.tguser.fetch_post_stats, which needs an authorized Telethon session "
        "(and tgfarm.s8_analytics.dzen_web.collect_and_record needs a logged-in browser)"
    ),
)
async def test_w17chain_real_metric_collection_is_unreachable_offline(chain):
    from tgfarm import tg_sync

    stats = await tg_sync.collect_post_stats()
    assert int(stats.get("posts", 0)) > 0


@pytest.mark.xfail(
    strict=True,
    reason=(
        "a recommendation with a real sample is unreachable offline: tgfarm.s12_learning.run "
        "learns from tgfarm.s8_analytics.recent_metrics, which only sees posts measured by "
        "tgfarm.tg_sync.collect_post_stats / tgfarm.s8_analytics.dzen_web.collect_and_record"
    ),
)
async def test_w17chain_evidence_backed_lesson_is_unreachable_offline(chain):
    from tgfarm import s12_learning

    await s12_learning.run()
    conclusion_kinds = {"strategy", "tactic", "slot", "format"}
    lessons = [
        lesson
        for lesson in s12_learning.lessons()
        if lesson["kind"] in conclusion_kinds
    ]
    assert lessons and all(int(lesson["sample_size"]) > 0 for lesson in lessons)


def test_w17chain_plan_schedule_booking_reaches_the_row_offline(chain):
    """The plan's `schedule` verdict becomes the instant the post really ships at.

    This link used to be an `xfail(strict=True)`, naming `tgfarm.api.py`'s rewrite
    endpoint as the only writer that could put a bookable draft back on the queue:
    HTTP plus a human, so no self-running walk could reach a booked slot. Both
    halves of that are closed now, and this test is the proof rather than a
    description of it — the walk that produces the booking is the chain's own
    `_build_chain`, with no request, no operator and no stubbed stage:

    * `tgfarm.editorial.queue` re-lists a topic whose draft the plan itself
      ordered and nobody answered yet, so the plan's second look can say
      `schedule` about a draft that already exists;
    * `tgfarm.s4_generation.book_plan_slots` writes that entry's instant onto
      `posts.plan_slot_at`;
    * `tgfarm.s7_publish.schedule_ready_posts` obeys the booking instead of
      choosing its own slot, which is what makes `scheduled_at == plan_slot_at`.

    Asserted on the equality, on the plan's own reason text and on the zero
    outbound calls — not on a counter the harness hands itself.
    """
    from tgfarm import s4_generation
    from tgfarm.core.run import ensure_utc

    booking = chain["booking"]
    assert booking.get("considered", 0) >= 1, (
        "the chain had no open editorial order to book: the plan's own draft never "
        "came back through tgfarm.editorial.queue, so tgfarm.editorial_plan could "
        "only ever answer 'produce'"
    )
    assert booking.get("booked", 0) >= 1, (
        f"no open order reached a booked slot: states={booking.get('states')}"
    )
    booked = [d for d in booking["decisions"] if d["state"] == s4_generation.PLAN_BOOKING_BOOKED]

    for decision in booked:
        assert decision["action"] == "schedule", (
            f"a booking claims a slot the plan never reserved as `schedule`: {decision}"
        )
        assert decision["plan_public_id"], f"booked without naming the plan: {decision}"
        assert decision["slot_at"], f"`schedule` entry with no instant to carry: {decision}"
        assert decision["channel_id"] is not None, f"booked for no channel: {decision}"
        # A decision the farm cannot argue with: the plan has to say why this
        # slot, not only that it picked one. `editorial_plan._reasons` writes the
        # free-slot line for a `schedule` entry; an entry that only states facts
        # (heat, sources, age) is not a justification for a time.
        slot_reason = [part for part in decision["reasons"] if "слот" in part]
        assert slot_reason, (
            f"the plan booked {decision['slot_at']} and gave no reason naming the slot: "
            f"{decision['reasons']}"
        )

    # The booked instant must be the one the persisted plan holds, read from
    # `editorial_plan_entries` and not from the writer's own dict.
    plan_ids = {str(d["plan_public_id"]) for d in booked}
    entries: dict[tuple[str, str], dict] = {}
    for plan_id in plan_ids:
        for entry in _persisted_plan(plan_id).get("entries") or []:
            entries[(plan_id, str(entry["cluster_public_id"]))] = entry
    for decision in booked:
        entry = entries.get((str(decision["plan_public_id"]), str(decision["cluster_public_id"])))
        assert entry is not None, (
            f"the booking names plan {decision['plan_public_id']} / "
            f"{decision['cluster_public_id']}, which has no such entry in the DB"
        )
        assert entry["action"] == "schedule", (
            f"the persisted entry says {entry['action']!r} while the writer booked a slot"
        )
        assert entry["slot_at"] is not None, "the persisted `schedule` entry carries no slot"
        booked_moment = ensure_utc(datetime.fromisoformat(str(decision["slot_at"])))
        assert ensure_utc(entry["slot_at"]) == booked_moment, (
            f"the writer booked {decision['slot_at']} from an entry holding {entry['slot_at']}"
        )

    # The scheduler obeyed: a post whose `scheduled_at` IS the plan's instant.
    shipped = [
        row
        for row in chain["booked_rows"]
        if row["scheduled_at"] is not None
        and row["plan_slot_at"] is not None
        and ensure_utc(row["scheduled_at"]) == ensure_utc(row["plan_slot_at"])
    ]
    unread = [
        (row["public_id"], row["status"], row["plan_slot_at"], row["scheduled_at"])
        for row in chain["booked_rows"][:6]
    ]
    assert shipped, (
        "nothing shipped at the instant the plan booked: tgfarm.s7_publish."
        "schedule_ready_posts chose its own slot, so the booking is a note nobody "
        f"reads. Booked rows (post, status, plan slot, scheduled at): {unread}"
    )
    for row in shipped:
        assert row["status"] == "scheduled", (
            f"a post shipped at its booked instant reads {row['status']}"
        )
        assert row["cluster_public_id"], f"booked post {row['public_id']} has no topic"

    # §6, measured on the writer itself: a booking says WHEN, never WHAT. Each
    # booked row is compared with the same row as the chain read it *before* the
    # booking ran, so a format or provenance rewrite cannot hide behind "the
    # values still look plausible" — only `plan_slot_at` may differ.
    before = {row["public_id"]: row for row in chain["provenance_before"]}
    for row in chain["booked_rows"]:
        origin = before.get(row["public_id"])
        assert origin is not None, f"booked post {row['public_id']} predates the booking step"
        assert origin["plan_slot_at"] is None, (
            f"post {row['public_id']} already carried a booked slot before the "
            "booking ran, so this chain booked nothing"
        )
        assert row["post_format"] == origin["post_format"], (
            f"the booking changed post {row['public_id']}'s format "
            f"({origin['post_format']!r} -> {row['post_format']!r})"
        )
        assert row["format_reason"] == origin["format_reason"], (
            f"the booking rewrote post {row['public_id']}'s format reason"
        )
        assert row["plan_entry_id"] == origin["plan_entry_id"], (
            f"the booking repointed post {row['public_id']} away from the entry that "
            "ordered the draft"
        )

    assert any(
        int(row["items_out"] or 0) for row in _journal_by_stage("s4_plan_booking")
    ), "the booking stage journalled no items_out while it wrote slots"

    # Same invariant as every other link, restated here because this is the one
    # that got a new writer: booking a slot is a DB write, not a delivery.
    assert chain["counts"]["post_stats"] == chain["baseline"]["post_stats"], (
        "post_stats grew while the plan booked slots"
    )
    assert chain["calls_during_chain"] == [], (
        f"the booking link attempted {len(chain['calls_during_chain'])} outbound calls: "
        f"{chain['calls_during_chain'][:5]}"
    )
    # The guard covers this entry too: `book_plan_slots` reads a plan row and
    # writes a post column, and nothing else — so an outbound call here would be
    # a transport the harness already fails the chain over.
    assert chain["recorder"].calls == chain["calls_during_chain"], (
        "a call was attempted after the chain's own walk"
    )
