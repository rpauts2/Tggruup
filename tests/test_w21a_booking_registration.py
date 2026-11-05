"""W21-A seam: the booking pass is on the clock, behind the right switch, in the
right shape — and its id is readable from the module that owns the work.

Three claims, none of which the code made before this package:

1. `python -m tgfarm.cli serve` with `PLAN_EXECUTION_ENABLED=true` reaches
   `s4_generation.book_plan_slots`. At `a6c9b46` the only caller was a test
   stage, so the pass existed and never ran; a missing registration is exactly
   the defect the two neighbouring jobs were added for, one layer further down.
2. With the switch off it is not registered at all, so a flag-off process has
   the same job set base has. This is checked as a *set difference between two
   builds in the same test*, not as an absolute count of ids, because
   `tests/conftest.py` gives the suite one shared database and an absolute
   number here would be a neighbour's number.
3. The job is shaped like its neighbours — same trigger family, `max_instances`,
   `coalesce`, a misfire grace — because a plan job with a different life cycle
   from the switch that owns it is how a portfolio silently stops being booked.

The id and the interval are asserted to be *readable from* `tgfarm.s4_generation`
rather than hard-coded here, since `tgfarm/api.py`'s job list is a central
contract another package owns: the coordinator adds the third entry to that
payload from these same constants, and a test that pinned the literal string
would be the one thing that could not survive the rename.
"""

from __future__ import annotations

import ast
import inspect
import subprocess
import sys
from pathlib import Path

import pytest
from apscheduler.triggers.interval import IntervalTrigger

from tgfarm import pipeline, plan_execution, s4_generation
from tgfarm.core.settings import reset_settings_cache

ROOT = Path(__file__).resolve().parents[1]
PIPELINE_SOURCE = (ROOT / "tgfarm" / "pipeline.py").read_text(encoding="utf-8")
S4_SOURCE = (ROOT / "tgfarm" / "s4_generation" / "__init__.py").read_text(encoding="utf-8")

#: The three job ids the plan owns, in the order the scheduler registers them.
PLAN_JOB_IDS = (
    plan_execution.PLAN_BUILD_JOB_ID,
    plan_execution.PLAN_APPLY_JOB_ID,
    s4_generation.PLAN_BOOKING_JOB_ID,
)


def _flag(monkeypatch, enabled: bool) -> None:
    """Flip only this switch, and make `get_settings()` see the flip."""
    monkeypatch.setenv("PLAN_EXECUTION_ENABLED", "true" if enabled else "false")
    reset_settings_cache()


def _job_ids() -> set[str]:
    scheduler = pipeline._build_scheduler()
    try:
        return {job.id for job in scheduler.get_jobs()}
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)


def _guarded_block() -> str:
    """The source of `if settings.plan_execution_enabled:` and everything it owns.

    Cut on indentation rather than on a line count: the block's own length is
    this package's business, and a fixed slice would silently stop covering it
    the next time a job is added.
    """
    lines = PIPELINE_SOURCE.splitlines()
    start = next(
        index
        for index, line in enumerate(lines)
        if line.strip() == "if settings.plan_execution_enabled:"
    )
    indent = len(lines[start]) - len(lines[start].lstrip())
    out = [lines[start]]
    for line in lines[start + 1 :]:
        if line.strip() and (len(line) - len(line.lstrip())) <= indent:
            break
        out.append(line)
    return "\n".join(out)


# ------------------------------------------------------------------ registered
@pytest.mark.parametrize("job_id", PLAN_JOB_IDS)
def test_every_plan_job_is_registered_when_the_switch_is_on(monkeypatch, job_id):
    """The gap this package closes, in its narrowest form: the job exists."""
    _flag(monkeypatch, enabled=True)
    scheduler = pipeline._build_scheduler()
    try:
        assert scheduler.get_job(job_id) is not None, f"{job_id} is not on the clock"
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
        reset_settings_cache()


