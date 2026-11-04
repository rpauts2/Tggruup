"""Do not draft for Dzen when only the offline composer can answer (priority 3).

Live numbers (2026-11-04, 6 h window): 156 Dzen drafts, 153 rejected by the
length gate because every one of them came from the offline composer. Drafting
for a channel that cannot ship is waste; ``DZEN_REQUIRES_LLM`` already refuses
to publish that text, so the generation stage must not produce it either.
"""

from __future__ import annotations

from types import SimpleNamespace

from tgfarm.config import Persona
from tgfarm.core import llm_health
from tgfarm.s3_router import ChannelPlan
from tgfarm.s4_generation import plans_after_llm_gate


def _plan(platform: str, channel_id: str | None = None):
    return SimpleNamespace(platform=platform, channel_id=channel_id or f"ch_{platform}")


def test_dzen_plans_are_dropped_while_the_llm_is_outage():
    plans = [_plan("vk"), _plan("dzen"), _plan("telegram")]
    gated = plans_after_llm_gate(plans, requires_llm=True, llm_ok=False)
    assert [plan.platform for plan in gated] == ["vk", "telegram"]


def test_dzen_plans_survive_when_the_llm_answers():
    plans = [_plan("vk"), _plan("dzen")]
    assert plans_after_llm_gate(plans, requires_llm=True, llm_ok=True) == plans


def test_gate_is_off_when_the_flag_is_disabled():
    plans = [_plan("dzen")]
    assert plans_after_llm_gate(plans, requires_llm=False, llm_ok=False) == plans


def test_other_platforms_are_never_dropped():
    plans = [_plan("vk"), _plan("telegram"), _plan("max")]
    assert plans_after_llm_gate(plans, requires_llm=True, llm_ok=False) == plans


def test_empty_plans_are_safe():
    assert plans_after_llm_gate([], requires_llm=True, llm_ok=False) == []


def test_the_real_plan_object_carries_the_platform_the_gate_reads():
    """`ChannelPlan` must have the field, not just the stand-ins above.

    The gate reads `plan.platform`; every unit test here passed a namespace that
    already had it, which is how the field stayed missing on the real dataclass
    while the gate silently dropped nobody.
    """
    persona = Persona(
        id="zen", title="Дзен", niche="business", min_chars=150, max_chars=700, platform="dzen"
    )
    plan = ChannelPlan(
        cluster_public_id="c1",
        channel_id="zen",
        persona=persona,
        angle="news",
        score=0.5,
        platform=persona.platform,
    )
    assert plan.platform == "dzen"
    assert plans_after_llm_gate([plan], requires_llm=True, llm_ok=False) == []


def test_recently_working_true_on_empty_history(monkeypatch):
    # Cold start: no drafts at all must not be read as an outage.
    class _Empty:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, stmt):
            return self

        def scalars(self):
            return self

        def all(self):
            return []

    monkeypatch.setattr(llm_health, "session_scope", lambda: _Empty())
    assert llm_health.llm_recently_working() is True


def test_recently_working_reads_draft_provenance(monkeypatch):
    class _Rows:
        def __init__(self, rows):
            self._rows = rows

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, stmt):
            return self

        def scalars(self):
            return self

        def all(self):
            return self._rows

    monkeypatch.setattr(
        llm_health, "session_scope", lambda: _Rows(["offline", "offline"])
    )
    assert llm_health.llm_recently_working() is False

    monkeypatch.setattr(
        llm_health, "session_scope", lambda: _Rows(["offline", "compatible"])
    )
    assert llm_health.llm_recently_working() is True
