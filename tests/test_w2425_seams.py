"""Seams: landed code is only a feature once the running process reaches it.

Wave 24/25 shipped four packages whose implementations were accepted while their
call sites stayed with the coordinator: the per-stage LLM budget (the stage loop
never named the stage), the offline provenance pair (S4 copied it through two
conditionals), the build identity (nothing asked for it), and the resilience
state (the panel could not see a breaker that was open). A module that nothing
calls passes every import-level check and changes no behaviour, so each test
here asks the running object — the pipeline wrapper, the CLI dispatcher, the
serve path, the served endpoint — rather than the definition it sits on.
"""

from __future__ import annotations

import argparse
import json
import logging
import uuid

import pytest

from tgfarm import cli
from tgfarm.core import settings as settings_mod


def _run_id() -> str:
    return "w2425-" + uuid.uuid4().hex[:10]


# ------------------------------------------------------------------ stage identity
@pytest.mark.asyncio
async def test_a_stage_run_by_the_pipeline_owns_its_llm_spend() -> None:
    """`_stage` must name the stage it is running, or the budget has no accounts.

    The per-stage call budget charges whatever stage is ambient. Without the
    wrapper every online call in the farm lands in one shared bucket, so a stage
    that burned its allowance could not be told from a stage that never spent,
    and the refusal message would name the wrong victim.
    """
    from tgfarm import pipeline
    from tgfarm.llm.client import CURRENT_STAGE

    seen: list[str] = []

    async def stage_body() -> dict:
        seen.append(CURRENT_STAGE.get())
        return {}

    result = pipeline.CycleResult(run_id=_run_id())
    await pipeline._stage(result, "s4_generate", stage_body)

    assert seen == ["s4_generate"], "the stage loop never published its own name"
    assert CURRENT_STAGE.get() == "unknown", "the stage name leaked past the stage"


def test_a_caller_outside_any_stage_is_still_bounded() -> None:
    """`unknown` is an account, not an exemption.

    Work that bypasses `_stage` — a CLI verb, a manual API call — has no stage
    to blame, and the budget must still count it. The default is the bucket that
    does that; a default of `None` or an empty string would mean unbounded.
    """
    from tgfarm.llm import resilience as res
    from tgfarm.llm.client import CURRENT_STAGE

    assert CURRENT_STAGE.get() == "unknown"
    res.reset_resilience_state()
    try:
        assert res.BUDGET.try_spend(CURRENT_STAGE.get(), daily_budget=1) is True
        assert res.BUDGET.try_spend(CURRENT_STAGE.get(), daily_budget=1) is False
        assert res.BUDGET.spent("unknown") == 1
    finally:
        res.reset_resilience_state()


# ----------------------------------------------------------- offline provenance pair
def test_an_offline_response_cannot_carry_a_model_name() -> None:
    """The guarantee S4 now relies on when it copies the pair verbatim.

    S4 used to re-derive `generation_provider`/`generation_model` with two
    conditionals, which is how a second spelling of the same fact could appear.
    Those conditionals were replaced by a plain copy because `LLMResponse`
    corrects the pair in its own constructor. If that correction ever moves,
    this test is what fails first.
    """
    from tgfarm.llm.client import OFFLINE_MODEL, OFFLINE_PROVIDER, LLMResponse

    for model in ("", "none", "gemini-2.5-flash:free"):
        response = LLMResponse("text", OFFLINE_PROVIDER, model, 12)
        assert response.provider == OFFLINE_PROVIDER
        assert response.model == OFFLINE_MODEL, f"model={model!r} survived an offline result"