def test_the_booking_func_is_the_tick_that_wraps_the_pass(monkeypatch):
    """Registered identity, not merely a name in the registry.

    A `functools.partial`, a lambda around a stale copy or the bare
    `book_plan_slots` (blocking the event loop) would all still give the job an
    id; only the coroutine function this package ships is the correct target.
    """
    _flag(monkeypatch, enabled=True)
    scheduler = pipeline._build_scheduler()
    try:
        job = scheduler.get_job(s4_generation.PLAN_BOOKING_JOB_ID)
        assert job is not None
        assert job.func is s4_generation.book_plan_tick
        assert callable(job.func)
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
        reset_settings_cache()


def test_the_booking_job_is_shaped_like_the_two_it_sits_between(monkeypatch):
    """Interval trigger, one instance, coalesced, a grace time, a readable name."""
    _flag(monkeypatch, enabled=True)
    scheduler = pipeline._build_scheduler()
    try:
        job = scheduler.get_job(s4_generation.PLAN_BOOKING_JOB_ID)
        neighbours = [scheduler.get_job(job_id) for job_id in PLAN_JOB_IDS[:2]]
        assert job is not None
        assert isinstance(job.trigger, IntervalTrigger)
        assert job.trigger.interval.total_seconds() == (
            s4_generation.BOOKING_INTERVAL_MINUTES * 60
        ), "the trigger must be built from the interval constant, not a number beside it"
        assert job.max_instances == 1
        assert job.coalesce is True
        assert job.misfire_grace_time and job.misfire_grace_time > 0
        assert str(job.name or "").strip(), "an unnamed job is unreadable in the panel"
        for neighbour in neighbours:
            assert neighbour.max_instances == job.max_instances
            assert neighbour.coalesce == job.coalesce
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
        reset_settings_cache()


# -------------------------------------------------------------------- flag off
def test_flag_off_leaves_the_job_set_exactly_as_flag_on_minus_the_plan_jobs(monkeypatch):
    """Off means base, measured as a difference between two builds of the registry.

    The set diff is the whole test: an unconditional `add_job` for the booking
    pass shows up here, and so does a plan job that quietly stopped registering.
    No absolute id count is asserted, because the base set is another package's
    number to change.
    """
    _flag(monkeypatch, enabled=False)
    try:
        ids_off = _job_ids()
    finally:
        reset_settings_cache()

    _flag(monkeypatch, enabled=True)
    try:
        ids_on = _job_ids()
    finally:
        reset_settings_cache()

    assert ids_on - ids_off == set(PLAN_JOB_IDS), (
        f"the switch adds {sorted(ids_on - ids_off)}, not the three plan jobs"
    )
    assert ids_off & set(PLAN_JOB_IDS) == set(), "a plan job leaked into the flag-off registry"
    assert s4_generation.PLAN_BOOKING_JOB_ID not in ids_off


# ---------------------------------------------------------------- the contract
def test_the_id_and_the_rhythm_are_declared_by_the_module_that_owns_the_work():
    """What `api.py`'s job list will read, and where a rename has to happen.

    `plan_execution.py` is another package's file this wave, so the third job's
    identity lives with `book_plan_slots`: same module, same section, exported
    through `__all__` so a registry edit can import it instead of copying it.
    """
    assert s4_generation.PLAN_BOOKING_JOB_ID == "editorial-plan-booking"
    assert isinstance(s4_generation.BOOKING_INTERVAL_MINUTES, int)
    assert 0 < s4_generation.BOOKING_INTERVAL_MINUTES <= 60
    for name in (
        "PLAN_BOOKING_JOB_ID",
        "BOOKING_INTERVAL_MINUTES",
        "STAGE_BOOKING",
        "book_plan_tick",
    ):
        assert name in s4_generation.__all__, f"{name} is not exported for the registry"


def test_pipeline_names_the_booking_job_once_and_inside_the_guarded_block():
    """One registration, and it is the guarded one.

    Same discipline as `tests/test_plan_consumer_seam.py`, which counts the two
    existing ids by constant name because `pipeline` passes `id=…X`, not the
    literal string. A second reference would mean a job registered outside the
    switch, which is a behaviour change for every flag-off farm.
    """
    block = _guarded_block()
    assert PIPELINE_SOURCE.count("s4_generation.PLAN_BOOKING_JOB_ID") == 1
    assert block.count("scheduler.add_job(") == 3, (
        "the guarded block owns exactly the three plan jobs"
    )
    for reference in (
        "id=plan_execution.PLAN_BUILD_JOB_ID",
        "id=plan_execution.PLAN_APPLY_JOB_ID",
        "id=s4_generation.PLAN_BOOKING_JOB_ID",
    ):
        assert reference in block, f"{reference} is not registered inside the switch"


