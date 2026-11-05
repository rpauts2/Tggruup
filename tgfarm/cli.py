"""Command line interface.

    tgfarm init                 create schema + seed channels and sources
    tgfarm collect              one fetch cycle from every enabled source
    tgfarm cycle                one full pipeline cycle
    tgfarm serve                start the scheduler (long-running)
    tgfarm status               fleet state at a glance
    tgfarm trends               hot clusters right now
    tgfarm posts [--status X]   list generated posts
    tgfarm show <post_id>       print one post with its quality report
    tgfarm render <post_id>     (re)render the cover
    tgfarm video-render <id>    render an original silent 9:16 MP4 from a post
    tgfarm publish <post_id>    force-publish one post now
    tgfarm reddit-test <post_id> preview or explicitly live-test Reddit crossposting
    tgfarm channels             channel inventory + today's load
    tgfarm report [--days 7]    performance report
    tgfarm editorial            ranked queue of ready clusters
    tgfarm audit [--days 7]     content, source and AI usage audit
    tgfarm agent-benchmark --cluster ID  compare writer vs writer + strategist (never publishes)
    tgfarm agent-benchmark --report      summarize paired benchmark history
    tgfarm capability [--format]  honest capability level of every platform
    tgfarm rules-status [--format] stored destination rules state, read only
    tgfarm doctor               diagnose configuration and connectivity
    tgfarm backup-test FILE     restore a backup to a temporary DB and verify it
    tgfarm demo                 seed synthetic data and run a cycle offline
    tgfarm dzen-login           open Dzen and keep the Yandex session in a profile
    tgfarm dzen-stats           read Dzen Studio metrics into PostStat
    tgfarm repair-duplicates [--apply]  preview, then repoint stored duplicate links
    tgfarm publication-evidence [--apply]  preview, then reclassify evidence-free publications
    tgfarm requeue-failed [--apply] [--reason TEXT]  preview, then return terminal failures to the queue
    tgfarm history-import FILE  import old publication exports (preview by default)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from typing import Any

from tgfarm.core.logging import get_logger, setup_logging
from tgfarm.core.settings import get_settings
from tgfarm.db.bootstrap import ensure_bootstrapped

log = get_logger(__name__)


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def _force_utf8_stdout() -> None:
    """Windows consoles default to cp1252 and blow up on Cyrillic output."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _bootstrap() -> None:
    ensure_bootstrapped()


# --------------------------------------------------------------------- actions
def cmd_init(_args) -> int:
    from tgfarm.config import get_config

    _bootstrap()
    config = get_config()
    _print(
        {
            "ok": True,
            "channels": [p.id for p in config.personas],
            "sources": len(config.sources),
            "slot_hours": config.schedule.hours(),
        }
    )
    return 0


def cmd_collect(args) -> int:
    from tgfarm import s1_sources

    _bootstrap()

    async def _run() -> int:
        if args.all:
            return await s1_sources.run_all_sources()
        return await s1_sources.run(limit=args.limit)

    written = asyncio.run(_run())
    _print({"collected_new_items": written})
    return 0


def cmd_cycle(args) -> int:
    from tgfarm import pipeline

    async def _run() -> int:
        await pipeline.bootstrap()
        result = await pipeline._guarded_cycle(
            collect=not args.no_collect,
            max_clusters=args.max_clusters,
        )
        if result is None:
            _print({
                "error": "cycle skipped, timed out, or failed",
                "health": pipeline.cycle_health(),
            })
            return 2
        _print(result.as_dict())
        return 0 if not result.errors else 2

    return asyncio.run(_run())


def cmd_serve(args) -> int:
    import uvicorn

    from tgfarm.api import create_app
    from tgfarm.core.apiauth import ensure_token
    from tgfarm.core.logging import get_logger

    log = get_logger(__name__)
    settings = get_settings()

    host = args.host or settings.api_host
    port = args.port or settings.api_port

    if host not in ("127.0.0.1", "localhost", "::1"):
        # The API can force-publish. Exposing it is a deliberate act, and the
        # token is then the only thing standing between the network and that.
        log.warning(
            "binding the admin API to %s — it can publish posts. "
            "Every /api call needs the token below.",
            host,
        )

    token = ensure_token()
    print(f"панель:  http://{host}:{port}/")
    print(f"токен:   {token}")
    print("        (панель подставляет его сама; вручную — заголовок X-TGFarm-Token)")

    uvicorn.run(
        create_app(with_scheduler=not args.no_scheduler),
        host=host,
        port=port,
        log_level="info",
    )
    return 0


def cmd_desktop(args) -> int:
    """Launch the desktop application window."""
    from tgfarm.desktop import DesktopApp

    _bootstrap()
    setup_logging()
    app = DesktopApp(
        port=args.port or None,
        minimize_to_tray=not args.no_tray,
        with_scheduler=not args.no_scheduler,
    )
    return app.run()


def cmd_telegram(args) -> int:
    from tgfarm import tg_sync, tguser

    if args.telegram_command == "login":
        if not tguser.is_configured():
            print(
                "Сначала задайте TELEGRAM_API_ID, TELEGRAM_API_HASH и TELEGRAM_PHONE.\n"
                "api_id и api_hash берутся на https://my.telegram.org → API development tools.\n"
                "Сессия будет лежать в data/telegram — это по сути сам аккаунт, "
                "не копируйте этот файл."
            )
            return 1
        result = asyncio.run(tguser.login())
        _print(result)
        return 0

    if args.telegram_command == "logout":
        _print(asyncio.run(tguser.logout()))
        return 0

    if not tguser.is_configured():
        print("Не заданы TELEGRAM_API_ID / TELEGRAM_API_HASH / TELEGRAM_PHONE.")
        return 1
    if not tguser.is_authorized():
        print("Нет авторизованной сессии. Выполните: tgfarm telegram login")
        return 1

    if args.telegram_command == "status":
        _print(
            {
                "configured": True,
                "authorized": True,
                "session": str(tguser.session_path()),
                "channels": tg_sync.channel_reality(),
            }
        )
        return 0

    if args.telegram_command == "chats":
        chats = asyncio.run(tguser.list_chats(limit=args.limit))
        _print({"chats": [c.as_dict() for c in chats]})
        return 0

    if args.telegram_command == "top":
        from tgfarm.s1_sources.telegram_top import discover_top_chats

        _print(
            {
                "chats": discover_top_chats(
                    limit=args.limit,
                    min_participants=args.min_participants,
                )
            }
        )
        return 0

    if args.telegram_command == "sync":
        report = asyncio.run(tg_sync.sync_channels())
        _print(report)
        return 0 if report["ok"] else 1

    if args.telegram_command == "stats":
        _print(asyncio.run(tg_sync.collect_post_stats()))
        return 0

    return 1


