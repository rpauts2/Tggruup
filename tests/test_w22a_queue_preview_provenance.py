"""W22-A: the queue preview has to say whether its plan provenance was read.

The distribution plan card shows an operator the upcoming queue — the one place
where "what goes next" is visible — and until now that list carried five keys and
no answer to «этот пост по плану или совпал?». `api._post_view` answered the same
question about the same `posts` row from the owner
(`tgfarm/editorial_plan.py::plan_provenance`), so the post card could say «по
плану» while the queue card said nothing at all. Two answers, one row.

The shape of the fix is fixed by the contract, not by taste:

* the row keys come from `plan_provenance`, resolved with ONE bounded
  `resolve_plan_labels` call per platform window;
* the platform item carries `queue_provenance`, either `"measured"` or
  `"plan_columns_missing"`, never null and never absent;
* the two silences stay distinguishable in both directions — "read fine and
  nothing here is by plan" is `measured` with null row keys, and "could not read"
  is `plan_columns_missing` with the row keys *absent* (the owner's
  `provenance_not_read()`), so neither can be misread as the other.

Why the drift case is the important one and not an edge case: the live
`tgfarm.db` has no `posts.plan_entry_id` / `posts.plan_slot_at` — the additive
pass has not run — and `select(Post, Channel)` asks the store for every column the
mapper declares. SQLite then refuses the *whole* statement, so a naive provenance
wire-up would break `/api/distribution/plan` in the only real condition. That is
exactly what the rejected `w21c-preview-provenance` (`b271066`) would have done:
its fallback helper took a `platform_id` string and spread it into `.where(*...)`
as characters. Every fallback test below therefore runs against a *real* drifted
store built with the same `ALTER TABLE ... DROP COLUMN` the drift preview reads,
not against a monkeypatched exception.

The stores here are private (`tmp_path` SQLite files with `distribution.session_scope`
pointed at them) because the session-wide test database is shared and dropping the
booking columns from it would break every later test that reads `posts`.
"""

from __future__ import annotations

import itertools
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, inspect, select
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.orm import sessionmaker

import tgfarm.api as api
import tgfarm.distribution as distribution
from tgfarm.config import Persona
from tgfarm.db.models import (
    Base,
    Channel,
    EditorialPlan,
    EditorialPlanEntry,
    Post,
)

#: The triple the owner is the only producer of, and the drift vocabulary.
PROVENANCE_KEYS = ("plan_entry_id", "plan_slot_at", "plan_public_id")
MEASURED = "measured"
COLUMNS_MISSING = "plan_columns_missing"
BOOKING_COLUMNS = ("plan_entry_id", "plan_slot_at")

#: The five keys the preview carried before this package; the window must still
#: carry them, or the panel at `ui/app.js:2424` loses the queue it renders today.
LEGACY_KEYS = ("post_public_id", "title", "channel", "status", "scheduled_at")

PLATFORMS = ("telegram", "vk", "dzen")
_SEQ = itertools.count()


# --------------------------------------------------------------------- fixtures
class _Store(SimpleNamespace):
    """A private SQLite store with the full schema, plus a session factory."""

    engine: object
    factory: object


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """`distribution.session_scope` pointed at a fresh, fully-migrated store."""
    engine = create_engine(f"sqlite:///{(tmp_path / 'preview.db').as_posix()}")
    Base.metadata.create_all(engine)
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

    monkeypatch.setattr(distribution, "session_scope", _scope)
    try:
        yield _Store(engine=engine, factory=factory)
    finally:
        engine.dispose()


def _drift(store) -> None:
    """Take the store back to the live base: the booking columns are not there.

    `ix_posts_plan_entry_id` has to go first — SQLite refuses
    `ALTER TABLE ... DROP COLUMN` while an index still names the column, which is
    the same reason `plan_additive_migrations` re-adds that index on boot.
    """
    with store.engine.begin() as conn:
        conn.exec_driver_sql("DROP INDEX IF EXISTS ix_posts_plan_entry_id")
        for name in BOOKING_COLUMNS:
            conn.exec_driver_sql(f"ALTER TABLE posts DROP COLUMN {name}")


