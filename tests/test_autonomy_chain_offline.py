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

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._undo: list[tuple[object, str, object]] = []

    def _hit(self, transport: str, target: object) -> None:
        text = str(target)[:300]
        self.calls.append(f"{transport} {text}")
        raise RuntimeError(f"w17chain offline guard refused outbound {transport} call: {text}")

    def _patch(self, owner: object, name: str, value: object) -> None:
        self._undo.append((owner, name, getattr(owner, name)))
        setattr(owner, name, value)

    def install(self) -> None:
        import aiohttp
        import httpx
        import requests

        recorder = self

        def httpx_send(client_self, request, *args, **kwargs):
            recorder._hit("httpx", getattr(request, "url", request))

        async def httpx_asend(client_self, request, *args, **kwargs):
            recorder._hit("httpx.async", getattr(request, "url", request))

        def httpx_transport(transport_self, request):
            recorder._hit("httpx.transport", getattr(request, "url", request))

        async def httpx_atransport(transport_self, request):
            recorder._hit("httpx.atransport", getattr(request, "url", request))

        def requests_send(session_self, request, *args, **kwargs):
            recorder._hit("requests", getattr(request, "url", request))

        async def aiohttp_request(session_self, method, url, *args, **kwargs):
            recorder._hit("aiohttp", f"{method} {url}")

        real_connect = socket.socket.connect

        def guarded_connect(sock_self, address):
            host = address[0] if isinstance(address, tuple) else address
            if isinstance(host, str) and host not in recorder.LOCAL_HOSTS:
                recorder._hit("socket", host)
            return real_connect(sock_self, address)

        self._patch(httpx.Client, "send", httpx_send)
        self._patch(httpx.AsyncClient, "send", httpx_asend)
        self._patch(httpx.HTTPTransport, "handle_request", httpx_transport)
        self._patch(httpx.AsyncHTTPTransport, "handle_async_request", httpx_atransport)
        self._patch(requests.Session, "send", requests_send)
        self._patch(aiohttp.ClientSession, "_request", aiohttp_request)
        self._patch(socket.socket, "connect", guarded_connect)

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

    async def _generate() -> dict:
        created: list[str] = []
        for public_id, _label, _heat, _members in clusters:
            created.extend(await s4_generation.run(public_id))
        return {"clusters": len(clusters), "drafts": len(created)}

    await stage("s4_generate", _generate)
    await stage("s5_quality", lambda: s5_quality.run())

    passed_ids = await to_thread(pipeline._passed_post_ids)
    if passed_ids:
        await stage("s6_visual", lambda: s6_visual.run(passed_ids))

    await stage("s7_publish", s7_publish.run)
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

    watched_env = ("DATABASE_URL", "TGFARM_MEDIA_DIR", "TGFARM_MODE")
    saved = {name: os.environ.get(name) for name in watched_env}
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    os.environ["TGFARM_MEDIA_DIR"] = str(media_dir)
    os.environ["TGFARM_MODE"] = "off"

    from tgfarm.db.bootstrap import ensure_bootstrapped
    from tgfarm.db.vector import get_index

    recorder = OutboundRecorder()
    recorder.install()
    _reset_runtime_caches()
    get_index().clear()
    ensure_bootstrapped()

    try:
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
