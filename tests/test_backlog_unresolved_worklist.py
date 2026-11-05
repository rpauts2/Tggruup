"""The queue card must name the publications whose outcome nobody can prove.

`_backlog` answers «что происходит с очередью» and its stock table answers
«сколько ждёт оператора» — both in counts. The count of `publish_unknown` was the
one number on that card an operator could not act on: to act, one has to know
*which* post, and the only payload the overview renders is
`/api/posts?limit=40`, ordered by `created_at desc`.

Measured on the live base 2026-11-05 through a read-only connection
(`sqlite3.connect("file:…tgfarm.db?mode=ro", uri=True)`, never `get_engine()`,
which fires `PRAGMA journal_mode=WAL`): 51 rows sit in `publish_unknown`, all of
them created 2026-10-29, and their rank by `created_at desc` runs from 7794 to
9368 out of 21124 posts — so not one of the 51 reaches even the newest 500 posts,
let alone the 40 the panel asks for. The count proved they existed; the card could
not name a single one.

These tests pin the six things the fix has to keep true:

* the list is oldest-first and capped **in SQL**, with its denominator from a
  separate aggregate — a truncated list presented as the whole set is the failure;
* a read that died yields `None`/absent, never `[]`, which would read as
  «ни одной неподтверждённой публикации нет»;
* a publish error reaches the payload only through `redact_sensitive_text`;
* nothing here writes: the same snapshot twice changes no post and no operator log
  (§80 — the worklist offers the existing reconcile route, it never fires it);
* the total agrees with the other reader of the same population,
  `tgfarm/review.py::review_queue`, so the two cannot tell different stories.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, event, func, select
from sqlalchemy.exc import SQLAlchemyError

from tgfarm import api
from tgfarm.db.models import Channel, OperatorLog, Post
from tgfarm.db.session import get_engine, session_scope

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)

_seeded_channels: list[str] = []
_seeded_public_ids: list[str] = []


def _hours(delta: float) -> datetime:
    return NOW + timedelta(hours=delta)


def _channel(*, platform: str, title: str = "Unresolved test") -> str:
    channel_id = f"blu_{uuid4().hex[:10]}"
    _seeded_channels.append(channel_id)
    with session_scope() as session:
        session.add(
            Channel(id=channel_id, title=title, niche="test", platform=platform, enabled=True)
        )
    return channel_id


def _post(channel_id: str, status: str, *, created_h: float) -> str:
    public_id = f"blu{uuid4().hex[:10]}"
    _seeded_public_ids.append(public_id)
    with session_scope() as session:
        session.add(
            Post(
                public_id=public_id,
                channel_id=channel_id,
                hook="Заголовок",
                body="Тело поста.",
                status=status,
                created_at=_hours(created_h),
            )
        )
    return public_id


def _unknown(
    channel_id: str,
    *,
    created_h: float,
    hook: str = "Заголовок",
    attempts: int = 0,
    publish_error: str | None = None,
) -> str:
    public_id = f"blu{uuid4().hex[:10]}"
    _seeded_public_ids.append(public_id)
    with session_scope() as session:
        session.add(
            Post(
                public_id=public_id,
                channel_id=channel_id,
                hook=hook,
                body="Тело поста.",
                status="publish_unknown",
                created_at=_hours(created_h),
                attempts=attempts,
                publish_error=publish_error,
            )
        )
    return public_id


def _platform() -> str:
    """A platform name nobody else in the suite can be using."""
    return f"blu{uuid4().hex[:8]}"


def _measured_total() -> int:
    """The same aggregate the endpoint is supposed to run, counted by hand."""
    with session_scope() as session:
        return int(
            session.execute(
                select(func.count()).select_from(Post).where(
                    Post.status == api.BACKLOG_UNRESOLVED_STATUS
                )
            ).scalar_one()
        )


@pytest.fixture(autouse=True)
def _drop_own_rows():
    """Leave no residue: the suite shares one session database, and a
    `publish_unknown` row left behind becomes another file's evidence."""
    yield
    with session_scope() as session:
        if _seeded_public_ids:
            session.execute(delete(Post).where(Post.public_id.in_(_seeded_public_ids)))
        if _seeded_channels:
            session.execute(delete(Channel).where(Channel.id.in_(_seeded_channels)))
    _seeded_channels.clear()
    _seeded_public_ids.clear()