def _columns(store, table: str) -> set[str]:
    return {c["name"] for c in inspect(store.engine).get_columns(table)}


# ---------------------------------------------------------------------- seeding
def _channel(store, platform: str, *, enabled: bool = True) -> tuple[str, Persona]:
    """One persona row in the store, and the `Persona` the planner must be told."""
    channel_id = f"pl{next(_SEQ):05d}"
    with store.factory() as session:
        session.add(
            Channel(
                id=channel_id,
                title=f"канал {channel_id}",
                platform=platform,
                enabled=enabled,
                timezone="UTC",
            )
        )
        session.commit()
    persona = Persona(
        id=channel_id, title=channel_id, niche="general", platform=platform, enabled=True
    )
    return channel_id, persona


def _plan_entry(store, *, public_id: str | None = None) -> tuple[int, str]:
    """A booked entry and its snapshot's public id.

    The id is read back rather than assumed, because `resolve_plan_labels` joins
    `editorial_plan_entries` to `editorial_plans` on the autoincrement key.
    """
    snapshot_id = public_id or f"snap{next(_SEQ):05d}"
    with store.factory() as session:
        plan = EditorialPlan(public_id=snapshot_id, horizon_days=7, status="active")
        session.add(plan)
        session.flush()
        entry = EditorialPlanEntry(
            plan_id=plan.id,
            cluster_public_id=f"cl{next(_SEQ):05d}",
            title="запись плана",
            niche="general",
            action="produce",
            platform="telegram",
            slot_at=datetime.now(UTC),
        )
        session.add(entry)
        session.commit()
        return int(entry.id), str(plan.public_id)


def _post(
    store,
    channel_id: str,
    *,
    status: str = "scheduled",
    hook: str = "заголовок",
    scheduled_at: datetime | None = None,
    plan_entry_id: int | None = None,
    plan_slot_at: datetime | None = None,
) -> str:
    public_id = f"pst{next(_SEQ):06d}"
    with store.factory() as session:
        session.add(
            Post(
                public_id=public_id,
                channel_id=channel_id,
                status=status,
                hook=hook,
                body="тело",
                scheduled_at=scheduled_at or datetime.now(UTC),
                created_at=scheduled_at or datetime.now(UTC),
                plan_entry_id=plan_entry_id,
                plan_slot_at=plan_slot_at,
            )
        )
        session.commit()
    return public_id


def _primary_plan(monkeypatch, personas: list[Persona]) -> dict[str, dict]:
    """What `/api/distribution/plan` shows for these personas, keyed by platform."""
    monkeypatch.setattr(distribution, "get_config_personas", lambda: list(personas))
    settings = SimpleNamespace(mode="dry", approval_required=False)
    catalog = {
        platform: {"enabled": True, "configured": True, "name": platform.upper()}
        for platform in PLATFORMS
    }
    items = distribution._build_primary_platform_plan(datetime.now(UTC), settings, catalog)
    return {item["platform"]: item for item in items}


def _row(preview: list[dict], public_id: str) -> dict:
    return next(item for item in preview if item["post_public_id"] == public_id)


@contextmanager
def _statements_touching(store, needle: str):
    """Every statement that names `needle`, collected while the block runs."""
    seen: list[str] = []

    def _before(conn, cursor, statement, parameters, context, executemany):
        if needle in statement:
            seen.append(statement)

    event.listen(store.engine, "before_cursor_execute", _before)
    try:
        yield seen
    finally:
        event.remove(store.engine, "before_cursor_execute", _before)


# ------------------------------------------------------- the drift is real, first
def test_the_drifted_store_is_the_condition_the_live_database_is_in(store):
    """The fixture must fail the way production does, or it proves nothing.

    `select(Post, Channel)` loads every mapped column, so a store without the
    booking columns refuses the whole statement with SQLite's own words — the
    marker `provenance_columns_missing` matches. Proving that here is what makes
    the fallback tests below tests of SQL rather than tests of a mock.
    """
    channel_id, _persona = _channel(store, "telegram")
    _post(store, channel_id)
    _drift(store)

    assert not (set(BOOKING_COLUMNS) & _columns(store, "posts"))

    with store.factory() as session, pytest.raises(SQLAlchemyError) as caught:
        session.execute(
            select(Post, Channel)
            .join(Channel, Channel.id == Post.channel_id)
            .where(Channel.platform == "telegram")
            .limit(5)
        ).all()

    assert "no such column" in str(caught.value), str(caught.value)[:200]


