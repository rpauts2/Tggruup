"""Configuration diagnostics.

Extracted from the CLI so the desktop menu and the command line report the same
things. A second copy of these checks would drift, and the first symptom would be
"the app says everything is fine" while the terminal disagrees.
"""

from __future__ import annotations

import asyncio
import json

from tgfarm.core.settings import get_settings


def _directory_has_entries(path) -> bool:
    """Return False for a missing/unreadable profile, not an uncaught exception."""
    try:
        return path.is_dir() and next(path.iterdir(), None) is not None
    except OSError:
        return False


def run_doctor() -> dict:
    """Check everything that has to be true before the farm can work."""
    from tgfarm.config import get_config
    from tgfarm.db import init_db
    from tgfarm.llm.client import get_llm, get_stats
    from tgfarm.llm.embedding import get_embedder

    settings = get_settings()
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": ok, "detail": detail})

    try:
        init_db()
        check("database", True, settings.database_url.split("://")[0])
    except Exception as exc:  # noqa: BLE001 - a broken database is a report, not a crash
        check("database", False, str(exc)[:200])

    config = get_config()
    check(
        "config",
        len(config.enabled_personas) > 0,
        f"{len(config.enabled_personas)} active / {len(config.personas)} configured channels",
    )
    check("sources", len(config.sources) > 0, f"{len(config.sources)} sources")
    check("slots", len(config.schedule.hours()) > 0, f"{len(config.schedule.hours())} hours")

    embedder = get_embedder()
    check("embedder", True, f"{embedder.name} dim={embedder.dim}")

    llm = get_llm()
    # A key in .env is not a working provider: this probe makes one real call,
    # because "configured" and "answering" are different states and only the
    # second one keeps the farm writing.
    probe = asyncio.run(llm.probe())
    check("llm", bool(probe["ok"]), str(probe["detail"]))

    if not settings.image_enabled:
        check("images", True, "disabled; Pillow templates")
    elif settings.image_provider.strip().lower() == "cloudflare":
        ready = bool(settings.cloudflare_api_token and settings.cloudflare_image_model)
        detail = settings.cloudflare_image_model
        if not settings.cloudflare_account_id:
            detail += "; account id will be discovered"
        check("images", ready, detail)
    elif settings.image_provider.strip().lower() == "pollinations":
        check("images", True, f"pollinations/{settings.image_model or 'flux'}")
    else:
        check("images", settings.images_configured, settings.image_model or "IMAGE_MODEL missing")

    if settings.telegram_bot_token:
        from tgfarm.s7_publish import PUBLISHER

        health = asyncio.run(PUBLISHER.health())
        check("telegram", bool(health.get("username")), json.dumps(health)[:200])
    else:
        check("telegram", False, "TELEGRAM_BOT_TOKEN not set — publishing is dry-run")

    from importlib.util import find_spec

    playwright_ok = find_spec("playwright") is not None
    profile = settings.dzen_profile_path()
    profile_ready = _directory_has_entries(profile)
    if not settings.live:
        check("dzen", True, "dry-run; live publishing uses the web editor")
    elif playwright_ok and profile_ready:
        check("dzen", True, f"browser profile {profile}")
    else:
        reason = "playwright not installed" if not playwright_ok else "run `tgfarm dzen-login`"
        check("dzen", False, reason)

    from tgfarm.s8_analytics.dzen_web import metrics_status

    metrics = metrics_status()
    if not metrics["enabled"]:
        check("dzen-metrics", True, "disabled by DZEN_STATS_ENABLED")
    elif not playwright_ok or not profile_ready:
        check("dzen-metrics", False, "requires Playwright and an authenticated profile")
    else:
        detail = (
            f"{metrics['snapshots']} snapshots, "
            f"{metrics['mapped_publications']} mapped, last run {metrics['last_run'] or 'never'}"
        )
        if metrics.get("last_error"):
            detail += f"; last error: {metrics['last_error']}"
        check("dzen-metrics", True, detail)

    check("mode", settings.mode in ("live", "dry", "off"), settings.mode)

    return {"checks": checks, "llm_stats": get_stats()}


def summary(report: dict) -> tuple[int, int, list[str]]:
    """(passed, total, problems) — the shape both frontends want."""
    checks = report.get("checks", [])
    problems = [f"{row['check']}: {row['detail']}" for row in checks if not row.get("ok")]
    return sum(1 for row in checks if row.get("ok")), len(checks), problems


__all__ = ["run_doctor", "summary"]