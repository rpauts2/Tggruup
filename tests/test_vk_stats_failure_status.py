"""Fetch failures are not successful empty reads, and must not expose credentials."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from tgfarm.db import session as session_mod
from tgfarm.db.models import Base, Channel, CrosspostLog, Post, PostStat
from tgfarm.db.session import session_scope
from tgfarm.s8_analytics import vk_stats


@pytest.fixture(autouse=True)
def _temporary_database(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'vk.db').as_posix()}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(session_mod, "_engine", engine)
    monkeypatch.setattr(
        session_mod, "_SessionFactory", sessionmaker(bind=engine, expire_on_commit=False)
    )
    monkeypatch.setattr(
        vk_stats, "get_settings",
        lambda: SimpleNamespace(live=True, vk_user_token="stub-token", vk_group_id="123"),
    )
    yield
    engine.dispose()


def _seed_posts(count=1):
    now = datetime.now(UTC)
    with session_scope() as session:
        session.add(Channel(id="vk-honesty", title="Test", niche="tech", platform="vk"))
        for remote in range(1, count + 1):
            public_id = f"vk-honesty-{remote}"
            session.add(Post(
                public_id=public_id, channel_id="vk-honesty", status="published", published_at=now,
            ))
            session.add(CrosspostLog(
                post_public_id=public_id, platform="vk", status="posted", remote_id=str(remote),
                created_at=now - timedelta(microseconds=count - remote),
            ))


def _snapshots():
    with session_scope() as session:
        return session.scalars(select(PostStat)).all()


@pytest.mark.parametrize("error", [
    httpx.ConnectError("connection refused"),
    httpx.ReadTimeout("response timed out"),
    RuntimeError("vk wall.getById HTTP 503"),
], ids=["connection", "timeout", "http"])
def test_fetch_failure_without_saved_rows_reports_failed(monkeypatch, error):
    # The old empty branch falsely says VK returned records even when no response arrived.
    _seed_posts()

    async def fail_fetch(ids, *, token):
        raise error

    monkeypatch.setattr(vk_stats, "_fetch", fail_fetch)
    result = asyncio.run(vk_stats.collect_and_record())
    assert result["status"] == "failed"
    assert (result["saved"], result["checked"], result["planned"], result["attributable"]) == (0, 0, 1, 1)
    assert len(result["errors"]) == 1
    assert type(error).__name__ in result["reason"]
    assert "ВК вернул" not in result["reason"]
    assert _snapshots() == []


def test_fetch_errors_are_redacted_before_truncation_and_logging(monkeypatch, caplog):
    # Slicing raw exceptions exposes tokens in both the payload and warning logs.
    _seed_posts()
    secret = "NEVER-EXPOSE-THIS-" * 20

    async def fail_fetch(ids, *, token):
        raise httpx.ConnectError(f"https://example.invalid/?access_token={secret}&v=5.199")

    monkeypatch.setattr(vk_stats, "_fetch", fail_fetch)
    with caplog.at_level("WARNING"):
        result = asyncio.run(vk_stats.collect_and_record())
    blob = repr(result) + caplog.text
    assert "NEVER-EXPOSE-THIS" not in blob
    assert "[REDACTED]" in result["errors"][0]
    assert len(result["errors"][0]) <= 134  # exception name plus the existing 120-char bound
    assert _snapshots() == []


@pytest.mark.parametrize("fail_first", [True, False], ids=["failure-first", "failure-last"])
def test_partial_fetch_failure_keeps_collected_snapshots(monkeypatch, fail_first):
    _seed_posts(51)

    async def partial_fetch(ids, *, token):
        first_chunk = len(ids) == 50
        if first_chunk == fail_first:
            raise httpx.ReadTimeout("response timed out")
        remote = int(ids[0].rsplit("_", 1)[1])
        return [{
            "id": remote, "views": {"count": 120}, "likes": {"count": 3},
            "comments": {"count": 2}, "reposts": {"count": 1},
        }]

    monkeypatch.setattr(vk_stats, "_fetch", partial_fetch)
    result = asyncio.run(vk_stats.collect_and_record())
    assert result["status"] == "collected"
    assert (result["saved"], result["checked"], result["planned"], result["attributable"]) == (1, 1, 51, 51)
    assert len(result["errors"]) == 1
    assert "reason" not in result
    row, = _snapshots()
    assert (row.views, row.reactions, row.comments, row.forwards) == (120, 3, 2, 1)


@pytest.mark.parametrize("items, checked, unattributed", [
    ([], 0, 0),
    ([{"id": 999, "views": {"count": 100}, "likes": {"count": 0},
       "comments": {"count": 0}, "reposts": {"count": 0}}], 1, 1),
], ids=["empty-response", "unattributed-response"])
def test_successful_read_without_snapshots_stays_empty(monkeypatch, items, checked, unattributed):
    _seed_posts()

    async def successful_fetch(ids, *, token):
        return items

    monkeypatch.setattr(vk_stats, "_fetch", successful_fetch)
    result = asyncio.run(vk_stats.collect_and_record())
    assert result["status"] == "empty"
    assert (result["saved"], result["checked"], result["unattributed"]) == (0, checked, unattributed)
    assert result["errors"] == []
    assert "ВК вернул" in result["reason"]
    assert _snapshots() == []


def test_error_and_unattributed_response_without_snapshots_is_failed(monkeypatch):
    _seed_posts(51)

    async def partial_fetch(ids, *, token):
        if len(ids) == 50:
            return [{"id": 999, "views": {"count": 100}, "likes": {"count": 0},
                     "comments": {"count": 0}, "reposts": {"count": 0}}]
        raise httpx.ReadTimeout("response timed out")

    monkeypatch.setattr(vk_stats, "_fetch", partial_fetch)
    result = asyncio.run(vk_stats.collect_and_record())
    assert result["status"] == "failed"
    assert (result["saved"], result["checked"], result["unattributed"]) == (0, 1, 1)
    assert "ReadTimeout" in result["reason"]
    assert _snapshots() == []


def test_error_27_remains_blocked(monkeypatch):
    _seed_posts()

    async def rejected_fetch(ids, *, token):
        raise RuntimeError("vk wall.getById error 27")

    monkeypatch.setattr(vk_stats, "_fetch", rejected_fetch)
    result = asyncio.run(vk_stats.collect_and_record())
    assert result["status"] == "blocked"
    assert result["saved"] == 0
    assert "VK_USER_TOKEN" in result["reason"]
    assert _snapshots() == []


def test_error_27_after_collection_keeps_partial_counters(monkeypatch):
    _seed_posts(101)
    calls = []

    async def partial_fetch(ids, *, token):
        calls.append(ids)
        if len(calls) == 1:
            remote = int(ids[0].rsplit("_", 1)[1])
            return [
                {"id": remote, "views": {"count": 120}, "likes": {"count": 3},
                 "comments": {"count": 2}, "reposts": {"count": 1}},
                {"id": 999, "views": {"count": 100}},
            ]
        raise RuntimeError("vk wall.getById error 27 access_token=NEVER-EXPOSE-27")

    monkeypatch.setattr(vk_stats, "_fetch", partial_fetch)
    result = asyncio.run(vk_stats.collect_and_record(limit=101))
    assert result["status"] == "collected"
    assert (result["saved"], result["checked"], result["planned"], result["unattributed"]) == (1, 2, 101, 1)
    assert result["attributable"] == 101
    assert len(calls) == 2
    assert len(result["errors"]) == 1
    assert "error 27" in result["errors"][0]
    assert "[REDACTED]" in result["errors"][0]
    assert "NEVER-EXPOSE-27" not in repr(result)
    assert "VK_USER_TOKEN" in result["reason"]
    assert len(_snapshots()) == 1


def test_no_publications_stays_empty(monkeypatch):
    async def unexpected_fetch(ids, *, token):
        raise AssertionError("no publications must not trigger a request")

    monkeypatch.setattr(vk_stats, "_fetch", unexpected_fetch)
    result = asyncio.run(vk_stats.collect_and_record())
    assert result["status"] == "empty"
    assert (result["saved"], result["checked"], result["attributable"]) == (0, 0, 0)
    assert "no recent vk publications" in result["skipped"]
    assert _snapshots() == []
