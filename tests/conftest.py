"""Shared fixtures: every test runs against a throwaway SQLite database and the
real YAML config, with the network and every paid credential hard-disabled."""

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

# Credentials that must never leak from the operator's .env into a test run.
# Environment variables win over .env in pydantic-settings, so blanking them is
# enough. Without this a test that touches an adapter publishes to the real
# channel — which is exactly what happened once with the VK wall.
OFFLINE_CREDENTIALS = (
    "VK_ACCESS_TOKEN",
    "VK_GROUP_ID",
    "MAX_ACCESS_TOKEN",
    "MAX_CHANNEL_ID",
    "X_BEARER_TOKEN",
    "X_USER_ID",
    "X_OAUTH_CLIENT_ID",
    "X_OAUTH_CLIENT_SECRET",
    "X_ACCESS_TOKEN",
    "X_REFRESH_TOKEN",
    "X_TOKEN_EXPIRES_AT",
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
    "THREADS_TOKEN_EXPIRES_AT",
    "THREADS_USER_ID",
    "THREADS_USERNAME",
    "LINKEDIN_CLIENT_ID",
    "LINKEDIN_CLIENT_SECRET",
    "LINKEDIN_ACCESS_TOKEN",
    "LINKEDIN_REFRESH_TOKEN",
    "LINKEDIN_TOKEN_EXPIRES_AT",
    "LINKEDIN_REFRESH_TOKEN_EXPIRES_AT",
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