# ---------------------------------------------------------------- the premise
def test_the_posts_window_the_queue_renders_from_cannot_reach_an_old_unknown():
    """The gap reproduced, not asserted from prose.

    `/api/posts` is ordered `created_at desc` and the overview asks it for 40 rows
    (`tgfarm/ui/app.js`: `/api/posts?limit=40`), so once the farm has written more
    than a window of newer posts a `publish_unknown` row cannot be named from that
    payload — while the same row is exactly what the queue card counts.

    On the live base the arithmetic was worse than the minimum: 51 rows in that
    state (mode=ro, 2026-11-05), all created 2026-10-29, ranked 7794…9368 newest-
    first out of 21124 posts, so none of them sits in even the endpoint's largest
    window of 500.
    """
    platform = _platform()
    channel = _channel(platform=platform)
    public_id = _unknown(channel, created_h=-40000)
    for step in range(45):
        _post(channel, "draft", created_h=-step)

    window = api._posts(None, None, 40)
    report = api._backlog(now=NOW)

    assert len(window) == 40, "the panel asks for a window; the test must have one"
    assert public_id not in {row["id"] for row in window}, (
        "an old unknown reached the newest 40, so the window no longer hides it here"
    )
    assert public_id in {item["public_id"] for item in report["unresolved"]["items"]}, (
        "and the card that counts it still has to name it"
    )


def test_the_status_the_worklist_names_is_a_state_the_stock_counts():
    """One spelling of the fact, or the two halves of the card disagree."""
    assert api.BACKLOG_UNRESOLVED_STATUS in api.BACKLOG_WAITING
    assert isinstance(api.BACKLOG_UNRESOLVED_LIMIT, int)
    assert api.BACKLOG_UNRESOLVED_LIMIT >= 1


# --------------------------------------------------------------------- shape
def test_oldest_rows_are_named_first_and_the_rest_stay_in_the_denominator():
    platform = _platform()
    channel = _channel(platform=platform)
    ids = [_unknown(channel, created_h=-50000 + step) for step in range(4)]

    block = api._backlog(now=NOW)["unresolved"]

    assert block["total"] == _measured_total()
    assert block["shown"] == len(block["items"]) <= block["limit"]
    named = [item["public_id"] for item in block["items"]]
    # `ids[0]` carries the oldest stamp of the ladder, so its position in a list
    # sorted by age must be the smallest one.
    positions = [named.index(public_id) for public_id in ids]
    assert len(named) == len(set(named)), "one row named twice is one row missing"
    assert positions == sorted(positions), (
        f"the four seeded rows came back out of age order: {positions}"
    )
    assert block["items"][positions[0]]["age_hours"] >= 50000, (
        "the line the card puts first is the oldest of the ladder"
    )
    oldest = next(row for row in block["items"] if row["public_id"] == ids[0])
    assert oldest["platform"] == platform
    assert oldest["channel_id"] == channel
    assert oldest["channel_title"] == "Unresolved test"
    assert oldest["created_at"].startswith("20"), "an ISO stamp, not a bare epoch number"


def test_every_field_the_panel_reads_is_in_every_row():
    platform = _platform()
    channel = _channel(platform=platform)
    public_id = _unknown(channel, created_h=-41000, hook="Заголовок", attempts=3)

    item = next(
        row
        for row in api._backlog(now=NOW)["unresolved"]["items"]
        if row["public_id"] == public_id
    )

    assert set(item) == {
        "public_id",
        "hook",
        "channel_id",
        "channel_title",
        "platform",
        "created_at",
        "age_hours",
        "attempts",
        "error",
    }
    assert item["attempts"] == 3, "a real zero-vs-count matters: 3 attempts were measured"
    assert item["error"] is None, "no publish error was written: absent, not «нет ошибки»"
    assert item["platform"] == platform


def test_a_channel_row_missing_from_the_table_reports_unknown_not_telegram():
    """The card may not guess which platform an orphaned post was sent to.

    The app engine runs with `PRAGMA foreign_keys=ON`, so an ORM delete of a
    referenced channel is refused — which is the point: a dangling `channel_id`
    can only arrive as legacy damage, and a raw connection without the pragma is
    how a repair (and this test) sees one.
    """
    channel = _channel(platform=_platform())
    public_id = _unknown(channel, created_h=-42000)
    path = os.environ["DATABASE_URL"].replace("sqlite:///", "")
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("DELETE FROM channels WHERE id = ?", (channel,))
        connection.commit()
    _seeded_channels.remove(channel)

    item = next(
        row
        for row in api._backlog(now=NOW)["unresolved"]["items"]
        if row["public_id"] == public_id
    )

    assert item["platform"] == "unknown"
    assert item["channel_title"] is None, "no title to borrow from a row that is gone"