# ------------------------------------------------------------------------- version
def test_the_version_verb_is_reachable_through_the_parser(monkeypatch, capsys) -> None:
    """`tgfarm version` must dispatch, and must print what the artifact knows.

    The fabricated identity below is a stamped one; the assertion is that the
    verb surfaces the fields an operator asks about, in machine-readable form.
    """
    from tgfarm import buildinfo

    fabricated = {
        "state": buildinfo.STATE_STAMPED,
        "packaging_mode": buildinfo.PACKAGING_PYINSTALLER,
        "source_sha": "0123456789abcdef0123456789abcdef01234567",
        "proves_this_process": True,
        "identity_line": "tgfarm build identity mode=pyinstaller state=stamped sha=0123456789ab",
    }
    monkeypatch.setattr(buildinfo, "describe", lambda **_kwargs: fabricated)

    args = cli.build_parser().parse_args(["version"])
    assert args.func is cli.cmd_version, "`version` is registered but wired to something else"

    assert args.func(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == buildinfo.STATE_STAMPED
    assert payload["proves_this_process"] is True


# ---------------------------------------------------------------------- serve path
def test_serve_announces_its_build_and_still_not_its_token(
    monkeypatch, capsys, tmp_path
) -> None:
    """Two halves of one startup line, tested together because they ship together.

    W24-A stopped `serve` printing the standing admin token; W24-B wants the same
    first lines to answer "which build is this". A build identity that names a
    commit is harmless, and it is only worth logging if the credential next to it
    stayed suppressed — so the sentinel must be absent while the identity is
    present in the log, not merely in the banner.
    """
    import uvicorn

    from tgfarm.core import apiauth

    sentinel = "SENTINEL-build-identity-token-6c1b"
    monkeypatch.setattr(settings_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    monkeypatch.setattr(apiauth.secrets, "token_urlsafe", lambda *a, **k: sentinel)

    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    farm = logging.getLogger("tgfarm")
    previous_level = farm.level
    farm.addHandler(handler)
    farm.setLevel(logging.INFO)
    try:
        assert cli.cmd_serve(argparse.Namespace(host="", port=8124, no_scheduler=True)) == 0
    finally:
        farm.removeHandler(handler)
        farm.setLevel(previous_level)

    captured = capsys.readouterr()
    assert sentinel not in captured.out, "the serve banner prints the token again"
    assert sentinel not in captured.err, "the token moved to stderr"

    logged = [record.getMessage() for record in records]
    identity = [line for line in logged if "tgfarm build identity" in line]
    assert identity, "serve started without ever saying which build it is"
    line = identity[0]
    assert "sha=" in line and "state=" in line, f"identity line is not the contract: {line}"
    assert line.isascii(), "the identity line must survive a cp1252 console"
    assert "proves_process=" in line, "the line must say whether it describes this process"


# ------------------------------------------------------------------------ endpoints
def test_the_panel_can_ask_about_resilience_and_build(tmp_path, monkeypatch) -> None:
    """An open circuit breaker is invisible unless the API says so.

    `/api/metrics` already reported per-provider counters; the failover work
    added two things a counter cannot express — which providers the breaker is
    refusing right now, and how many attempts each stage has spent today. Both
    are asserted here as shape, because their contents are runtime state.
    """
    from fastapi.testclient import TestClient

    from tgfarm.api import create_app
    from tgfarm.core.apiauth import HEADER, ensure_token

    monkeypatch.setattr(settings_mod, "DATA_DIR", tmp_path)
    token = ensure_token()

    with TestClient(
        create_app(with_scheduler=False), base_url="http://127.0.0.1"
    ) as test_client:
        metrics = test_client.get("/api/metrics", headers={HEADER: token})
        build = test_client.get("/api/build", headers={HEADER: token})
        anonymous = test_client.get("/api/build")

    assert metrics.status_code == 200
    resilience = metrics.json()["resilience"]
    assert set(resilience) == {"breaker", "stage_budget"}
    assert isinstance(resilience["breaker"], dict)
    assert {"day", "spent", "refusals"} <= set(resilience["stage_budget"])

    assert build.status_code == 200
    payload = build.json()
    assert payload["state"] in {"stamped", "unstamped", "invalid"}, payload["state"]
    assert "proves_this_process" in payload
    assert anonymous.status_code in (401, 403), "the build endpoint is reachable without a token"