def test_the_scheduled_pass_has_a_production_caller_now():
    """The one-line statement of the package: `book_plan_slots` is called by code.

    Checked in the tick's own body rather than by a repository-wide grep, so a
    future edit that moves the caller elsewhere still has to keep one.
    """
    assert "book_plan_slots" in inspect.getsource(s4_generation.book_plan_tick)
    assert "s4_generation.book_plan_tick" in PIPELINE_SOURCE


def test_the_defect_s_grep_now_finds_a_caller_outside_s4():
    """`grep -rn book_plan_slots tgfarm/` returned the definition and nothing else.

    Restated as a test over the package's own files: at least one module outside
    `s4_generation` has to *call* the pass or the tick that wraps it, otherwise a
    self-running farm is back to booking nothing. Names are read off the AST, not
    off the text, because a prose mention in a comment is exactly the nothing
    this package was filed against — and test files are excluded, since a caller
    only a test can reach is the defect rather than the fix.
    """
    referenced = []
    for path in sorted((ROOT / "tgfarm").rglob("*.py")):
        if "s4_generation" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {
            node.attr if isinstance(node, ast.Attribute) else node.id
            for node in ast.walk(tree)
            if isinstance(node, (ast.Attribute, ast.Name))
        }
        if names & {"book_plan_slots", "book_plan_tick"}:
            referenced.append(str(path.relative_to(ROOT)))
    # W21-A shipped one caller: the scheduler. W22-D added the two the accepted
    # residual asked for — `tgfarm/cli.py`'s `plan-book` verb (the operator's
    # rehearsal and consent path) and `tgfarm/demo.py`'s booking stage (the offline
    # release gate) — both through this same entry point rather than a copy of it.
    # The claim this test guards is unchanged in kind: the pass is called by
    # production modules, and the set of them is known by name. A fourth caller
    # still lands here red, which is the point of listing them at all.
    expected = [
        str(Path("tgfarm") / "cli.py"),
        str(Path("tgfarm") / "demo.py"),
        str(Path("tgfarm") / "pipeline.py"),
    ]
    assert referenced == expected, (
        "the booking pass should be called by the scheduler, the CLI verb and the "
        f"offline gate, and by nothing else, found {referenced}"
    )


# ------------------------------------------------------------------ no cycles
def test_s4_generation_imports_the_pipeline_only_inside_a_function():
    """`pipeline` registers this job, so s4 may not import it at module level.

    `plan_execution.py` is imported by `pipeline` and imports `s7_publish` and
    `editorial_plan` at module level; a module-level `from tgfarm.pipeline import
    _record` in s4 would put the journaling helper in that ring. The AST walk
    reads only the module body, so the lazy import inside the journaling helper
    stays legal and a hoisted one is red.
    """
    tree = ast.parse(S4_SOURCE)
    module_level = [
        node
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    for node in module_level:
        target = getattr(node, "module", None) or ""
        names = [alias.name for alias in node.names]
        assert "tgfarm.pipeline" not in target, "s4 must not import pipeline at module level"
        assert "pipeline" not in names or target != "tgfarm"
    assert "from tgfarm.pipeline import" in S4_SOURCE, "the journaling import went missing"


def test_the_package_imports_in_both_orders_in_a_fresh_process():
    """A registration is not a cycle, and a fresh interpreter is the only place
    that can say so: inside the test session both modules are already loaded."""
    for statement in (
        "import tgfarm.pipeline, tgfarm.s4_generation",
        "import tgfarm.s4_generation, tgfarm.pipeline",
    ):
        done = subprocess.run(  # noqa: S603 - a fixed argv, no shell
            [sys.executable, "-c", statement],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert done.returncode == 0, f"{statement} failed: {done.stderr[-400:]}"