def test_the_excerpt_is_bounded_where_the_column_is_not():
    """`posts.hook` is `Text`: an imported row can carry megabytes of it."""
    platform = _platform()
    channel = _channel(platform=platform)
    public_id = _unknown(channel, created_h=-43000, hook="А" * 5000)

    item = next(
        row
        for row in api._backlog(now=NOW)["unresolved"]["items"]
        if row["public_id"] == public_id
    )

    assert len(item["hook"]) == api.BACKLOG_UNRESOLVED_EXCERPT_CHARS


# ------------------------------------------------------------------ bounded in SQL
def test_the_cap_is_a_sql_limit_and_the_count_is_a_separate_query():
    """`LIMIT` in the statement, not `[:10]` after fetching the pile.

    The same discipline as `maintenance._JOURNAL_READ_CHUNK`: a read that pulls
    every row to slice it in Python grows with the table it is supposed to protect.
    The spy records the SQL the engine actually ran, so this is not a reading of
    the source file.
    """
    statements: list[tuple[str, str]] = []

    def _spy(conn, cursor, statement, parameters, context, executemany):
        # Most statements arrive with a dict of binds; an expanded `IN (…)`
        # arrives as a plain sequence. Both are flattened to text, because what
        # this test asks is which SQL the engine really ran.
        if isinstance(parameters, dict):
            rendered = json.dumps({key: str(value) for key, value in parameters.items()})
        else:
            rendered = json.dumps([str(value) for value in (parameters or ())])
        statements.append((statement, rendered))

    engine = get_engine()
    event.listen(engine, "before_cursor_execute", _spy)
    try:
        block = api._backlog(now=NOW)["unresolved"]
    finally:
        event.remove(engine, "before_cursor_execute", _spy)

    reads = [
        (statement, params)
        for statement, params in statements
        if "FROM posts" in statement and api.BACKLOG_UNRESOLVED_STATUS in params
    ]
    assert reads, "the card must read the population at all"
    listed = [(statement, params) for statement, params in reads if "LIMIT" in statement.upper()]
    counted = [
        (statement, params)
        for statement, params in reads
        if "count(" in statement.lower() and "GROUP BY" not in statement.upper()
    ]

    assert listed, "the worklist must be bounded by the database, not sliced afterwards"
    assert str(block["limit"]) in listed[0][1], "the cap travels as the LIMIT value"
    assert counted, "the denominator must come from its own aggregate"
    assert not any("LIMIT" in statement.upper() for statement, _ in counted), (
        "a capped count would report the cap as the total"
    )


def test_the_denominator_survives_a_list_smaller_than_the_population():
    platform = _platform()
    channel = _channel(platform=platform)
    for step in range(api.BACKLOG_UNRESOLVED_LIMIT + 3):
        _unknown(channel, created_h=-44000 - step)

    block = api._backlog(now=NOW)["unresolved"]

    assert block["shown"] == block["limit"], "the list stays capped"
    assert block["total"] >= block["shown"] + 3, (
        "and still says more exist — the three beyond the cap are not nothing"
    )


# --------------------------------------------------------------------- redaction
def test_a_token_shaped_publish_error_never_reaches_the_panel():
    """§44: an operator token must not ride a dashboard.

    `publish_error` is platform text. The redactor the ledger rows already use is
    the only thing between it and this payload.
    """
    platform = _platform()
    channel = _channel(platform=platform)
    secret = f"op{uuid4().hex[:12]}"
    public_id = _unknown(
        channel,
        created_h=-45000,
        publish_error=f'HTTP 401 {{"access_token": "{secret}"}} ' + ("x" * 400),
    )

    report = api._backlog(now=NOW)
    item = next(
        row for row in report["unresolved"]["items"] if row["public_id"] == public_id
    )

    assert secret not in item["error"], f"the token survived redaction: {item['error']!r}"
    assert secret not in json.dumps(report, ensure_ascii=False, default=str)
    assert len(item["error"]) <= 200
    assert "HTTP 401" in item["error"], "the reason stays readable; only the credential goes"