def cmd_status(_args) -> int:
    from tgfarm import pipeline

    _bootstrap()
    _print(pipeline.status())
    return 0


def cmd_trends(args) -> int:
    from tgfarm import s2_trends
    
    _bootstrap()
    clusters = s2_trends.ready_clusters(min_items=args.min_items)
    _print(
        [
            {
                "id": public_id,
                "heat": round(heat, 3),
                "label": label[:140],
                "sources": len(items),
                "distinct_outlets": len({i.source_id for i in items}),
            }
            for public_id, label, heat, items in clusters[: args.limit]
        ]
    )
    return 0


def cmd_posts(args) -> int:
    from sqlalchemy import select

    from tgfarm.db.models import Post
    from tgfarm.db.session import session_scope

    _bootstrap()
    with session_scope() as session:
        stmt = select(Post)
        if args.status:
            stmt = stmt.where(Post.status == args.status)
        rows = session.execute(stmt.order_by(Post.created_at.desc()).limit(args.limit)).scalars().all()
        _print(
            [
                {
                    "id": r.public_id,
                    "channel": r.channel_id,
                    "angle": r.angle,
                    "status": r.status,
                    "chars": r.char_len,
                    "scheduled_at": r.scheduled_at,
                    "published_at": r.published_at,
                    "reject_reason": r.reject_reason,
                    "hook": r.hook[:110],
                }
                for r in rows
            ]
        )
    return 0


def cmd_show(args) -> int:
    from sqlalchemy import select

    from tgfarm.db.models import Media, Post
    from tgfarm.db.session import session_scope

    _bootstrap()
    with session_scope() as session:
        post = session.execute(
            select(Post).where(Post.public_id == args.post_id)
        ).scalar_one_or_none()
        if post is None:
            _print({"error": "not found"})
            return 1
        media = session.execute(
            select(Media).where(Media.post_id == post.id)
        ).scalars().all()
        _print(
            {
                "id": post.public_id,
                "channel": post.channel_id,
                "angle": post.angle,
                "status": post.status,
                "quality": post.quality,
                "rewrites": post.rewrites_used,
                "text": f"{post.hook}\n\n{post.body}",
                "sources": post.sources,
                "claims": post.claims,
                "hashtags": post.hashtags,
                "media": [m.path for m in media],
                "scheduled_at": post.scheduled_at,
            }
        )
    return 0


def cmd_repair(args) -> int:
    """Re-render missing covers and prune media rows whose file is gone."""
    from tgfarm.s6_visual import repair

    _bootstrap()
    _print(repair(args.post_id or None))
    return 0


def cmd_render(args) -> int:
    from tgfarm.config import get_config
    from tgfarm.s5_quality import load_draft
    from tgfarm.s6_visual import persist_media, render_for_post

    _bootstrap()
    loaded = load_draft(args.post_id)
    if loaded is None:
        _print({"error": "post not found or channel unknown"})
        return 1
    draft, channel_id = loaded
    persona = get_config().persona(channel_id)
    asset = render_for_post(draft, persona)
    persist_media(args.post_id, asset)
    _print(asset.model_dump())
    return 0


def cmd_video_render(args) -> int:
    """Render an original silent vertical MP4 from a saved post."""
    from tgfarm.s11_video.renderer import render_post_short

    _bootstrap()
    artifact = render_post_short(
        args.post_id,
        force=bool(args.force),
        output_path=args.output or None,
    )
    _print(artifact.as_dict())
    return 0


def cmd_publish(args) -> int:
    from sqlalchemy import select

    from tgfarm import s7_publish
    from tgfarm.db.models import Post
    from tgfarm.db.session import session_scope

    _bootstrap()
    with session_scope() as session:
        post = session.execute(
            select(Post).where(Post.public_id == args.post_id)
        ).scalar_one_or_none()
        if post is None:
            _print({"error": "not found"})
            return 1
        if post.status not in {"scheduled", "gated"}:
            _print({
                "error": "not publishable now",
                "post_id": args.post_id,
                "status": post.status,
                "allowed_statuses": ["scheduled", "gated"],
            })
            return 2

    # Use the publisher's conditional UPDATE; never overwrite a worker that
    # claimed this post between the status check and the forced publish.
    rescheduled = s7_publish.unpublish([args.post_id])
    if not rescheduled:
        _print({"error": "state changed; reload the queue and try again", "post_id": args.post_id})
        return 2

    result = asyncio.run(
        s7_publish.publish_due(limit=1, post_public_ids=[args.post_id])
    )
    _print(result)
    return 0 if result.get("failed", 0) == 0 else 1


def cmd_reddit_test(args) -> int:
    """Dry-run or explicitly confirmed live crosspost of one published post."""
    from sqlalchemy import select

    from tgfarm import s9_growth
    from tgfarm.db.models import Post
    from tgfarm.db.session import session_scope
    from tgfarm.platforms import is_platform_enabled

    _bootstrap()
    settings = get_settings()
    if settings.live and not args.confirm_live:
        _print({
            "error": "live mode requires explicit --confirm-live for Reddit",
            "hint": "Check subreddit rules and OAuth access before the one-post smoke test.",
        })
        return 2
    if not is_platform_enabled("reddit"):
        _print({
            "error": "Reddit is disabled in the global platform center",
            "hint": "Enable Reddit only after OAuth settings and subreddit rules are checked.",
        })
        return 2

    with session_scope() as session:
        post = session.execute(
            select(Post).where(Post.public_id == args.post_id)
        ).scalar_one_or_none()
        if post is None:
            _print({"error": "post not found", "post_id": args.post_id})
            return 1
        if post.status != "published":
            _print({
                "error": "Reddit test requires a primary post already marked published",
                "post_id": args.post_id,
                "status": post.status,
            })
            return 2
        post_pk = post.id

    results = asyncio.run(s9_growth.crosspost_post(post_pk, ["reddit"]))
    if not results:
        _print({
            "post_id": args.post_id,
            "mode": "live" if settings.live else "dry_run",
            "results": [],
            "note": (
                "No new attempt was created. Check the global switch, adapter credentials, "
                "and existing crosspost history before trying again."
            ),
        })
        return 0

    _print({
        "post_id": args.post_id,
        "mode": "live" if settings.live else "dry_run",
        "results": [result.model_dump() for result in results],
    })
    return 0 if all(result.status in {"posted", "dry_run"} for result in results) else 1