# --------------------------------------------------- the contract: two vocabularies
def test_the_preview_carries_the_three_keys_when_the_columns_are_readable(store, monkeypatch):
    """A booked post is by plan in the queue exactly as it is on its card."""
    channel_id, persona = _channel(store, "telegram")
    entry_id, snapshot = _plan_entry(store)
    # The owner stamps `plan_slot_at` at whole-second precision
    # (`editorial_plan._slot_stamp`), so the seed is second-aligned: a microsecond
    # here would be the test's own invention, not a drift in the payload.
    booked = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=5)
    public_id = _post(
        store, channel_id, plan_entry_id=entry_id, plan_slot_at=booked
    )

    item = _primary_plan(monkeypatch, [persona])["telegram"]
    row = _row(item["queue_preview"], public_id)

    assert item["queue_provenance"] == MEASURED
    assert row["plan_entry_id"] == entry_id
    assert row["plan_public_id"] == snapshot
    assert row["plan_slot_at"] is not None
    assert datetime.fromisoformat(row["plan_slot_at"]) == booked
    # The five keys the panel already renders are not traded away for the new ones.
    assert set(LEGACY_KEYS) <= set(row)


def test_a_readable_window_where_nothing_is_by_plan_is_measured_with_nulls(
    store, monkeypatch
):
    """The inversion this contract exists to prevent, in one of its two directions.

    A queue of unplanned posts is a *read* that came back empty: `measured`, with
    the three keys present and null. It must never be reported as
    `plan_columns_missing`, which would tell the operator the store cannot answer
    when it just answered "none of these were booked".
    """
    channel_id, persona = _channel(store, "telegram")
    public_id = _post(store, channel_id)

    item = _primary_plan(monkeypatch, [persona])["telegram"]
    row = _row(item["queue_preview"], public_id)

    assert item["queue_provenance"] == MEASURED
    for key in PROVENANCE_KEYS:
        assert key in row, f"{key} is absent where the read succeeded"
        assert row[key] is None, row[key]


def test_a_drifted_window_is_reported_as_missing_and_not_as_measured(store, monkeypatch):
    """...and the other direction: an unread read may not look like a measured zero.

    The rows keep the five keys they always had — the operator still sees the
    queue — while the three provenance keys are *absent*, because the owner's
    `provenance_not_read()` is the only answer that does not invent a "created
    without a plan" about a store that was never asked. `queue_provenance` is what
    says so, at the item level, once for the window.
    """
    channel_id, persona = _channel(store, "telegram")
    public_id = _post(store, channel_id, hook="в очереди")
    _drift(store)

    item = _primary_plan(monkeypatch, [persona])["telegram"]

    assert item["queue_provenance"] == COLUMNS_MISSING
    row = _row(item["queue_preview"], public_id)
    assert row["title"] == "в очереди"
    assert row["status"] == "scheduled"
    assert set(LEGACY_KEYS) <= set(row)
    for key in PROVENANCE_KEYS:
        assert key not in row, f"{key} was invented for a read that could not happen"


def test_the_endpoint_does_not_die_on_the_live_schema(store, monkeypatch):
    """The single reason `b271066` is not accepted, pinned as a requirement.

    On a drifted store the naive wire-up raises `OperationalError` out of
    `_build_primary_platform_plan`, which is `/api/distribution/plan` returning 500
    in the only condition the production database is in today.
    """
    channel_id, persona = _channel(store, "telegram")
    _post(store, channel_id)
    _drift(store)

    items = _primary_plan(monkeypatch, [persona])

    assert set(items) == {"telegram"}, items
    assert items["telegram"]["queue_preview"], "the drifted window lost the queue"