# ------------------------------------------------------------------------ honesty
def test_a_failed_read_names_nothing_and_counts_nothing(monkeypatch):
    """`None` for a read that died; `[]`/`0` would be a verdict nobody measured."""
    boom = SQLAlchemyError("no such column: posts.publish_error")

    class _Broken:
        def __enter__(self):
            raise boom

        def __exit__(self, *excinfo):
            return False

    monkeypatch.setattr(api, "session_scope", lambda: _Broken())

    report = api._backlog(now=NOW)

    assert report["error"], "the card still has to say the read failed"
    assert report["unresolved"] is None, "absent, not an empty list that reads as «всё чисто»"
    assert report["platforms"] == []


def test_a_measured_empty_population_is_a_real_zero(tmp_path, monkeypatch):
    """The other half of the same rule: 0 is allowed once something was counted.

    The session database is shared and another file may hold a row in this state,
    so this runs against its own empty store — `api.session_scope` is pointed at a
    fresh SQLite file with the two tables the card reads, and nothing in the
    shared database is deleted.
    """
    from contextlib import contextmanager

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from tgfarm.db.models import Base

    engine = create_engine(f"sqlite:///{(tmp_path / 'empty.db').as_posix()}")
    Base.metadata.create_all(engine, tables=[Channel.__table__, Post.__table__])
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    @contextmanager
    def _scope():
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr(api, "session_scope", _scope)
    try:
        block = api._backlog(now=NOW)["unresolved"]
    finally:
        engine.dispose()

    assert block is not None, "the read answered, so the block must be there"
    assert block["total"] == 0, "a measured nothing is 0, not None and not «не прочитано»"
    assert block["shown"] == 0
    assert block["items"] == []


# ---------------------------------------------------------------------------- §80
def test_reading_the_worklist_writes_nothing():
    """A card that offers a reconcile action must not perform one by itself."""
    platform = _platform()
    channel = _channel(platform=platform)
    for step in range(3):
        _unknown(channel, created_h=-46000 - step)

    with session_scope() as session:
        before = dict(
            session.execute(select(Post.status, func.count()).group_by(Post.status)).all()
        )
        logs_before = int(session.execute(select(func.count()).select_from(OperatorLog)).scalar_one())

    first = api._backlog(now=NOW)["unresolved"]
    second = api._backlog(now=NOW)["unresolved"]

    with session_scope() as session:
        after = dict(
            session.execute(select(Post.status, func.count()).group_by(Post.status)).all()
        )
        logs_after = int(session.execute(select(func.count()).select_from(OperatorLog)).scalar_one())

    assert before == after, "the read moved a post"
    assert logs_before == logs_after, "the read journalled an operator action"
    assert [item["public_id"] for item in first["items"]] == [
        item["public_id"] for item in second["items"]
    ], "two snapshots of the same queue must not shuffle the worklist"


def test_the_endpoint_serves_the_worklist_without_losing_the_counts():
    from fastapi.testclient import TestClient

    from tgfarm.api import create_app
    from tgfarm.core.apiauth import HEADER, ensure_token

    with TestClient(create_app(with_scheduler=False), base_url="http://127.0.0.1") as client:
        response = client.get("/api/backlog", headers={HEADER: ensure_token()})

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {
        "generated_at",
        "flow_hours",
        "compare_days",
        "publication",
        "totals",
        "platforms",
        "unresolved",
        "error",
    }, "the block is added, every existing card key still ships"
    block = payload["unresolved"]
    assert set(block) == {"status", "total", "shown", "limit", "items"}
    assert block["status"] == api.BACKLOG_UNRESOLVED_STATUS


# ------------------------------------------------------------------- one story
def test_the_two_readers_of_this_population_agree_on_how_many_there_are():
    """`/api/review/queue` already lists these rows on the review tab.

    The worklist may not quietly become a third definition of «неподтверждённых
    сколько»: both readers count `posts.status == 'publish_unknown'`, and a new
    filter in either of them has to show up here as a mismatch.
    """
    from tgfarm.review import review_queue

    platform = _platform()
    channel = _channel(platform=platform)
    _unknown(channel, created_h=-47000)

    block = api._backlog(now=NOW)["unresolved"]
    review = review_queue(limit=1)

    assert block["total"] == review["counts"]["unknown"], (
        "the queue card and the review board disagree about who waits for a decision"
    )
    assert block["status"] == "publish_unknown"
    assert review["buckets"]["unknown"], "the board names them; so must the card"
    assert all(
        item["public_id"] != "" for item in block["items"]
    ), "a row the operator cannot address is not a named row"