def cmd_channels(_args) -> int:
    from tgfarm import s8_analytics

    _bootstrap()
    _print(s8_analytics.channel_inventory())
    return 0


def cmd_report(args) -> int:
    from tgfarm import s8_analytics
    from tgfarm.s8_analytics.dzen_web import metrics_status

    _bootstrap()
    _print(
        {
            "fleet": s8_analytics.fleet_summary(args.days),
            "angles": s8_analytics.angle_performance(args.days),
            "best_hours": s8_analytics.slot_performance(args.days),
            "dzen": metrics_status(),
        }
    )
    return 0


def cmd_editorial(args) -> int:
    from tgfarm.editorial import queue

    _bootstrap()
    _print({"queue": queue(args.limit)})
    return 0


def cmd_audit(args) -> int:
    from tgfarm.s8_analytics.audit import content_audit

    _bootstrap()
    _print(content_audit(args.days, channel_id=args.channel or None))
    return 0


def cmd_agent_benchmark(args) -> int:
    from tgfarm.agents.benchmark import aggregate_benchmarks, run_paired_benchmark

    if args.report:
        _print(aggregate_benchmarks())
        return 0

    _bootstrap()
    result = asyncio.run(
        run_paired_benchmark(args.cluster, channel_id=args.channel or None)
    )
    _print(result)
    return 0 if result.get("status") == "completed" else 2


def cmd_maintain(args) -> int:
    """Run the maintenance pass manually; the cycle also runs it every time."""
    from tgfarm.maintenance import run_maintenance

    result = run_maintenance()
    if getattr(args, "vacuum", False):
        from tgfarm.db.session import get_engine

        with get_engine().connect() as conn:
            conn.exec_driver_sql("VACUUM")
        result["vacuum"] = True
    _print(result)
    return 0


def cmd_repair_duplicates(args) -> int:
    """Repoint stored duplicate links, preview first.

    The pass rewrites tens of thousands of history rows, so the default run only
    reports what it would change. `--apply` is the operator's consent (§80), and
    the copy in `backup/` is what makes it reversible.
    """
    from tgfarm.s2_trends.duplicate_repair import DEFAULT_BUDGET_SECONDS, repair_duplicate_links

    budget = DEFAULT_BUDGET_SECONDS if args.budget is None else float(args.budget)
    if args.apply:
        print("пишу в базу из DATABASE_URL: свежая копия должна лежать в backup/ (§80)")
    report = repair_duplicate_links(budget_seconds=budget, dry_run=not args.apply)
    _print(report.as_dict())
    if not args.apply:
        print("предпросмотр: ни одна строка не изменена; повтори с --apply")
    return 0


def cmd_capability(args) -> int:
    """Print the honest five-level capability matrix of every platform."""
    from tgfarm.platform_capability import LEVEL_RANK, capability_counts, capability_matrix

    rows = capability_matrix()
    if getattr(args, "format", "text") == "markdown":
        print("| Площадка | Уровень | Что делает TgFarm |")
        print("|---|---|---|")
        ordered = sorted(
            rows.items(),
            key=lambda item: (-LEVEL_RANK[str(item[1]["capability"])], item[0]),
        )
        for platform_id, row in ordered:
            print(f"| {platform_id} | {row['capability']} | {row['capability_note']} |")
        print("")
    _print(capability_counts())
    return 0


# -------------------------------------------------- destination rules, read side
#: What this command prints wherever the stored row holds no value, instead of an
#: empty cell, an invented hash or a `0 h` age (P34: `0` means "counted and
#: empty"; a missing snapshot is not a measured zero, and a missing timestamp is
#: not a fresh one).
NO_RULES_SNAPSHOT = "нет снимка правил"

#: How many destinations one read pass names at most — the same cap
#: :func:`tgfarm.community_policy.policy_history` gives the panel. The scan is
#: bounded and the report says when the bound cut it short.
RULES_STATUS_HISTORY_LIMIT = 200

#: The states of `tgfarm/maintenance.py::watch_destination_rules`, spelled with
#: the same words, plus the sentence *this* command can prove for each of them.
#: The step itself is never called from here: it opens the platform and records a
#: snapshot, and a read command does neither, so it is imported for its watch set
#: only. `tests/test_cli_rules_watch.py` fails if a state name here stops
#: appearing in that step's source, which is what keeps the two vocabularies one
#: vocabulary.
RULES_STATE_MEANINGS: dict[str, str] = {
    "nothing_to_watch": (
        "ни одна площадка не настроена для наблюдения: переключатели выключены либо "
        "история правил не называет ни одного назначения"
    ),
    "disabled": (
        "это решение оператора: площадка выключена в центре площадок, поэтому её "
        "правила не читаются и история не пополняется"
    ),
    "enabled_no_known_destination": (
        "решения ещё нет: наблюдение за площадкой включено и читатель правил есть, "
        "но история не называет ни одного назначения, поэтому перечитывать нечего; "
        "первое назначение появится из выбора цели или публикации"
    ),
    "watched": (
        "наблюдение включено, читатель правил есть, история называет назначения; "
        "показаны сохранённые снимки, этим прогоном ничего не перечитывалось"
    ),
    "no_rules_reader": (
        "читателя правил этой площадки в коде TgFarm ещё нет — это наша лакуна, а "
        "не отсутствие правил у площадки; назначения из истории не перечитываются"
    ),
}


