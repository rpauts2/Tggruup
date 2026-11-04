"""Drafts that cannot ship must not rot in the backlog (priority 4).

Live state (2026-11-04): 64 posts sat in ``gated`` — i.e. quality-passed and
waiting for a slot — but 26 of them were offline-composed Dzen drafts that the
publisher refuses to send (``DZEN_REQUIRES_LLM``). They never got a slot they
could use, and they inflated the "ready" number the operator sees.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import select

from tgfarm.db.models import Channel, Post
from tgfarm.db.session import session_scope


def _channel(platform: str, suffix: str) -> str:
    channel_id = f"rot_{platform}_{suffix}"
    with session_scope() as session:
        session.add(
            Channel(id=channel_id, title="Rot test", niche="test", platform=platform)
        )
        session.flush()
    return channel_id


def _post(channel_id: str, *, provider: str = "offline", age_hours: float = 100.0, status: str = "gated") -> str:
    public_id = f"rot{uuid4().hex[:10]}"
    with session_scope() as session:
        session.add(
            Post(
                public_id=public_id,
                channel_id=channel_id,
                status=status,
                hook="Хук",
                body="Тело, прошедшее гейты.",
                generation_provider=provider,
                created_at=datetime.now(UTC) - timedelta(hours=age_hours),
            )
        )
    return public_id


def _row(public_id: str) -> Post:
    with session_scope() as session:
        return session.execute(
            select(Post).where(Post.public_id == public_id)
        ).scalar_one()


def test_expire_only_old_offline_dzen_drafts(monkeypatch):
    from tgfarm import maintenance

    suffix = uuid4().hex[:6]
    dzen = _channel("dzen", suffix)
    vk = _channel("vk", suffix)
    old_offline = _post(dzen, provider="offline", age_hours=100)
    fresh_offline = _post(dzen, provider="offline", age_hours=2)
    online = _post(dzen, provider="compatible", age_hours=100)
    vk_offline = _post(vk, provider="offline", age_hours=100)

    monkeypatch.setattr(
        maintenance, "get_settings", lambda: SimpleNamespace(dzen_requires_llm=True)
    )
    changed = maintenance.expire_unpublishable_drafts(hours=72)

    assert changed >= 1
    rows = {
        pid: _row(pid)
        for pid in (old_offline, fresh_offline, online, vk_offline)
    }
    assert rows[old_offline].status == "rejected"
    assert "DZEN_REQUIRES_LLM" in (rows[old_offline].reject_reason or "")
    assert rows[fresh_offline].status == "gated"  # younger than the window
    assert rows[online].status == "gated"  # online draft could still ship
    assert rows[vk_offline].status == "gated"  # different platform


def test_expire_is_a_no_op_when_dzen_accepts_offline_text(monkeypatch):
    from tgfarm import maintenance

    channel_id = _channel("dzen", uuid4().hex[:6])
    public_id = _post(channel_id, provider="offline", age_hours=100)
    monkeypatch.setattr(
        maintenance, "get_settings", lambda: SimpleNamespace(dzen_requires_llm=False)
    )
    assert maintenance.expire_unpublishable_drafts(hours=72) == 0
    assert _row(public_id).status == "gated"


def test_scheduler_leaves_offline_dzen_drafts_without_a_slot(monkeypatch):
    from tgfarm import s7_publish

    channel_id = _channel("dzen", uuid4().hex[:6])
    public_id = _post(channel_id, provider="offline", age_hours=1, status="gated")
    monkeypatch.setattr(
        s7_publish,
        "get_settings",
        lambda: SimpleNamespace(approval_required=False, dzen_requires_llm=True),
    )
    s7_publish.schedule_ready_posts(limit=50)
    row = _row(public_id)
    assert row.status == "gated"
    assert row.scheduled_at is None
