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
    tgfarm publish <post_id>    force-publish one post now
    tgfarm channels             channel inventory + today's load
    tgfarm report [--days 7]    performance report
    tgfarm editorial            ranked queue of ready clusters
    tgfarm audit [--days 7]     content, source and AI usage audit
    tgfarm doctor               diagnose configuration and connectivity
    tgfarm demo                 seed synthetic data and run a cycle offline
    tgfarm dzen-login           open Dzen and keep the Yandex session in a profile
    tgfarm dzen-stats           read Dzen Studio metrics into PostStat
    tgfarm history-import FILE  import old publication exports (preview by default)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime

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


def cmd_doctor(_args) -> int:
    from tgfarm.diagnostics import run_doctor

    report = run_doctor()
    _print(report)
    return 0 if all(row["ok"] for row in report["checks"]) else 1


def cmd_demo(args) -> int:
    from tgfarm.demo import run_demo

    result = asyncio.run(run_demo(cycles=args.cycles, offline=True))
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

    p_render = sub.add_parser("render", help="render a cover image")
    p_render.add_argument("post_id")
    p_render.set_defaults(func=cmd_render)

    p_publish = sub.add_parser("publish", help="force-publish one post")
    p_publish.add_argument("post_id")
    p_publish.set_defaults(func=cmd_publish)

    sub.add_parser("channels", help="channel inventory").set_defaults(func=cmd_channels)

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

    sub.add_parser("doctor", help="diagnose configuration").set_defaults(func=cmd_doctor)
    sub.add_parser("backup", help="create and verify a SQLite backup").set_defaults(func=cmd_backup)

    p_demo = sub.add_parser("demo", help="offline end-to-end demo with synthetic data")
    p_demo.add_argument("--cycles", type=int, default=1)
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
