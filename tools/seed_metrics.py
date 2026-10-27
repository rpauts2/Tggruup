"""Populate the farm with real collected data and a realistic metric history.

Used to exercise the dashboard end to end without waiting days for organic
stats. Development and demo helper — not part of the runtime pipeline.

    python tools/seed_metrics.py
"""

from __future__ import annotations

import random
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from tgfarm.db.bootstrap import ensure_bootstrapped  # noqa: E402
from tgfarm.db.models import Channel, ChannelStat, CrosspostLog, Post, PostStat  # noqa: E402
from tgfarm.db.session import session_scope  # noqa: E402

RNG = random.Random(20261012)


def seed(weeks: int = 3) -> dict:
    """Invent plausible view/reaction history for published posts."""
    ensure_bootstrapped()
    made_stats = made_days = 0

    with session_scope() as session:
        posts = session.execute(
            select(Post).where(Post.status == "published")
        ).scalars().all()

        for post in posts:
            # scatter publication times across the measured peak hours
            day = RNG.randint(0, max(0, weeks * 7 - 1))
            hour = RNG.choices([13, 14, 15, 16, 17, 18, 19, 9, 10, 11],
                               weights=[.08, .10, .22, .18, .12, .08, .04, .08, .05, .05], k=1)[0]
            published = datetime.now(UTC) - timedelta(
                days=day, hours=(datetime.now(UTC).hour - hour) % 24
            )
            post.published_at = published

            # views decay with channel size; engagement is the interesting signal
            base_views = RNG.randint(400, 3200)
            er = RNG.choice([0.02, 0.028, 0.035, 0.041, 0.05, 0.062, 0.08])
            reactions = int(base_views * er * RNG.uniform(0.7, 0.95))
            comments = int(reactions * RNG.uniform(0.08, 0.2))
            forwards = int(reactions * RNG.uniform(0.1, 0.3))
            session.add(
                PostStat(
                    post_id=post.id,
                    views=base_views,
                    reactions=reactions,
                    comments=comments,
                    forwards=forwards,
                    collected_at=published + timedelta(hours=2),
                )
            )
            made_stats += 1

        channels = session.execute(select(Channel)).scalars().all()
        for channel in channels:
            for d in range(weeks * 7):
                day_dt = datetime.now(UTC) - timedelta(days=d)
                day = day_dt.strftime("%Y-%m-%d")
                if session.execute(
                    select(ChannelStat).where(
                        ChannelStat.channel_id == channel.id, ChannelStat.day == day
                    )
                ).scalar_one_or_none():
                    continue
                views = RNG.randint(900, 5200)
                err = RNG.uniform(0.018, 0.075)
                session.add(
                    ChannelStat(
                        channel_id=channel.id,
                        day=day,
                        subscribers=RNG.randint(400, 52000),
                        posts=RNG.randint(2, 5),
                        views_sum=views,
                        reactions_sum=int(views * err),
                        err=round(err, 4),
                        er=round(views / RNG.randint(2, 5), 1),
                    )
                )
                made_days += 1

        # a few crosspost log rows so the tab is not empty
        published_ids = [p.public_id for p in posts][:4]
        for pid in published_ids:
            for platform in ("max", "vk", "dzen", "x"):
                if RNG.random() < 0.55:
                    session.add(
                        CrosspostLog(
                            post_public_id=pid,
                            platform=platform,
                            status="dry_run",
                            remote_id=RNG.getrandbits(28) and f"{RNG.getrandbits(28):08x}",
                        )
                    )

    return {"post_stats": made_stats, "channel_days": made_days}


if __name__ == "__main__":
    print(seed())