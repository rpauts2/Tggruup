"""Shared fixtures: every test runs against a throwaway SQLite database and the
real YAML config, with the network and every paid credential hard-disabled and
every ordinary tunable pinned to the default the code declares."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("TGFARM_MODE", "off")
os.environ.setdefault("LLM_PROVIDERS", "offline")
os.environ.setdefault("EMBEDDING_PROVIDER", "local")

# Ordinary tunables — thresholds, intervals, budgets, window sizes, behaviour
# switches — pinned to the default the CODE declares. The credential blanking
# below only covers secrets, so a numeric setting was still being inherited from
# whichever `.env` happened to sit next to the checkout, and a test that only
# passes under one machine's `.env` is a defective test. It already bit us: at
# 98958e4 `tests/test_trends.py::test_heat_rewards_multi_source_fresh_stories`
# passed on the operator laptop (`HEAT_THRESHOLD=0.55`) and failed in CI and in
# fresh worktrees on the same commit (code default 0.68, `assert 0 == 1` at
# tests/test_trends.py:140, CI run 41920465137). Same mechanism as the credential
# blanking: environment variables win over `.env` in pydantic-settings.
# The values are read back out of `Settings`, never written here, so this list
# cannot encode an operator preference and it follows a default change in
# `tgfarm/core/settings.py` automatically.
# TGFARM_MODE / LLM_PROVIDERS / EMBEDDING_PROVIDER are deliberately absent: they
# use `setdefault` above so CI can keep choosing them.
DETERMINISTIC_TUNABLES = (
    # pipeline scoring and cadence
    "HEAT_THRESHOLD",
    "DUPLICATE_SIMILARITY",
    "SAMENESS_THRESHOLD",
    "COLLECT_INTERVAL_MIN",
    "MAX_POSTS_PER_CHANNEL_DAY",
    "SOURCE_ADAPTIVE_CADENCE",
    "SOURCE_IDLE_BACKOFF_MAX",
    "SOURCE_FAILURE_BACKOFF_MAX",
    "SOURCE_AUTO_COOLDOWN_AFTER_FAILURES",
    "SOURCE_AUTO_COOLDOWN_HOURS",
    "SOURCE_MAX_AGE_HOURS",
    "EMBEDDING_DIM",
    "FORMAT_EVIDENCE_STALE_AFTER_DAYS",
    # AI spend and gates
    "AI_POLICY",
    "AI_DAILY_BUDGET",
    "AI_RELEVANCE_THRESHOLD",
    "AI_QUALITY_THRESHOLD",
    "IMAGE_DAILY_BUDGET",
    "WEB_SEARCH_MAX_RESULTS",
    "HOLDOUT_SHARE",
    "PRIMARY_PUBLISH_MAX_ATTEMPTS",
    # supervision, alerting and retention windows
    "CYCLE_TIMEOUT_MIN",
    "CYCLE_STALE_MIN",
    "ALERT_NO_PUBLISH_HOURS",
    "QUEUE_BACKLOG_ALERT_THRESHOLD",
    "EDITORIAL_BACKLOG_ALERT_THRESHOLD",
    "ALERT_COOLDOWN_MIN",
    "ALERT_HEARTBEAT_HOURS",
    "WATCHDOG_INTERVAL_MIN",
    "WATCHDOG_RECOVERY_BASE_MIN",
    "WATCHDOG_RECOVERY_MAX_MIN",
    "PLATFORM_AUTH_MAX_AGE_HOURS",
    "STALE_CLUSTER_HOURS",
    "RETENTION_RAW_DAYS",
    "RETENTION_RUNS_DAYS",
    "BACKUP_MAX_AGE_HOURS",
    "KEEP_BACKUPS",
    "DISK_FREE_WARN_GB",
    "DISK_FREE_CRITICAL_GB",
    # publishing behaviour switches and pacing
    "TGFARM_APPROVAL_REQUIRED",
    "VK_PRIME_SHARE",
    "TELEGRAM_USER_READONLY",
    "TELEGRAM_TOP_GROUPS_ENABLED",
    "TELEGRAM_TOP_GROUPS_LIMIT",
    "TELEGRAM_TOP_GROUPS_INTERVAL_MIN",
    "TELEGRAM_TOP_GROUP_MIN_PARTICIPANTS",
    "REDDIT_API_APPROVED",
    "MAX_CHANNEL_IS_CHANNEL",
    "MAX_CHANNEL_BLOCKED",
    "DZEN_MIN_GAP_S",
    "DZEN_HUMAN_DELAY_MS",
    "DZEN_CAPTCHA_WAIT_S",
    "DZEN_PUBLISH_TIMEOUT_S",
    "DZEN_LOGIN_TIMEOUT_S",
    "DZEN_STATS_INTERVAL_MIN",
    "DZEN_STATS_TIMEOUT_S",
    "DZEN_REQUIRES_LLM",
    "DZEN_STATS_ENABLED",
    "VIDEO_MAX_MB",
    "VIDEO_MAX_DURATION_S",
    "VIDEO_ALLOW_UNKNOWN",
    "ARTICLE_IMAGE_ENABLED",
    "ARTICLE_IMAGE_MAX_MB",
    "ARTICLE_IMAGE_MAX_WIDTH",
    "ARTICLE_IMAGE_TIMEOUT_S",
    "IMAGE_TIMEOUT_S",
    "IMAGE_QUALITY",
    # image routing: IMAGE_ENABLED/VIDEO_ENABLED are already forced off below,
    # and `pollinations` is keyless, so the provider itself must not drift.
    "IMAGE_PROVIDER",
    "IMAGE_MODEL",
    "CLOUDFLARE_IMAGE_STEPS",
)


# Credentials that must never leak from the operator's .env into a test run.
# Environment variables win over .env in pydantic-settings, so blanking them is
# enough. Without this a test that touches an adapter publishes to the real
# channel — which is exactly what happened once with the VK wall.
OFFLINE_CREDENTIALS = (
    "VK_ACCESS_TOKEN",
    "VK_GROUP_ID",
    "MAX_ACCESS_TOKEN",
    "MAX_CHANNEL_ID",
    "X_USER_ID",
    "X_OAUTH_CLIENT_ID",
    "X_OAUTH_CLIENT_SECRET",
    "X_ACCESS_TOKEN",
    "X_REFRESH_TOKEN",
    "BLUESKY_HANDLE",
    "BLUESKY_APP_PASSWORD",
    "REDDIT_CLIENT_ID",
    "REDDIT_CLIENT_SECRET",
    "REDDIT_REFRESH_TOKEN",
    "REDDIT_SUBREDDIT",
    "REDDIT_USER_AGENT",
    "MASTODON_INSTANCE",
    "MASTODON_CLIENT_ID",
    "MASTODON_CLIENT_SECRET",
    "MASTODON_ACCESS_TOKEN",
    "THREADS_APP_ID",
    "THREADS_APP_SECRET",
    "THREADS_ACCESS_TOKEN",
    "THREADS_USER_ID",
    "THREADS_USERNAME",
    "LINKEDIN_CLIENT_ID",
    "LINKEDIN_CLIENT_SECRET",
    "LINKEDIN_ACCESS_TOKEN",
    "LINKEDIN_REFRESH_TOKEN",
    "LINKEDIN_MEMBER_ID",
    "LINKEDIN_MEMBER_NAME",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
    "OPENCODE_ZEN_TOKEN",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "LLM_API_KEY",
    "LONGCAT_API_KEY",
    "CLOUDFLARE_ACCOUNT_ID",
    "CLOUDFLARE_API_TOKEN",
    "TAVILY_API_KEY",
    "SERPER_API_KEY",
    "IMAGE_API_KEY",
    "IMAGE_BASE_URL",
)


def _format_default(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _pin_tunables_to_code_defaults() -> None:
    """Put every pinned tunable in the environment at the value the code declares.

    A typo or a renamed field must be an error, not a silent no-op: an unpinned
    tunable is precisely the hole this function exists to close.
    """
    from tgfarm.core.settings import Settings

    declared = {(field.alias or name): field.default for name, field in Settings.model_fields.items()}
    for alias in DETERMINISTIC_TUNABLES:
        if alias not in declared:
            raise RuntimeError(f"conftest pins {alias}, which Settings no longer declares")
        default = declared[alias]
        if default is None:
            raise RuntimeError(f"conftest pins {alias}, which has no code default to pin")
        os.environ[alias] = _format_default(default)


_pin_tunables_to_code_defaults()


def _reset_caches() -> None:
    from tgfarm.core import settings as settings_mod
    from tgfarm.db import session as session_mod
    from tgfarm.llm import client as client_mod
    from tgfarm.llm import embedding as embedding_mod
    from tgfarm.llm import images as images_mod

    settings_mod.reset_settings_cache()
    session_mod.dispose_engine()
    client_mod.reset_llm()
    embedding_mod.reset_embedder()
    images_mod._PROVIDER = None  # noqa: SLF001 - the cache is module state


@pytest.fixture(scope="session", autouse=True)
def _offline_env():
    """Force offline credentials and a deterministic image setting."""
    for name in OFFLINE_CREDENTIALS:
        os.environ[name] = ""
    os.environ["IMAGE_ENABLED"] = "false"
    os.environ["VIDEO_ENABLED"] = "false"
    _reset_caches()
    yield
    _reset_caches()


@pytest.fixture(scope="session", autouse=True)
def _no_network(monkeypatch_session=None):
    """Make a real outbound connection a test failure, not a side effect."""
    import socket

    real_connect = socket.socket.connect

    def guarded_connect(self, address):
        host = address[0] if isinstance(address, tuple) else address
        if isinstance(host, str) and host not in ("127.0.0.1", "::1", "localhost", "0.0.0.0"):
            raise RuntimeError(f"network access attempted in a test: {host}")
        return real_connect(self, address)

    socket.socket.connect = guarded_connect
    try:
        yield
    finally:
        socket.socket.connect = real_connect


@pytest.fixture(scope="session", autouse=True)
def _isolate_db(tmp_path_factory, _offline_env) -> Path:
    db_path = tmp_path_factory.mktemp("db") / "test.db"
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    os.environ["TGFARM_MEDIA_DIR"] = str(tmp_path_factory.mktemp("media"))

    _reset_caches()

    from tgfarm.db.bootstrap import ensure_bootstrapped

    ensure_bootstrapped()
    yield db_path


@pytest.fixture()
def cfg():
    from tgfarm.config import reload_config

    return reload_config()


@pytest.fixture()
def db():
    from tgfarm.db.session import session_scope

    with session_scope() as session:
        yield session


@pytest.fixture()
def embedder():
    from tgfarm.llm.embedding import get_embedder

    return get_embedder()


def make_item(title: str, text: str = "", **kwargs):
    from tgfarm.core.schemas import RawItem

    payload = {
        "source_id": kwargs.pop("source_id", "test_source"),
        "external_id": kwargs.pop("external_id", "ext-" + str(abs(hash(title)))),
        "url": kwargs.pop("url", f"https://example.invalid/{abs(hash(title))}"),
        "title": title,
        "text": text,
        "source_weight": kwargs.pop("source_weight", 0.6),
        "niche_hint": kwargs.pop("niche_hint", ["tech"]),
    }
    payload.update(kwargs)
    return RawItem(**payload)