def test_the_window_is_the_same_rows_whether_the_columns_are_readable_or_not(
    store, monkeypatch
):
    """The fallback may drop columns, never posts — and never a status.

    Same statuses, same order, same cap: if the two reads disagreed about *which*
    five posts are next, the card would change its queue depending on whether the
    drift pass had run, and nothing downstream would say so. The `gated` member is
    in the expected window on purpose, because the tuple is passed to the fallback
    by the caller and a dropped status would otherwise show up only as a quieter
    card.
    """
    channel_id, persona = _channel(store, "telegram")
    start = datetime.now(UTC)
    gated = _post(store, channel_id, status="gated", scheduled_at=start)
    ids = [gated] + [
        _post(store, channel_id, scheduled_at=start + timedelta(minutes=index))
        for index in range(1, 8)
    ]
    # A disabled channel and a delivered status are outside the window in both reads.
    other_id, _ = _channel(store, "telegram", enabled=False)
    _post(store, other_id)
    _post(store, channel_id, status="published")

    before = _primary_plan(monkeypatch, [persona])["telegram"]["queue_preview"]
    _drift(store)
    after = _primary_plan(monkeypatch, [persona])["telegram"]["queue_preview"]

    assert [r["post_public_id"] for r in before] == ids[:5]
    assert [r["post_public_id"] for r in before] == [r["post_public_id"] for r in after]


def test_every_platform_item_names_its_provenance_state(store, monkeypatch):
    """`queue_provenance` is never null and never absent, on every platform."""
    channel_id, telegram = _channel(store, "telegram")
    vk_id, vk = _channel(store, "vk")
    _post(store, channel_id)
    _post(store, vk_id)

    for drifted in (False, True):
        if drifted:
            _drift(store)
        for platform, item in _primary_plan(monkeypatch, [telegram, vk]).items():
            assert "queue_provenance" in item, f"{platform} omits the state"
            assert item["queue_provenance"] in (MEASURED, COLUMNS_MISSING), item
            assert item["queue_provenance"] == (
                COLUMNS_MISSING if drifted else MEASURED
            ), platform


# --------------------------------------------------------- one bounded read
def test_the_labels_resolve_in_one_read_per_platform_window(store, monkeypatch):
    """Contract: no per-row query.

    Three booked posts in one window must cost exactly one statement against
    `editorial_plan_entries`. A per-row resolve would be three, and it would pass
    every value assertion above.
    """
    channel_id, persona = _channel(store, "telegram")
    expected: dict[int, str] = {}
    for _ in range(3):
        entry_id, snapshot = _plan_entry(store)
        expected[entry_id] = snapshot
        _post(store, channel_id, plan_entry_id=entry_id, plan_slot_at=datetime.now(UTC))

    with _statements_touching(store, "editorial_plan_entries") as seen:
        item = _primary_plan(monkeypatch, [persona])["telegram"]

    assert len(seen) == 1, f"the label lookup was paid for {len(seen)} times"
    assert item["queue_provenance"] == MEASURED
    assert len(item["queue_preview"]) == 3
    # Every row got its answer out of the single read, which is what makes the
    # bounded count above a real statement about this window and not a no-op.
    assert {r["plan_entry_id"]: r["plan_public_id"] for r in item["queue_preview"]} == expected


def test_a_window_that_names_nothing_costs_no_entry_read(store, monkeypatch):
    """`resolve_plan_labels` touches no table when nothing is named, and the
    preview must not paper over that by querying anyway."""
    channel_id, persona = _channel(store, "telegram")
    _post(store, channel_id)

    with _statements_touching(store, "editorial_plan_entries") as seen:
        item = _primary_plan(monkeypatch, [persona])["telegram"]

    assert seen == [], f"an unnamed window still joined the plan tables: {len(seen)}"
    assert item["queue_provenance"] == MEASURED


