from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

from sqlalchemy import select


def test_unconfigured_crosspost_does_not_accumulate_skipped_rows(monkeypatch):
    from tgfarm.db.models import Channel, CrosspostLog, Post
    from tgfarm.db.session import session_scope
    from tgfarm.platforms import is_platform_enabled
    from tgfarm.s9_growth import crosspost_post
    from tgfarm.s9_growth.adapters import ADAPTERS

    suffix = uuid.uuid4().hex[:8]
    channel_id = f"skip_{suffix}"
    public_id = f"skippost{suffix}"
    with session_scope() as session:
        session.add(Channel(
            id=channel_id, title="skip test", niche="tech",
            platform="telegram", enabled=True,
        ))
        session.flush()
        post = Post(
            public_id=public_id,
            channel_id=channel_id,
            hook="Skip logging test",
            body="A body",
            status="published",
            published_at=datetime.now(UTC),
        )
        session.add(post)
        session.flush()
        post_id = post.id

    adapter = ADAPTERS["bluesky"]
    monkeypatch.setattr("tgfarm.platforms.is_platform_enabled", lambda _platform: True)
    monkeypatch.setattr(adapter, "configured", lambda: False)

    first = asyncio.run(crosspost_post(post_id, ["bluesky"]))
    second = asyncio.run(crosspost_post(post_id, ["bluesky"]))

    assert first[0].status == "skipped"
    assert second[0].status == "skipped"
    with session_scope() as session:
        rows = session.execute(
            select(CrosspostLog).where(
                CrosspostLog.post_public_id == public_id,
                CrosspostLog.platform == "bluesky",
            )
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == "skipped"
