from datetime import datetime, UTC

from sqlalchemy import func, select

from tgfarm.db.models import PipelineRun, Post
from tgfarm.db.session import session_scope

with session_scope() as s:
    rows = s.execute(select(Post.status, func.count()).group_by(Post.status)).all()
    print("statuses:", dict(rows))
    due = s.execute(
        select(func.count()).select_from(Post).where(
            Post.status == "scheduled", Post.scheduled_at <= datetime.now(UTC)
        )
    ).scalar()
    nxt = s.execute(
        select(func.min(Post.scheduled_at)).where(Post.status == "scheduled")
    ).scalar()
    last = s.execute(
        select(func.max(Post.scheduled_at)).where(Post.status == "scheduled")
    ).scalar()
    print("scheduled due now:", due, "next:", nxt, "last:", last)
    runs = s.execute(
        select(PipelineRun.stage, PipelineRun.started_at, PipelineRun.status)
        .order_by(PipelineRun.id.desc()).limit(12)
    ).all()
    for row in runs:
        print("run:", row.stage, row.started_at, row.status)