# ------------------------------------------------------------- one owner, one answer
def test_the_preview_and_the_post_card_agree_on_the_same_post(store, monkeypatch):
    """Contract: the row keys are the three `api.py` serves for the same row.

    Compared through the real `api._post_view`, not against a restated dict, so a
    second implementation in either service fails here even when its own tests
    pass. The three cases are the ones that differ: booked, dangling reference
    (the entry row is gone, the id is not), and unplanned.
    """
    channel_id, persona = _channel(store, "telegram")
    entry_id, snapshot = _plan_entry(store)
    booked = datetime.now(UTC) - timedelta(minutes=3)

    planned = _post(store, channel_id, plan_entry_id=entry_id, plan_slot_at=booked)

    # A second entry whose snapshot row is then deleted: the post keeps naming it.
    gone_entry_id, _ = _plan_entry(store)
    dangling = _post(store, channel_id, plan_entry_id=gone_entry_id, plan_slot_at=booked)
    unplanned = _post(store, channel_id)

    with store.factory() as session:
        plan = session.get(EditorialPlan, session.get(EditorialPlanEntry, gone_entry_id).plan_id)
        session.delete(plan)
        session.commit()

    item = _primary_plan(monkeypatch, [persona])["telegram"]
    preview = item["queue_preview"]
    assert item["queue_provenance"] == MEASURED

    with store.factory() as session:
        for public_id in (planned, dangling, unplanned):
            row = session.execute(
                select(Post).where(Post.public_id == public_id)
            ).scalar_one()
            served = api._post_view(session, row, detail=False)
            shown = _row(preview, public_id)
            assert {k: shown[k] for k in PROVENANCE_KEYS} == {k: served[k] for k in PROVENANCE_KEYS}, (
                public_id
            )

    # And the values themselves, so the comparison above cannot pass by both sides
    # returning the same wrong thing.
    shown = _row(preview, planned)
    assert (shown["plan_entry_id"], shown["plan_public_id"]) == (entry_id, snapshot)
    shown = _row(preview, dangling)
    assert shown["plan_entry_id"] == gone_entry_id
    assert shown["plan_public_id"] is None, "a deleted snapshot was renamed into an answer"
    assert _row(preview, unplanned)["plan_entry_id"] is None


def test_the_five_legacy_keys_are_still_formed_the_way_the_panel_renders_them(
    store, monkeypatch
):
    """The window's own formatting is a contract, and both shapes share it.

    `_queue_preview_fields` exists because the truncation and the minute-precision
    stamp would otherwise be written twice — once for the ORM row, once for the
    drift fallback — and a post would render differently depending on whether the
    migration had run. Pinned here on purpose: the mutation that moved
    `timespec="minutes"` to `"seconds"` survived every other test in this file.
    """
    channel_id, persona = _channel(store, "telegram")
    long_hook = "д" * 200
    # Second-and-microsecond precision, so a truncation to minutes is observable
    # rather than accidental.
    at = datetime.now(UTC).replace(second=37, microsecond=500000)
    public_id = _post(store, channel_id, hook=long_hook, scheduled_at=at)

    stamps = {}
    for drifted in (False, True):
        if drifted:
            _drift(store)
        row = _row(
            _primary_plan(monkeypatch, [persona])["telegram"]["queue_preview"], public_id
        )
        assert row["title"] == "д" * 160, len(row["title"])
        assert row["scheduled_at"] == at.isoformat(timespec="minutes"), row["scheduled_at"]
        assert row["scheduled_at"].endswith("+00:00")
        assert row["scheduled_at"].split("+")[0].count(":") == 1, "seconds leaked into the stamp"
        stamps[row["post_public_id"]] = row["scheduled_at"]

    assert len(stamps) == 1, "the same post stamped differently in the two shapes"


# ------------------------------------------------------------------- not swallowed
def test_a_query_that_failed_is_not_reported_as_a_missing_column(store):
    """The fallback answers the drift and nothing else.

    "no such table" is not "no such column": a window that broke has to propagate,
    because a queue the store refused to answer and a queue that is empty must not
    arrive as the same five rows of nothing. The columns here are readable — the
    only thing missing is a table the join needs — so this exercises the predicate
    rather than the drift path.
    """
    with store.engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE channels")

    window = (
        Channel.platform == "telegram",
        Channel.enabled.is_(True),
        Post.status.in_(("gated", "scheduled")),
    )
    with store.factory() as session:
        with pytest.raises(SQLAlchemyError) as caught:
            distribution._primary_queue_preview(session, window)

    assert "no such table" in str(caught.value)
    assert "no such column" not in str(caught.value)


