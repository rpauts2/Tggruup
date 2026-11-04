"""Proof that recent drafts came from a real model, not the offline composer.

Provider health flags park for an hour after a single 402/403 (and a free model
may need a one-off consent), so they lie in both directions. Draft provenance
cannot: the offline composer stamps itself. Both the Dzen publisher and the
generation stage read this one signal.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from tgfarm.db.models import Post as PostRow
from tgfarm.db.session import session_scope


def llm_recently_working(minutes: int = 90, *, limit: int = 5) -> bool:
    """Did any of the most recent drafts come from an online model?

    With no drafts at all the farm is not blocked on a guess: it returns True so
    a cold start is never mistaken for an outage.
    """
    cutoff = datetime.now(UTC) - timedelta(minutes=max(1, minutes))
    with session_scope() as session:
        rows = session.execute(
            select(PostRow.generation_provider)
            .where(PostRow.created_at >= cutoff)
            .order_by(PostRow.created_at.desc())
            .limit(max(1, limit))
        ).scalars().all()
    if not rows:
        return True
    return any(provider != "offline" for provider in rows)
