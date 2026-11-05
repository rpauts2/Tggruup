"""GET /api/editorial/plan/execution — the plan consumer's state, with its
unknowns kept unknown.

`tgfarm/plan_execution.py` is the editorial plan's only consumer and it sits
behind `PLAN_EXECUTION_ENABLED`, which ships off. The three existing plan
endpoints say what the plan decided; none of them can say whether anything was
listening. This file pins the endpoint that answers that, and every test below
is about the one failure mode the endpoint exists to avoid: a missing measurement
rendered as a harmless zero. A panel that shows `registered: false` for a
scheduler it cannot see, `applied: 0` for a tick that never counted, or
"nothing was applied" for a consumer that only ever ran in another process, tells
the operator the farm is healthy and idle when all it knows is that it is
looking in the wrong place.

Storage and escaping, stated because the brief asks which one this repo does:
the API returns stored text RAW — no HTML escaping anywhere in `tgfarm/api.py` —
and `tgfarm/ui/app.js` escapes at render time with its own `esc()`. That is the
behaviour of every other endpoint (`/api/editorial/plan` hands back a stored
entry title verbatim, and the reconcile route echoes an undeclared journal status
into a 409), so this endpoint does the same, and the test below pins both halves:
raw where the field is in the contract, absent where it is not.

Shared-database discipline: `tests/conftest.py` gives every file the same session
DB, so each id this file invents is `w18b2-` prefixed, every measurement is a
delta against a count taken in the same test, and the autouse fixture deletes
only the rows named by that test. It also pins the switch itself and drops the
cached `Settings` after undoing the environment, so no later file inherits a
flipped `PLAN_EXECUTION_ENABLED`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, update

from tgfarm import pipeline, plan_execution, s4_generation
from tgfarm.core.apiauth import ensure_token
from tgfarm.core.settings import reset_settings_cache
from tgfarm.db.models import (
    Channel,
    Cluster,
    EditorialPlan,
    EditorialPlanEntry,
    PipelineRun,
    Post,
)
from tgfarm.db.session import session_scope
from tgfarm.editorial_plan import build_editorial_plan

#: Stored text that would be dangerous if this layer tried to be helpful about it.
HOSTILE = '<script>alert("w18b2")</script>'
#: Short enough for `PipelineRun.status`, which is String(16).
HOSTILE_TOKEN = "<img src=x>"

#: The five paths the coordinator fixed for the panel, and their sub-keys.
CONTRACT: dict[str, tuple[str, ...]] = {
    "enabled": (),
    "jobs": ("id", "registered", "next_run_at"),
    "last_apply": ("at", "plan_id", "applied", "held", "reasons"),
    "plan": ("id", "created_at", "total", "produce", "schedule", "hold", "coverage"),
    "note": (),
}


def _client() -> TestClient:
    """An app without a scheduler — the ordinary shape of a served panel.

    `with_scheduler=False` and no `with:` block means the lifespan never runs, so
    `pipeline._scheduler` stays `None`: exactly the process that must not be able
    to say a job is missing.
    """
    from tgfarm.api import create_app

    return TestClient(
        create_app(with_scheduler=False),
        base_url="http://127.0.0.1",
        headers={"X-TGFarm-Token": ensure_token()},
    )


def _execution(client: TestClient) -> dict[str, Any]:
    response = client.get("/api/editorial/plan/execution")
    assert response.status_code == 200
    return response.json()


def _counts() -> dict[str, int]:
    with session_scope() as session:
        return {
            "plans": int(session.scalar(select(func.count()).select_from(EditorialPlan))),
            "entries": int(session.scalar(select(func.count()).select_from(EditorialPlanEntry))),
            "runs": int(session.scalar(select(func.count()).select_from(PipelineRun))),
            "posts": int(session.scalar(select(func.count()).select_from(Post))),
        }


def _apply_rows() -> list[int]:
    """Ids of the journal rows this stage owns, oldest first.

    Used to tell whether the shared DB is in the "never ran" state at all; the
    never-ran pin itself does not depend on that answer.
    """
    with session_scope() as session:
        return list(
            session.scalars(
                select(PipelineRun.id).where(
                    PipelineRun.stage == plan_execution.STAGE_APPLY
                ).order_by(PipelineRun.id)
            )
        )


class _Owned:
    """Row ids one test created, named so they are the only rows it deletes."""

    def __init__(self) -> None:
        self.cluster_ids: list[str] = []
        self.channel_ids: list[str] = []
        self.post_ids: list[str] = []
        self.run_ids: list[int] = []
        self.plan_ids: list[int] = []
        #: An active plan the builder displaced, so leaving means putting it back.
        self.restored_plan_id: int | None = None

    def forget(self) -> None:
        with session_scope() as session:
            if self.run_ids:
                session.execute(delete(PipelineRun).where(PipelineRun.id.in_(self.run_ids)))
            #: `superseded_by_id` is a foreign key into `editorial_plans`, and the
            #: row carrying it belongs to whoever planned before this test, so the
            #: pointer has to go before the row it points at.
            if self.restored_plan_id is not None:
                session.execute(
                    update(EditorialPlan)
                    .where(EditorialPlan.id == self.restored_plan_id)
                    .values(status="active", superseded_by_id=None)
                )
            if self.plan_ids:
                session.execute(
                    update(EditorialPlan)
                    .where(EditorialPlan.superseded_by_id.in_(self.plan_ids))
                    .values(superseded_by_id=None)
                )
                session.execute(
                    delete(EditorialPlanEntry).where(
                        EditorialPlanEntry.plan_id.in_(self.plan_ids)
                    )
                )
                session.execute(delete(EditorialPlan).where(EditorialPlan.id.in_(self.plan_ids)))
            if self.post_ids:
                session.execute(delete(Post).where(Post.public_id.in_(self.post_ids)))
            if self.channel_ids:
                session.execute(delete(Channel).where(Channel.id.in_(self.channel_ids)))
            if self.cluster_ids:
                session.execute(delete(Cluster).where(Cluster.public_id.in_(self.cluster_ids)))


@pytest.fixture()
def owned() -> _Owned:
    rows = _Owned()
    yield rows
    rows.forget()


@pytest.fixture()
def _no_active_plan():
    """Neutralise another file's active snapshots for one test, then put them back.

    The same mechanism `tests/test_plan_execution_flag.py` uses: this file needs
    the "no plan exists" state, which is what the live farm is in today, and the
    shared session DB is not its own to clean. Deleting somebody else's row would
    be worse than borrowing it, so the flip is undone on the way out.
    """
    with session_scope() as session:
        ids = session.execute(
            select(EditorialPlan.id).where(EditorialPlan.status == "active")
        ).scalars().all()
        for plan_id in ids:
            session.get(EditorialPlan, plan_id).status = "superseded"
    yield
    with session_scope() as session:
        for plan_id in ids:
            row = session.get(EditorialPlan, plan_id)
            if row is not None:
                row.status = "active"


@pytest.fixture(autouse=True)
def _pinned_switch(monkeypatch):
    """Own `PLAN_EXECUTION_ENABLED` for this file and leave no cached flip.

    The undo comes before the cache reset on purpose: `get_settings()` reads the
    environment when it rebuilds, so resetting while a monkeypatched value is
    still in the environment would hand the next file a consumer that looks
    switched on.
    """
    monkeypatch.setenv("PLAN_EXECUTION_ENABLED", "false")
    reset_settings_cache()
    yield
    monkeypatch.undo()
    reset_settings_cache()


class _FakeJob:
    def __init__(self, next_run_time: datetime | None) -> None:
        self.next_run_time = next_run_time


class _FakeScheduler:
    """The part of APScheduler this reader touches, with a chosen state."""

    def __init__(self, jobs: dict[str, _FakeJob], running: bool) -> None:
        self._jobs = jobs
        self.running = running

    def get_job(self, job_id: str) -> _FakeJob | None:
        return self._jobs.get(job_id)


def _seed_corpus(owned: _Owned, suffix: str) -> None:
    """Clusters and gated drafts the builder can actually plan over."""
    now = datetime.now(UTC)
    with session_scope() as session:
        for index, platform in enumerate(("telegram", "vk")):
            channel_id = f"w18b2-chan-{suffix}-{index}"
            owned.channel_ids.append(channel_id)
            session.add(
                Channel(
                    id=channel_id,
                    title=channel_id,
                    niche="tech",
                    platform=platform,
                    enabled=True,
                    posts_per_day=2,
                )
            )
        for index in range(4):
            cluster_id = f"w18b2-cluster-{suffix}-{index}"
            owned.cluster_ids.append(cluster_id)
            session.add(
                Cluster(
                    public_id=cluster_id,
                    label=cluster_id,
                    niche="tech",
                    status="ready",
                    heat=0.9 - 0.1 * index,
                    last_seen_at=now - timedelta(hours=1),
                )
            )
            post_id = f"w18b2-post-{suffix}-{index}"
            owned.post_ids.append(post_id)
            session.add(
                Post(
                    public_id=post_id,
                    channel_id=owned.channel_ids[index % 2],
                    hook="hook",
                    body="body",
                    status="gated",
                    cluster_public_id=cluster_id,
                )
            )


def _journal_apply_tick(
    owned: _Owned,
    *,
    status: str,
    items_in: int | None,
    items_out: int | None,
    meta: dict[str, Any],
) -> None:
    """Write one apply tick the way the consumer writes it, never by hand.

    `plan_execution._record` is the module's own journaling entry point and it
    delegates to `pipeline._record`, which is the owner of the three-state
    counter rule. Hand-writing the row would let a test invent a `meta` the
    executor cannot produce and then pin the endpoint to the invention.
    """
    before = set(_apply_rows())
    plan_execution._record(  # noqa: SLF001 - the consumer's own journal writer
        plan_execution.STAGE_APPLY,
        status,
        items_in,
        items_out,
        5,
        {"job": plan_execution.PLAN_APPLY_JOB_ID, **meta},
    )
    created = [row_id for row_id in _apply_rows() if row_id not in before]
    assert len(created) == 1, "the journalled tick did not reach the database"
    owned.run_ids.extend(created)


# ---------------------------------------------------------------- contract shape
def test_the_five_contract_paths_are_all_present() -> None:
    """The panel is being built against these words; the shape is the contract."""
    body = _execution(_client())
    assert set(body) == set(CONTRACT)
    assert isinstance(body["enabled"], bool)
    assert isinstance(body["note"], str) and body["note"]
    assert [job["id"] for job in body["jobs"]] == [
        plan_execution.PLAN_BUILD_JOB_ID,
        plan_execution.PLAN_APPLY_JOB_ID,
        s4_generation.PLAN_BOOKING_JOB_ID,
    ]
    for job in body["jobs"]:
        assert set(job) == set(CONTRACT["jobs"])
    assert set(body["last_apply"]) == set(CONTRACT["last_apply"])
    assert set(body["plan"]) == set(CONTRACT["plan"])


# ------------------------------------------------------------------- the switch
def test_flag_off_answers_and_says_so() -> None:
    """Off is the shipped default, and an off consumer still has a state."""
    body = _execution(_client())
    assert body["enabled"] is False
    assert body["note"].startswith("Потребитель плана выключен")


def test_the_switch_is_read_from_config_and_never_inferred_from_the_jobs(
    monkeypatch,
) -> None:
    """A running scheduler with no plan jobs must not turn `enabled` off.

    The two facts come from different places: the switch is what the operator
    configured, registration is what some process did with it, and they disagree
    the moment the config changed without a restart. Collapsing them would hide
    precisely that disagreement, which is the thing worth showing.
    """
    monkeypatch.setenv("PLAN_EXECUTION_ENABLED", "true")
    reset_settings_cache()
    monkeypatch.setattr(pipeline, "_scheduler", _FakeScheduler({}, running=True))
    body = _execution(_client())
    assert body["enabled"] is True
    assert [job["registered"] for job in body["jobs"]] == [False, False, False]


# -------------------------------------------------------------------- the jobs
def test_no_visible_scheduler_is_unknown_for_both_jobs() -> None:
    """The anti-false-zero pin: a scheduler this process cannot see is not absent.

    `pipeline._scheduler` is module state, so an API app built without one, a
    panel talking to a remote API and a serve process that has not started its
    scheduler yet all look identical from here — and none of them licenses
    `registered: false`.
    """
    assert pipeline._scheduler is None  # noqa: SLF001 - the precondition itself
    body = _execution(_client())
    for job in body["jobs"]:
        assert job["registered"] is None
        assert job["next_run_at"] is None


def test_a_built_but_unstarted_scheduler_is_still_unknown(monkeypatch) -> None:
    """APScheduler keeps pending jobs invisible until `start()`, so `get_job`
    would answer `None` for a job that does exist; only a running scheduler can
    be believed when it says a job is not there."""
    monkeypatch.setattr(
        pipeline, "_scheduler", _FakeScheduler({plan_execution.PLAN_BUILD_JOB_ID: _FakeJob(None)},
                                               running=False)
    )
    body = _execution(_client())
    assert [job["registered"] for job in body["jobs"]] == [None, None, None]


def test_a_running_scheduler_answers_registration_truthfully(monkeypatch) -> None:
    """`True` with its next run, `False` for the job genuinely missing."""
    #: Three hours ahead of UTC: the zone APScheduler reports in, since
    #: `_build_scheduler` passes `timezone=settings.tz`.
    moscow = timezone(timedelta(hours=3))
    monkeypatch.setattr(
        pipeline,
        "_scheduler",
        _FakeScheduler(
            {
                plan_execution.PLAN_BUILD_JOB_ID: _FakeJob(
                    datetime(2026, 1, 1, 12, tzinfo=moscow)
                ),
                s4_generation.PLAN_BOOKING_JOB_ID: _FakeJob(
                    datetime(2026, 1, 1, 13, 30, tzinfo=moscow)
                ),
            },
            running=True,
        ),
    )
    build, apply_, booking = _execution(_client())["jobs"]
    assert build == {
        "id": plan_execution.PLAN_BUILD_JOB_ID,
        "registered": True,
        # One clock for the whole response: a naive journal stamp and a local
        # next-run would otherwise name the same instant two different ways.
        "next_run_at": "2026-01-01T09:00:00+00:00",
    }
    assert apply_["registered"] is False
    assert apply_["next_run_at"] is None
    # The pass that writes `posts.plan_slot_at` is answered from the same registry
    # as the other two: without it the plan keeps its minutes to itself, and a
    # panel that cannot say so reads as an idle farm rather than a missing job.
    assert booking == {
        "id": s4_generation.PLAN_BOOKING_JOB_ID,
        "registered": True,
        "next_run_at": "2026-01-01T10:30:00+00:00",
    }


# ------------------------------------------------------------------ the last tick
def test_a_journal_with_no_apply_tick_reads_as_never_ran(monkeypatch) -> None:
    """No apply tick means unknown, and `None` means "never measured", not "nil".

    Read against a stage name no writer has ever used, because that is the same
    branch of the reader and it cannot depend on what another file left in the
    shared session DB: a `0` here reads as "it ran and applied nothing", which is
    a different fact and an unfair verdict on a consumer that has not run.
    """
    monkeypatch.setattr(plan_execution, "STAGE_APPLY", "w18b2_stage_nobody_writes")
    last = _execution(_client())["last_apply"]
    assert last == {"at": None, "plan_id": None, "applied": None, "held": None, "reasons": None}
    assert last["applied"] is None and last["held"] is None


def test_the_real_journal_says_the_same_while_it_is_empty() -> None:
    """The same nulls, read off the stage the consumer actually writes.

    `editorial_plan_apply` is journalled only by `tgfarm/plan_execution.py`, so
    for most of a suite run this journal genuinely is empty and the pin above is
    checked against the real stage name too. Once some file has run an apply
    tick ahead of this one, the state is not observable without editing rows this
    file does not own, and the test says so instead of pretending.
    """
    rows = _apply_rows()
    if rows:
        pytest.skip(f"the shared journal already holds apply row(s) {rows}")
    last = _execution(_client())["last_apply"]
    assert last == {"at": None, "plan_id": None, "applied": None, "held": None, "reasons": None}
    assert "нет ни одной записи" in _execution(_client())["note"]


def test_the_never_ran_note_names_the_switch_first_in_both_switch_states(
    monkeypatch,
) -> None:
    """The branch that shipped wrong, pinned from both sides.

    The salvaged note claimed "тикеты не регистрировались" — registration is a
    fact about a scheduler this process cannot see, and an empty journal does not
    evidence it. And with the switch on and nothing run, the honest sentence is
    that no tick has run yet, not that the jobs are missing.
    """
    monkeypatch.setattr(plan_execution, "STAGE_APPLY", "w18b2_stage_nobody_writes")
    off = _execution(_client())["note"]
    assert off.startswith("Потребитель плана выключен")
    assert "нет ни одной записи" in off and "не регистрировались" not in off

    monkeypatch.setenv("PLAN_EXECUTION_ENABLED", "true")
    reset_settings_cache()
    on = _execution(_client())["note"]
    assert on.startswith("Потребитель плана включён, но")
    assert "ни один тик ещё не отработал" in on and "не регистрировались" not in on


def test_a_journalled_apply_tick_comes_back_with_its_histogram(owned) -> None:
    """The tick the executor wrote is the tick the panel reads, counters and all."""
    _journal_apply_tick(
        owned,
        status="ok",
        items_in=5,
        items_out=2,
        meta={
            "plan_id": "w18b2-plan",
            "held": 3,
            "reasons": {
                plan_execution.REASON_SLOT_NOT_DUE: 2,
                plan_execution.REASON_PLAN_HOLD: 1,
            },
        },
    )
    body = _execution(_client())
    last = body["last_apply"]
    assert last["applied"] == 2
    assert last["held"] == 3
    assert last["plan_id"] == "w18b2-plan"
    assert last["reasons"] == {
        plan_execution.REASON_SLOT_NOT_DUE: 2,
        plan_execution.REASON_PLAN_HOLD: 1,
    }
    assert last["at"].endswith("+00:00")
    assert plan_execution.REASON_SLOT_NOT_DUE in body["note"]
    assert "применил 2" in body["note"] and "удержал 3" in body["note"]


def test_the_newest_tick_wins(owned) -> None:
    """The endpoint reports one tick, and it is the last one the consumer wrote."""
    _journal_apply_tick(
        owned, status="ok", items_in=1, items_out=1, meta={"plan_id": "older", "held": 0,
                                                           "reasons": {}}
    )
    _journal_apply_tick(
        owned, status="ok", items_in=2, items_out=7, meta={"plan_id": "newest", "held": 0,
                                                            "reasons": {}}
    )
    last = _execution(_client())["last_apply"]
    assert last["plan_id"] == "newest"
    assert last["applied"] == 7


def test_an_unmeasured_counter_is_not_reported_as_a_zero(owned) -> None:
    """The P34 rule, at the endpoint: a no-plan tick journalled `not_reported`.

    `apply_plan_tick` passes `None` for both counters when the plan was missing,
    and `_record` stores that as a column 0 plus the marker that undoes it. Read
    the columns and the answer is "it ran and queued nothing"; read the marker
    and the answer is "there was nothing to queue".
    """
    _journal_apply_tick(
        owned,
        status="skipped",
        items_in=None,
        items_out=None,
        meta={"plan_id": None, "held": 0,
              "reasons": {plan_execution.REASON_NO_PLAN: 1}},
    )
    last = _execution(_client())["last_apply"]
    assert last["applied"] is None
    assert last["held"] == 0
    assert last["reasons"] == {plan_execution.REASON_NO_PLAN: 1}
    assert "пропущен" in _execution(_client())["note"]


def test_an_error_tick_confirms_nothing(owned) -> None:
    """A crashed tick carries no plan, no counters and no histogram."""
    _journal_apply_tick(owned, status="error", items_in=None, items_out=None,
                        meta={"plan_id": None})
    last = _execution(_client())["last_apply"]
    assert last["plan_id"] is None
    assert last["applied"] is None
    assert last["held"] is None
    assert last["reasons"] is None


def test_a_measured_zero_stays_a_zero(owned) -> None:
    """The other half of the rule: a tick that counted nothing reports `0`.

    Refusing to invent zeros must not turn into refusing to report them, or a
    healthy idle queue looks like a broken reader.
    """
    _journal_apply_tick(
        owned, status="ok", items_in=0, items_out=0,
        meta={"plan_id": "w18b2-zero", "held": 0, "reasons": {}},
    )
    last = _execution(_client())["last_apply"]
    assert last["applied"] == 0
    assert last["held"] == 0
    assert last["reasons"] == {}


# ---------------------------------------------------------------------- the plan
def _build_active_plan(owned: _Owned) -> dict[str, Any]:
    """Persist a plan with the repo's own builder, remembering what it displaced.

    `build_editorial_plan(persist=True)` flips the previous ACTIVE snapshot to
    `superseded` and stamps `superseded_by_id`; leaving that flip behind would
    hand the next file a database with no active plan, so the displaced row is
    recorded and put back by `owned.forget()`.
    """
    with session_scope() as session:
        displaced = session.scalar(select(EditorialPlan.id).where(EditorialPlan.status == "active"))
    owned.restored_plan_id = int(displaced) if displaced is not None else None
    built = build_editorial_plan(now=datetime.now(UTC))
    with session_scope() as session:
        row_id = session.scalar(
            select(EditorialPlan.id).where(EditorialPlan.public_id == built["public_id"])
        )
    owned.plan_ids.append(int(row_id))
    return built


def test_the_plan_block_matches_what_the_builder_reported(owned) -> None:
    """Numbers built by the repo's own builder, read back through the endpoint."""
    _seed_corpus(owned, "plan")
    built = _build_active_plan(owned)
    summary = built["summary"]
    plan_block = _execution(_client())["plan"]
    assert plan_block["id"] == built["public_id"]
    assert plan_block["total"] == summary["total"]
    assert plan_block["produce"] == summary["produce"]
    assert plan_block["schedule"] == summary["schedule"]
    assert plan_block["hold"] == summary["hold"]
    assert plan_block["coverage"] == summary["coverage"]
    assert plan_block["created_at"].endswith("+00:00")
    assert summary["total"] > 0, "the seeded corpus planned nothing: the pin is vacuous"
    assert summary["produce"] + summary["schedule"] + summary["hold"] == summary["total"]