def test_a_store_that_failed_to_answer_is_not_reported_as_the_drift(store):
    """It is the predicate that decides the fallback, not the bare fact of an error.

    Pinned because the tempting hardening — `except SQLAlchemyError: read the narrow
    window` — passes every other test in this file: when the first read breaks, the
    fallback usually breaks the same way with the same message, and a test that only
    asks whether an error escaped cannot tell "propagated" from "failed twice". Here
    the narrow read *would* have answered, so a swallowed failure returns a plausible
    five rows stamped `plan_columns_missing` about a store that is merely corrupt.
    """
    channel_id, _persona = _channel(store, "telegram")
    _post(store, channel_id)
    window = (
        Channel.platform == "telegram",
        Channel.enabled.is_(True),
        Post.status.in_(("gated", "scheduled")),
    )
    with store.factory() as real:
        seen: list[object] = []

        def execute(statement, *args, **kwargs):
            seen.append(statement)
            if len(seen) == 1:
                raise SQLAlchemyError("database disk image is malformed")
            return real.execute(statement, *args, **kwargs)

        with pytest.raises(SQLAlchemyError) as caught:
            distribution._primary_queue_preview(SimpleNamespace(execute=execute), window)

    assert "malformed" in str(caught.value)
    assert len(seen) == 1, f"a failed read was papered over by a second one: {len(seen)}"


def test_the_drift_helper_is_not_handed_a_string(store):
    """The exact defect of `b271066`, pinned rather than remembered.

    Its `_queue_preview_without_booking(session, waiting: list)` was called with a
    `platform_id`, and SQLAlchemy spread the string into characters:
    `ArgumentError: Textual SQL expression 'v' should be explicitly declared as
    text('v')`. The window is a tuple of predicates on both sides here, so a string
    is a type error the helper cannot survive.
    """
    channel_id, _persona = _channel(store, "telegram")
    _post(store, channel_id)
    _drift(store)

    window = (
        Channel.platform == "telegram",
        Channel.enabled.is_(True),
        Post.status.in_(("gated", "scheduled")),
    )
    with store.factory() as session:
        preview, state = distribution._primary_queue_preview(session, window)

    assert state == COLUMNS_MISSING
    assert len(preview) == 1
    assert preview[0]["channel"] == f"канал {channel_id}"

    with store.factory() as session:
        with pytest.raises(ArgumentError) as caught:
            distribution._primary_queue_preview(session, "telegram")
    assert "no such column" not in str(caught.value), "a bare string produced a valid read"


# ------------------------------------------------------------------- one owner only
def test_distribution_asks_the_owner_for_the_keys_it_publishes():
    """The `PROVENANCE_SERVICES` rule, extended to the service this package added.

    `tests/test_w21c_provenance_owner.py` names only `api.py`, and that file is not
    this package's to edit, so the same guard is stated here for
    `tgfarm/distribution.py`: it must reach the payload through the owner and must
    not assemble a second copy of the triple.
    """
    import ast
    from pathlib import Path

    source = (Path(distribution.__file__)).read_text(encoding="utf-8")
    assert "plan_provenance(" in source, "distribution answers provenance without the owner"
    assert "resolve_plan_labels(" in source, "distribution resolves snapshots on its own"
    assert "provenance_not_read(" in source, "distribution invents its own unread shape"

    copies = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Dict):
            continue
        named = {
            key.value
            for key in node.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        if len(named & set(PROVENANCE_KEYS)) >= 2:
            copies.append(node.lineno)
    assert copies == [], f"distribution.py hand-writes the provenance triple at {copies}"


def test_the_provenance_state_names_are_the_owners_not_a_second_vocabulary():
    """`plan_columns_missing` is the owner's string, and `measured` is new here only
    because no owner name exists for "read successfully"."""
    from tgfarm.editorial_plan import PROVENANCE_COLUMNS_MISSING

    assert COLUMNS_MISSING == PROVENANCE_COLUMNS_MISSING
    assert distribution.QUEUE_PROVENANCE_MEASURED == MEASURED
