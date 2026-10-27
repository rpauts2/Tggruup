"""Ad-hoc inspection helper (dev only): dump clusters and posts."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from tgfarm.db.bootstrap import ensure_bootstrapped  # noqa: E402
from tgfarm.db.models import Cluster, Post, RawItem  # noqa: E402
from tgfarm.db.session import session_scope  # noqa: E402

ensure_bootstrapped()

with session_scope() as s:
    print("=== CLUSTERS ===")
    for c in s.execute(select(Cluster).order_by(Cluster.heat.desc())).scalars():
        niche = c.niche or "-"
        label = (c.label or "")[:62]
        print(f"{c.heat:.3f}  {c.status:<9} {niche:<10} items={c.item_count:<3} {label}")

    print("\n=== POSTS ===")
    for p in s.execute(select(Post)).scalars():
        reason = p.reject_reason or ""
        print(f"{p.status:<10} {p.channel_id:<20} {p.public_id} {reason[:60]}")

    print("\n=== ITEMS BY SOURCE ===")
    rows = s.execute(
        select(RawItem.source_id, RawItem.cluster_id, RawItem.title)
    ).all()
    by_source: dict[str, int] = {}
    for source_id, _, _ in rows:
        by_source[source_id] = by_source.get(source_id, 0) + 1
    for source_id, count in sorted(by_source.items(), key=lambda kv: -kv[1]):
        print(f"  {count:>3}  {source_id}")