def _rules_status_utc(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; an age must not depend on that quirk."""
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _rules_snapshot_destinations(*, history_limit: int) -> tuple[list[dict[str, Any]], int]:
    """Newest stored rules row per destination, and how many destinations exist.

    Returns ``(rows, destinations_total)``. The rows are oldest-check-first, the
    order the operator reads as "most overdue", and every destination-authored
    field leaves through :func:`tgfarm.community_policy.inert_external_text` — the
    treatment ``policy_history`` gives the panel, because community rules are
    untrusted data and never instructions (§45).

    A bare connection instead of :func:`tgfarm.db.session.session_scope` on
    purpose: ``session_scope`` commits on the way out, and a read command has
    nothing to commit. `watch_destination_rules` is not reused either — it calls
    the platforms. Both are the same table and the same newest-per-destination
    shape; only the side effects are dropped.
    """
    from sqlalchemy import func, select

    from tgfarm.community_policy import TARGET_ID_LIMIT, TARGET_NAME_LIMIT, inert_external_text
    from tgfarm.db.models import CommunityPolicySnapshot as Snapshot
    from tgfarm.db.session import get_engine

    newest = (
        select(
            Snapshot.platform,
            Snapshot.target_id,
            func.max(Snapshot.id).label("newest_id"),
        )
        .group_by(Snapshot.platform, Snapshot.target_id)
        .subquery()
    )
    cap = max(1, min(int(history_limit), RULES_STATUS_HISTORY_LIMIT))
    columns = select(
        Snapshot.platform,
        Snapshot.target_type,
        Snapshot.target_id,
        Snapshot.target_name,
        Snapshot.rules_hash,
        Snapshot.verdict_status,
        Snapshot.checked_at,
    ).join(newest, Snapshot.id == newest.c.newest_id)
    with get_engine().connect() as conn:
        total = int(conn.execute(select(func.count()).select_from(newest)).scalar_one() or 0)
        rows = conn.execute(
            columns.order_by(Snapshot.checked_at.asc(), Snapshot.id.asc()).limit(cap)
        ).all()
        return (
            [
                {
                    "platform": inert_external_text(row.platform, 32),
                    "target_type": inert_external_text(row.target_type, 32),
                    "target_id": inert_external_text(row.target_id, TARGET_ID_LIMIT),
                    "target_name": inert_external_text(row.target_name, TARGET_NAME_LIMIT),
                    "rules_hash": inert_external_text(row.rules_hash, 64),
                    "verdict_status": inert_external_text(row.verdict_status, 16),
                    "checked_at": _rules_status_utc(row.checked_at),
                }
                for row in rows
            ],
            total,
        )


def rules_status_report(
    *,
    platform: str | None = None,
    target_id: str | None = None,
    now: datetime | None = None,
    history_limit: int = RULES_STATUS_HISTORY_LIMIT,
) -> dict[str, Any]:
    """What we already know about destination rules, without asking a platform.

    The operator's question is registry row P67's: "under which rules did we
    publish". Answering it used to need a maintenance run or a panel click,
    because the only code that walks this history is the step that also re-reads
    the platforms. So this reports the stored facts alone: per platform one of the
    states `watch_destination_rules` itself uses, per destination the stored
    `rules_hash`, the age of that snapshot and the verdict that came with it.

    What it can prove and what it can only name: `disabled`, `no_rules_reader` and
    `enabled_no_known_destination` are read off the switches and the table,
    `nothing_to_watch` is their summary, and `watched` is reported with the same
    three conditions the maintenance step checks before it reads anything (the
    watch predicate `maintenance.rules_watch_switch`, a reader that exists, history
    that names destinations). It is never a claim
    that a re-read happened — no counters of a pass are printed here at all, and
    whether a Reddit reader can actually be asked (OAuth) is a question for the
    pass that would use it.
    """
    from tgfarm.maintenance import RULES_WATCH_PLATFORMS, rules_watch_switch

    current = _rules_status_utc(now) or datetime.now(UTC)
    rows, destinations_total = _rules_snapshot_destinations(history_limit=history_limit)
    scoped = [
        row
        for row in rows
        if (not platform or row["platform"] == str(platform))
        and (not target_id or row["target_id"] == str(target_id))
    ]
    # Three different numbers, because they answer three different questions: how
    # many destinations the table holds, how many this bounded scan named, and how
    # many are left once the operator's own filter is applied.
    scanned = len(rows)
    watch_set = set(RULES_WATCH_PLATFORMS)
    named = {str(row["platform"]) for row in scoped}
    universe = {str(platform)} if platform else (watch_set | named)

    by_platform: dict[str, list[dict[str, Any]]] = {}
    for row in scoped:
        by_platform.setdefault(row["platform"], []).append(row)

    entries: list[dict[str, Any]] = []
    for name in sorted(universe):
        has_reader = name in watch_set
        known = by_platform.get(name, [])
        # The step's own predicate, called rather than re-derived: `is_platform_enabled`
        # is False for a draft-only destination and has no `enabled.*` key for it, so
        # reading the switch alone here printed vc_ru as `disabled` and named a switch
        # the platform centre does not have.
        watch, switch_name = rules_watch_switch(name) if has_reader else (False, "")
        if not has_reader:
            # The history names it and no reader exists: our gap, said as ours,
            # whatever the switch says — a switch for a destination we cannot read
            # is not the interesting fact.
            state = "no_rules_reader"
        elif not watch:
            state = "disabled"
        elif not known:
            state = "enabled_no_known_destination"
        else:
            state = "watched"
        entry: dict[str, Any] = {
            "platform": name,
            "state": state,
            "rules_reader": has_reader,
            "destinations": len(known),
            "reason": RULES_STATE_MEANINGS[state],
            "known": [_rules_destination_view(row, current=current) for row in known],
        }
        if switch_name:
            # The asymmetry maintenance keeps: a disabled platform carries a
            # switch and no counters, a readerless one carries neither. A draft-only
            # destination is watched on the strength of its editor and has no switch
            # at all, so it carries neither key rather than a `None`.
            entry["switch"] = switch_name
            entry["enabled"] = bool(watch)
        entries.append(entry)

    blocked = [
        f"{entry['platform']}:{entry['state']}"
        for entry in entries
        if entry["state"] not in {"disabled", "watched", "enabled_no_known_destination"}
    ]
    watched = [entry for entry in entries if entry["state"] == "watched"]
    detail = RULES_STATE_MEANINGS["nothing_to_watch" if not watched else "watched"]
    if platform or target_id:
        detail += (
            f" (область запроса: platform={platform or '-'} target={target_id or '-'}; "
            "состояния вне неё не показаны)"
        )
    return {
        "read_only": True,
        "source": "community_policy_snapshots",
        "generated_at": current.isoformat(),
        "state": "nothing_to_watch" if not watched else "watched",
        "detail": detail,
        "destinations_stored": int(destinations_total),
        "destinations_scanned": int(scanned),
        "destinations_in_scope": len(scoped),
        "truncated": bool(destinations_total > scanned),
        "scope": {"platform": str(platform or ""), "target_id": str(target_id or "")},
        "history_limit": max(1, min(int(history_limit), RULES_STATUS_HISTORY_LIMIT)),
        "blocked": blocked,
        "platforms": entries,
    }


def _rules_destination_view(row: dict[str, Any], *, current: datetime) -> dict[str, Any]:
    """One destination rendered from its stored row: hash, age, verdict, nothing else."""
    stored = row["checked_at"]
    age_hours = (
        round(max(0.0, (current - stored).total_seconds() / 3600.0), 2) if stored else None
    )
    return {
        "target_id": row["target_id"],
        "target_name": row["target_name"],
        "target_type": row["target_type"],
        "rules_hash": row["rules_hash"],
        "verdict_status": row["verdict_status"],
        "checked_at": stored.isoformat() if stored else "",
        "age_hours": age_hours,
        "age_text": NO_RULES_SNAPSHOT if age_hours is None else f"{age_hours:.1f} ч",
        "label": (
            f"{row['target_name']} ({row['target_id']})"
            if row["target_name"] and row["target_name"] != row["target_id"]
            else (row["target_id"] or NO_RULES_SNAPSHOT)
        ),
    }


def render_rules_status(report: dict[str, Any], *, fmt: str = "text") -> list[str]:
    """The human half of `rules-status`: one block per platform, never an empty table."""
    if fmt == "markdown":
        lines = [
            "| Площадка | Состояние | Назначение | Хеш правил | Возраст снимка | Причина |",
            "|---|---|---|---|---|---|",
        ]
    else:
        summary = (
            f"источник: {report['source']} | назначений в истории: "
            f"{report['destinations_stored']} (посчитано) | просмотрено: "
            f"{report['destinations_scanned']}"
        )
        if report["destinations_in_scope"] != report["destinations_scanned"]:
            summary += f" | в области запроса: {report['destinations_in_scope']}"
        if report["truncated"]:
            summary += " | срезано лимитом"
        lines = [
            "читаем сохранённое: сеть не тронута, база не изменена",
            summary,
            f"всего: {report['state']} — {report['detail']}",
        ]
    for entry in report["platforms"]:
        switch = f" (переключатель {entry['switch']})" if "switch" in entry else ""
        reason = entry["reason"]
        if fmt == "markdown":
            if not entry["known"]:
                # The empty path keeps the sentinel in every value slot: no stored
                # snapshot is named as one, and no age is invented as `0 h`.
                lines.append(
                    f"| {entry['platform']} | {entry['state']} | {NO_RULES_SNAPSHOT} | "
                    f"{NO_RULES_SNAPSHOT} | {NO_RULES_SNAPSHOT} | {reason} |"
                )
                continue
            for row in entry["known"]:
                lines.append(
                    f"| {entry['platform']} | {entry['state']} | {row['label']} | "
                    f"{row['rules_hash'] or NO_RULES_SNAPSHOT} | {row['age_text']} | {reason} |"
                )
            continue
        lines.append(f"{entry['platform']}: {entry['state']}{switch}")
        if entry["known"]:
            for row in entry["known"]:
                lines.append(
                    f"  {row['label']} | правила: "
                    f"{row['rules_hash'] or NO_RULES_SNAPSHOT} | возраст снимка: "
                    f"{row['age_text']} | срез: {row['checked_at'] or NO_RULES_SNAPSHOT} | "
                    f"вердикт: {row['verdict_status'] or NO_RULES_SNAPSHOT}"
                )
            lines.append(f"  назначений с сохранённым снимком правил: {entry['destinations']}")
        else:
            lines.append(f"  назначений с сохранённым снимком правил: {NO_RULES_SNAPSHOT}")
        lines.append(f"  причина: {reason}")
    if fmt != "markdown":
        for item in report["blocked"]:
            lines.append(f"пробел в коде TgFarm: {item}")
    return lines


def cmd_rules_status(args) -> int:
    """Print the stored rules state of every destination we could publish to.

    Deliberately *not* `watch_destination_rules`: that step re-reads Reddit and
    Pikabu and appends a history row, and a read command makes no network calls
    and writes nothing. This prints what the table and the switches already say,
    in the state vocabulary that step owns, so "under which rules did we publish"
    is answerable between two maintenance passes.
    """
    report = rules_status_report(
        platform=str(getattr(args, "platform", "") or "") or None,
        target_id=str(getattr(args, "target", "") or "") or None,
        history_limit=int(getattr(args, "history_limit", RULES_STATUS_HISTORY_LIMIT)),
    )
    for line in render_rules_status(report, fmt=getattr(args, "format", "text")):
        print(line)
    _print(report)
    return 0


def cmd_publication_evidence(args) -> int:
    """Report (and optionally reclassify) publications with no remote evidence.

    Preview is the default, exactly like `repair-duplicates`: the numbers are the
    product, and rewriting history needs both the operator's `--apply` and the
    consent flag `PUBLICATION_EVIDENCE_REPAIR_APPLY`, on top of a fresh copy in
    `backup/`. Without them nothing is written, and the report still says what is
    sitting in the database claiming to be published.
    """
    from tgfarm.maintenance import publication_evidence_repair

    _bootstrap()
    report = publication_evidence_repair(apply=bool(args.apply), limit=args.limit)
    _print(report)
    if not args.apply:
        print("предпросмотр: ни одна строка не изменена; повтори с --apply")
    elif report.get("refused"):
        print(f"отказано: {report['refused']}")
        return 1
    return 0


def cmd_requeue_unsubmitted(args) -> int:
    """Requeue posts whose publication provably never left the farm."""
    from tgfarm.s7_publish import requeue_unsubmitted

    _bootstrap()
    result = requeue_unsubmitted(
        platform=args.platform,
        limit=args.limit,
        apply=bool(args.apply),
    )
    _print(result)
    return 0


def cmd_requeue_failed(args) -> int:
    """Move terminal publish failures back into the queue, a bounded pass at a time.

    The sibling of `requeue-unsubmitted` for the rows that sweep deliberately will
    not touch: `requeue_unsubmitted` closes a stale never-submitted post out with
    "Requeue manually if it is still relevant", and until now the only manual path
    was one click per row on a board that lists 25 per bucket. Preview is the
    default, like the other two writes; `--apply` additionally needs
    `PUBLISH_FAILURE_REQUEUE_APPLY`, a fresh copy in `backup/`, and `--reason`,
    because declaring week-old material still relevant is a human decision and the
    journal has to record who made it.
    """
    from tgfarm.maintenance import REQUEUE_BUDGET_MAX, requeue_failed_publishes

    _bootstrap()
    report = requeue_failed_publishes(
        apply=bool(args.apply),
        limit=args.limit,
        platform=args.platform,
        reason=args.reason,
    )
    _print(report)
    if not args.apply:
        print(
            f"предпросмотр: {report['eligible']} строк можно вернуть в очередь "
            f"(за один проход не больше {min(report['limit'], REQUEUE_BUDGET_MAX)}); "
            "ни одна строка не изменена, повтори с --apply --reason \"...\""
        )
    elif report.get("refused"):
        print(f"отказано: {report['refused']}")
        return 1
    return 0


def cmd_doctor(_args) -> int:
    from tgfarm.diagnostics import run_doctor

    report = run_doctor()
    _print(report)
    return 0 if all(row["ok"] for row in report["checks"]) else 1


def cmd_demo(args) -> int:
    from tgfarm.demo import run_demo

    result = asyncio.run(
        run_demo(
            cycles=args.cycles,
            offline=True,
            isolated=not args.configured_store,
        )
    )
    _print(result)
    return 0


def cmd_dzen_stats(args) -> int:
    from tgfarm.s8_analytics.dzen_web import collect_and_record, metrics_status

    _bootstrap()
    result = asyncio.run(collect_and_record(force=not args.respect_interval))
    _print({"result": result, "status": metrics_status()})
    return 0


def cmd_dzen_login(args) -> int:
    from tgfarm.dzen_web import DzenWebError, DzenWebPublisher

    publisher = DzenWebPublisher()
    print("Открываю Дзен. Войдите в Яндекс в этом окне; окно закроется само.")
    try:
        result = asyncio.run(publisher.login(timeout_s=args.timeout))
    except (DzenWebError, KeyboardInterrupt) as exc:
        print(f"Не удалось сохранить сессию: {exc}")
        return 1
    _print(result)
    return 0


def _prepare_history_import(args) -> tuple[bool, dict | None]:
    """Require a verified SQLite backup before any write to imported history."""
    if not getattr(args, "apply", False):
        return True, None

    if getattr(args, "skip_preimport_backup", False):
        return True, {
            "verified": False,
            "skipped": True,
            "reason": "operator explicitly skipped the automatic backup; verify an external backup",
        }

    from tgfarm import pipeline

    backup = asyncio.run(pipeline.backup_job())
    if not backup.get("verified"):
        return False, backup
    return True, backup


def cmd_backup(_args) -> int:
    """Create and verify a SQLite backup now."""
    from tgfarm import pipeline

    _bootstrap()
    report = asyncio.run(pipeline.backup_job())
    _print(report)
    return 0 if report.get("verified") else 2


def cmd_backup_test(args) -> int:
    """Restore a snapshot into a disposable SQLite database and verify it."""
    from tgfarm.db.recovery import verify_sqlite_backup_restore

    report = verify_sqlite_backup_restore(args.path)
    _print(report)
    return 0 if report.get("verified") else 2


def cmd_history_import(args) -> int:
    """Import a local platform export into the archive, without publishing."""
    from tgfarm.history_import import import_history, load_history_export

    _bootstrap()
    ready, backup = _prepare_history_import(args)
    if not ready:
        _print({
            "ok": False,
            "error": "История не импортирована: не удалось подтвердить резервную копию.",
            "pre_import_backup": backup,
            "hint": "Для внешнего backup вручную проверьте копию БД и только затем используйте --skip-preimport-backup.",
        })
        return 2

    records = load_history_export(args.input, platform=args.platform)
    report = import_history(records, channel_id=args.channel, apply=args.apply)
    report["source_file"] = str(args.input)
    if backup is not None:
        report["pre_import_backup"] = backup
    _print(report)
    return 0


def cmd_history_import_telegram(args) -> int:
    """Import bounded history through the existing read-only Telegram session."""
    from tgfarm.history_import import fetch_telegram_history, import_history

    _bootstrap()
    ready, backup = _prepare_history_import(args)
    if not ready:
        _print({
            "ok": False,
            "error": "История не импортирована: не удалось подтвердить резервную копию.",
            "pre_import_backup": backup,
            "hint": "Для внешнего backup вручную проверьте копию БД и только затем используйте --skip-preimport-backup.",
        })
        return 2

    records = asyncio.run(fetch_telegram_history(args.chat, limit=args.limit))
    report = import_history(records, channel_id=args.channel, apply=args.apply)
    report["telegram_chat"] = args.chat
    report["requested_limit"] = args.limit
    if backup is not None:
        report["pre_import_backup"] = backup
    _print(report)
    return 0


# ---------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tgfarm", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--json", action="store_true", help="force JSON output")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create schema + seed config").set_defaults(func=cmd_init)

    p_collect = sub.add_parser("collect", help="fetch feeds once")
    p_collect.add_argument("--all", action="store_true", help="ignore per-source intervals")
    p_collect.add_argument("--limit", type=int, default=None)
    p_collect.set_defaults(func=cmd_collect)

    p_cycle = sub.add_parser("cycle", help="one full pipeline cycle")
    p_cycle.add_argument("--no-collect", action="store_true")
    p_cycle.add_argument("--max-clusters", type=int, default=4)
    p_cycle.set_defaults(func=cmd_cycle)

    p_telegram = sub.add_parser("telegram", help="read-only Telegram account session")
    p_telegram.add_argument(
        "telegram_command",
        choices=["login", "logout", "status", "chats", "top", "sync", "stats"],
    )
    p_telegram.add_argument("--limit", type=int, default=100)
    p_telegram.add_argument("--min-participants", type=int, default=1000)
    p_telegram.set_defaults(func=cmd_telegram)

    p_desktop = sub.add_parser("desktop", help="open the desktop application")
    p_desktop.add_argument("--port", type=int, default=0, help="fixed port (default: random)")
    p_desktop.add_argument("--no-tray", action="store_true", help="close instead of hiding")
    p_desktop.add_argument(
        "--no-scheduler", action="store_true", help="do not run the background pipeline"
    )
    p_desktop.set_defaults(func=cmd_desktop)

    p_serve = sub.add_parser("serve", help="run the scheduler + dashboard")
    p_serve.add_argument(
        "--host",
        default="",
        help="bind address (default 127.0.0.1; use 0.0.0.0 only deliberately)",
    )
    p_serve.add_argument(
        "--port",
        type=int,
        default=0,
        help="port (default 8000, or TGFARM_API_PORT)",
    )
    p_serve.add_argument(
        "--no-scheduler",
        action="store_true",
        help="serve the panel without the background pipeline",
    )
    p_serve.set_defaults(func=cmd_serve)
    sub.add_parser("status", help="fleet status").set_defaults(func=cmd_status)

    p_trends = sub.add_parser("trends", help="hot clusters")
    p_trends.add_argument("--limit", type=int, default=20)
    p_trends.add_argument("--min-items", type=int, default=2)
    p_trends.set_defaults(func=cmd_trends)

    p_posts = sub.add_parser("posts", help="list posts")
    p_posts.add_argument("--status", default=None)
    p_posts.add_argument("--limit", type=int, default=25)
    p_posts.set_defaults(func=cmd_posts)

    p_show = sub.add_parser("show", help="print one post")
    p_show.add_argument("post_id")
    p_show.set_defaults(func=cmd_show)

    p_repair = sub.add_parser("repair", help="restore missing covers, prune orphans")
    p_repair.add_argument("post_id", nargs="*", help="limit to specific post ids")
    p_repair.set_defaults(func=cmd_repair)

    p_maintain = sub.add_parser(
        "maintain", help="data hygiene: cluster TTL, retention, WAL checkpoint"
    )
    p_maintain.add_argument(
        "--vacuum", action="store_true", help="compact the database file (brief lock)"
    )
    p_maintain.set_defaults(func=cmd_maintain)

    p_repair_dupes = sub.add_parser(
        "repair-duplicates",
        help="repoint stored duplicate_of links to the root (preview by default)",
    )
    p_repair_dupes.add_argument(
        "--budget", type=float, default=None, help="wall-clock seconds for this pass"
    )
    p_repair_dupes.add_argument(
        "--apply",
        action="store_true",
        help="write for real; take a fresh copy of the database first",
    )
    p_repair_dupes.set_defaults(func=cmd_repair_duplicates)

    p_pub_evidence = sub.add_parser(
        "publication-evidence",
        help="posts marked published with no remote id of any kind (preview by default)",
    )
    p_pub_evidence.add_argument(
        "--limit", type=int, default=200, help="rows one apply pass may reclassify"
    )
    p_pub_evidence.add_argument(
        "--apply",
        action="store_true",
        help="move the rows to publish_unknown; needs PUBLICATION_EVIDENCE_REPAIR_APPLY "
        "and a fresh copy in backup/",
    )
    p_pub_evidence.set_defaults(func=cmd_publication_evidence)

    p_render = sub.add_parser("render", help="render a cover image")
    p_render.add_argument("post_id")
    p_render.set_defaults(func=cmd_render)

    p_video = sub.add_parser(
        "video-render",
        help="render an original silent 9:16 MP4 from a saved post (requires FFmpeg)",
    )
    p_video.add_argument("post_id", help="public ID of a saved post")
    p_video.add_argument("--output", default="", help="optional output .mp4 path")
    p_video.add_argument("--force", action="store_true", help="replace an existing output file")
    p_video.set_defaults(func=cmd_video_render)

    p_publish = sub.add_parser("publish", help="force-publish one post")
    p_publish.add_argument("post_id")
    p_publish.set_defaults(func=cmd_publish)

    p_reddit_test = sub.add_parser(
        "reddit-test", help="dry-run or explicitly confirmed live test of one Reddit crosspost"
    )
    p_reddit_test.add_argument("post_id", help="public id of an already-published farm post")
    p_reddit_test.add_argument(
        "--confirm-live",
        action="store_true",
        help="required when TGFARM_MODE=live; this can create a real Reddit submission",
    )
    p_reddit_test.set_defaults(func=cmd_reddit_test)

    sub.add_parser("channels", help="channel inventory").set_defaults(func=cmd_channels)
    p_requeue = sub.add_parser(
        "requeue-unsubmitted",
        help="requeue posts that failed before submission; park uncertain ones",
    )
    p_requeue.add_argument("--platform", default=None, help="only this platform id")
    p_requeue.add_argument("--limit", type=int, default=100)
    p_requeue.add_argument(
        "--apply",
        action="store_true",
        help="write the changes; without it the command is a dry run",
    )
    p_requeue.set_defaults(func=cmd_requeue_unsubmitted)

    p_requeue_failed = sub.add_parser(
        "requeue-failed",
        help="preview, then return terminal publish failures to the queue",
    )
    p_requeue_failed.add_argument("--platform", default=None, help="only this platform id")
    p_requeue_failed.add_argument(
        "--limit",
        type=int,
        default=50,
        help="rows one --apply pass may requeue (clamped to 200)",
    )
    p_requeue_failed.add_argument(
        "--reason",
        default="",
        help="why this material is still relevant; required for --apply, journalled",
    )
    p_requeue_failed.add_argument(
        "--apply",
        action="store_true",
        help="write for real; needs PUBLISH_FAILURE_REQUEUE_APPLY, a fresh copy in "
        "backup/ and --reason",
    )
    p_requeue_failed.set_defaults(func=cmd_requeue_failed)

    p_report = sub.add_parser("report", help="performance report")
    p_report.add_argument("--days", type=int, default=7)
    p_report.set_defaults(func=cmd_report)

    p_editorial = sub.add_parser("editorial", help="ranked queue of ready clusters")
    p_editorial.add_argument("--limit", type=int, default=25)
    p_editorial.set_defaults(func=cmd_editorial)

    p_audit = sub.add_parser("audit", help="content, source and AI usage audit")
    p_audit.add_argument("--days", type=int, default=7)
    p_audit.add_argument("--channel", default="", help="limit to one channel id")
    p_audit.set_defaults(func=cmd_audit)

    p_agent_benchmark = sub.add_parser(
        "agent-benchmark",
        help="paired, non-publishing comparison of the editorial strategist",
    )
    group_agent_benchmark = p_agent_benchmark.add_mutually_exclusive_group(required=True)
    group_agent_benchmark.add_argument("--cluster", help="public ID of one source cluster to evaluate")
    group_agent_benchmark.add_argument(
        "--report",
        action="store_true",
        help="summarize previous paired experiments without making LLM calls",
    )
    p_agent_benchmark.add_argument(
        "--channel",
        default="",
        help="optional channel/persona ID; otherwise use the first routed channel",
    )
    p_agent_benchmark.set_defaults(func=cmd_agent_benchmark)

    sub.add_parser("doctor", help="diagnose configuration").set_defaults(func=cmd_doctor)
    p_capability = sub.add_parser(
        "capability", help="honest capability level of every platform",
    )
    p_capability.add_argument(
        "--format", choices=("text", "markdown"), default="text",
        help="markdown prints the table used in docs/PLATFORM_CENTER.md",
    )
    p_capability.set_defaults(func=cmd_capability)

    p_rules = sub.add_parser(
        "rules-status",
        help="сохранённое состояние правил назначений: без сети и без записей",
    )
    p_rules.add_argument(
        "--format", choices=("text", "markdown"), default="text",
        help="markdown prints the table used in docs/PLATFORM_CENTER.md",
    )
    p_rules.add_argument("--platform", default="", help="only this platform id")
    p_rules.add_argument("--target", default="", help="only this destination id")
    p_rules.add_argument(
        "--history-limit",
        type=int,
        default=RULES_STATUS_HISTORY_LIMIT,
        help=f"destinations one read names (1..{RULES_STATUS_HISTORY_LIMIT})",
    )
    p_rules.set_defaults(func=cmd_rules_status)

    sub.add_parser("backup", help="create and verify a SQLite backup").set_defaults(func=cmd_backup)

    p_backup_test = sub.add_parser(
        "backup-test", help="test restoring a SQLite backup to a temporary database"
    )
    p_backup_test.add_argument("path", help="path to a .db backup snapshot")
    p_backup_test.set_defaults(func=cmd_backup_test)

    p_demo = sub.add_parser("demo", help="offline end-to-end demo with synthetic data")
    p_demo.add_argument("--cycles", type=int, default=1)
    p_demo.add_argument(
        "--configured-store",
        action="store_true",
        help="run against DATABASE_URL instead of a throwaway database; "
        "a repeat run on the same store has no new demo material to judge",
    )
    p_demo.set_defaults(func=cmd_demo)

    p_dzen = sub.add_parser(
        "dzen-login", help="open Dzen and save the Yandex session in a browser profile"
    )
    p_dzen.add_argument("--timeout", type=int, default=600)
    p_dzen.set_defaults(func=cmd_dzen_login)

    p_dzen_stats = sub.add_parser(
        "dzen-stats", help="read Dzen Studio metrics into PostStat"
    )
    p_dzen_stats.add_argument(
        "--respect-interval", action="store_true", help="skip if DZEN_STATS_INTERVAL_MIN has not elapsed"
    )
    p_dzen_stats.set_defaults(func=cmd_dzen_stats)

    p_history = sub.add_parser(
        "history-import", help="import a historical JSON/CSV export (preview by default)"
    )
    p_history.add_argument("input", help="path to a local .json or .csv export")
    p_history.add_argument("--channel", required=True, help="existing TgFarm channel id")
    p_history.add_argument(
        "--platform",
        choices=["auto", "telegram", "vk", "dzen", "generic"],
        default="auto",
        help="source export format/platform (default: auto)",
    )
    p_history.add_argument(
        "--apply",
        action="store_true",
        help="write deduplicated archive rows; without this flag only previews",
    )
    p_history.add_argument(
        "--skip-preimport-backup",
        action="store_true",
        help="skip the automatic backup only when an external database backup is verified",
    )
    p_history.set_defaults(func=cmd_history_import)

    p_tg_history = sub.add_parser(
        "history-import-telegram",
        help="preview/import Telegram history using the configured read-only user session",
    )
    p_tg_history.add_argument("chat", help="Telegram username, t.me link, or chat ID")
    p_tg_history.add_argument("--channel", required=True, help="existing TgFarm channel id")
    p_tg_history.add_argument(
        "--limit", type=int, default=1000, help="messages from the oldest part of history (1..5000)"
    )
    p_tg_history.add_argument(
        "--apply",
        action="store_true",
        help="write deduplicated archive rows; without this flag only previews",
    )
    p_tg_history.add_argument(
        "--skip-preimport-backup",
        action="store_true",
        help="skip the automatic backup only when an external database backup is verified",
    )
    p_tg_history.set_defaults(func=cmd_history_import_telegram)

    return parser


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdout()
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        log.exception("command failed")
        _print({"error": type(exc).__name__, "message": str(exc)[:500]})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