def test_no_active_plan_is_all_unknowns_not_an_empty_plan(_no_active_plan) -> None:
    """A plan that does not exist is not a plan with zero entries.

    `/api/editorial/plan` deliberately falls back to an ephemeral preview; this
    reader must not, or it would show decisions the consumer never saw.
    """
    body = _execution(_client())
    assert body["plan"] == {
        "id": None,
        "created_at": None,
        "total": None,
        "produce": None,
        "schedule": None,
        "hold": None,
        "coverage": None,
    }
    #: The switch clause still leads, whatever the plan block ended up as.
    assert body["note"].startswith("Потребитель плана")


# --------------------------------------------------------------------- read-only
def test_the_call_writes_nothing(owned) -> None:
    """Every row class this endpoint could plausibly touch, counted before and after."""
    _seed_corpus(owned, "readonly")
    built = _build_active_plan(owned)
    _journal_apply_tick(
        owned, status="ok", items_in=3, items_out=1,
        meta={"plan_id": built["public_id"], "held": 2,
              "reasons": {plan_execution.REASON_DAILY_CAP: 2}},
    )
    _journal_apply_tick(owned, status="ok", items_in=1, items_out=1,
                        meta={"plan_id": built["public_id"], "held": 0, "reasons": {}})
    client = _client()
    before = _counts()
    body = _execution(client)
    assert _counts() == before, "a read-only endpoint wrote a row"
    # A second call, in case the reader mutates anything it reads.
    assert _execution(client) == body
    assert _counts() == before


