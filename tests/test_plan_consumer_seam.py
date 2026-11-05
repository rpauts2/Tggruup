"""The plan's docstring may only claim a consumer that really reads the plan.

`tgfarm/editorial_plan.py` carries a `CONSUMER_MODULE` seam and a paragraph
about who consumes the plan. Until wave 17 the paragraph was the truth and the
seam was empty. Now a consumer exists, and the failure mode this file guards is
the one the project has already been burned by twice from the other side: a
claim of readiness that the code does not back up. So both directions are
checked statically — a named consumer must actually read the plan, and a
"nobody consumes this" sentence must not survive a set seam.

No database is touched here: every assertion is about source text, so this file
cannot interfere with the shared session DB other suites contend over.
"""

from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path

from tgfarm import editorial_plan, plan_execution
from tgfarm.core.settings import get_settings

PIPELINE_SOURCE = (Path(__file__).resolve().parents[1] / "tgfarm" / "pipeline.py").read_text(
    encoding="utf-8"
)


def test_a_named_consumer_is_a_real_importable_module() -> None:
    named = editorial_plan.CONSUMER_MODULE
    assert named, "the seam is empty, so the docstring must not describe a driver"
    assert importlib.util.find_spec(named) is not None, f"{named} is not importable"


def test_the_named_consumer_actually_reads_the_plan() -> None:
    """A module that imports the plan but never reads its decisions is not a consumer."""
    source = inspect.getsource(plan_execution)
    assert "get_active_plan" in source, "the consumer never reads the active plan"
    assert "build_editorial_plan" in source, "the consumer never builds the plan"
    for decision in ("schedule", "hold"):
        assert f'"{decision}"' in source, f"the consumer never handles a {decision!r} entry"


def test_the_consumer_never_publishes_and_never_generates() -> None:
    """The claim in both docstrings is the boundary; a literal check keeps it honest."""
    source = inspect.getsource(plan_execution)
    for forbidden in (
        "publish_post",
        "send_message",
        "httpx",
        "requests.post",
        "generate_draft",
        "llm.generate",
    ):
        assert forbidden not in source, f"the consumer calls {forbidden}: it may only queue"


def test_the_docstring_matches_the_seam_instead_of_outrunning_it() -> None:
    doc = inspect.getdoc(editorial_plan) or ""
    assert editorial_plan.CONSUMER_MODULE in doc, "the docstring does not name the consumer"
    assert "Nothing in\nthe pipeline consumes it" not in doc, (
        "the docstring still claims nobody consumes the plan while a consumer is named"
    )
    assert "PLAN_EXECUTION_ENABLED" in doc, "the docstring omits the switch that gates the driver"


def test_the_driver_is_off_by_default_and_registered_behind_that_switch() -> None:
    """Flag-off behaviour must equal the old tree: the jobs must not exist at all.

    The default is read off the model field, not off a live singleton: another
    suite in the shared session may have monkeypatched settings, and a default is
    the thing an operator actually gets when they set nothing.
    """
    field = type(get_settings()).model_fields["plan_execution_enabled"]
    assert field.default is False, "a new driver must default to off"
    assert "if settings.plan_execution_enabled:" in PIPELINE_SOURCE
    for attribute in ("PLAN_BUILD_JOB_ID", "PLAN_APPLY_JOB_ID"):
        # Counted by the constant's NAME: pipeline passes `id=plan_execution.X`,
        # so searching for the id's value would report zero hits and read as a
        # missing registration.
        reference = f"plan_execution.{attribute}"
        assert PIPELINE_SOURCE.count(reference) == 1, (
            f"{attribute} is referenced {PIPELINE_SOURCE.count(reference)} times, "
            "so a job id is either missing or registered outside the guarded block"
        )


def test_the_reverse_run_catches_a_falsy_seam(monkeypatch) -> None:
    """If `CONSUMER_MODULE` is cleared, the seam check must fail rather than agree.

    This is the mutation that reintroduces the defect: a docstring describing a
    driver while the seam says there is none.
    """
    monkeypatch.setattr(editorial_plan, "CONSUMER_MODULE", None)
    try:
        test_a_named_consumer_is_a_real_importable_module()
    except AssertionError as exc:
        assert "the seam is empty" in str(exc)
    else:  # pragma: no cover - the guard must never pass with an empty seam
        raise AssertionError("an empty CONSUMER_MODULE passed the seam check")