def test_the_route_is_authenticated_like_its_neighbours() -> None:
    """Auth is the middleware's, not this route's: an unauthenticated call is refused."""
    from tgfarm.api import create_app

    anonymous = TestClient(create_app(with_scheduler=False), base_url="http://127.0.0.1")
    assert anonymous.get("/api/editorial/plan/execution").status_code in (401, 403)


# ------------------------------------------------------------------------- text
def test_the_note_is_one_sentence_that_names_the_switch_and_stays_honest() -> None:
    """One Russian sentence, with the two claims it is not allowed to make."""
    body = _execution(_client())
    note = body["note"]
    assert note.endswith(".") and ". " not in note, note
    assert note.startswith("Потребитель плана выключен")
    #: Not knowable from a process that cannot see the scheduler, so not said.
    assert "не регистрировались" not in note
    #: The repo's word for a tick is "тик"; "тикет" was never anybody's term.
    assert "тикет" not in note


def test_stored_text_comes_back_raw_and_the_reader_decides_what_it_shows(owned) -> None:
    """Escaping is the panel's job here, exactly as in every other route.

    Measured, not assumed. `/api/editorial/plan` returns a stored entry title
    verbatim — `<script>` arrives as `<script>` inside a JSON string, with only
    the quotes the JSON encoding itself requires — so this endpoint would be the
    only place in the API quietly rewriting operator data if it escaped. Two
    other things the measurement turned up, and both are pinned here: the plan's
    stored `summary["coverage_note"]` never reaches any response at all, because
    `_plan_dict` recomputes it from the live posts on every read; and the one
    free-text field this endpoint owns, `note`, carries a stored journal value
    through unaltered. The plan headline block is ids and counts only, so a
    hostile title cannot reach it.
    """
    _seed_corpus(owned, "escape")
    built = _build_active_plan(owned)
    with session_scope() as session:
        plan = session.scalar(
            select(EditorialPlan).where(EditorialPlan.public_id == built["public_id"])
        )
        summary = dict(plan.summary or {})
        summary["coverage_note"] = HOSTILE
        plan.summary = summary
        session.execute(
            update(EditorialPlanEntry)
            .where(EditorialPlanEntry.plan_id == plan.id)
            .values(title=HOSTILE)
        )
    _journal_apply_tick(owned, status=HOSTILE_TOKEN, items_in=1, items_out=1,
                        meta={"plan_id": None, "held": 0, "reasons": {}})

    from tgfarm.api import create_app

    editorial = TestClient(
        create_app(with_scheduler=False),
        base_url="http://127.0.0.1",
        headers={"X-TGFarm-Token": ensure_token()},
    ).get("/api/editorial/plan")
    assert editorial.status_code == 200
    assert editorial.headers["content-type"].startswith("application/json")
    #: Raw, like every other route: the value round-trips byte for byte.
    assert {entry["title"] for entry in editorial.json()["entries"]} == {HOSTILE}
    assert "&lt;script&gt;" not in editorial.text
    #: Stored, and never served: `_plan_dict` overwrites this key per read.
    assert HOSTILE not in editorial.json()["summary"]["coverage_note"]

    body = _execution(_client())
    assert HOSTILE_TOKEN in body["note"]
    assert HOSTILE not in json.dumps(body, ensure_ascii=False)
