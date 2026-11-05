"""The plan consumer's endpoint and its panel block must be one contract.

W18-B shipped `GET /api/editorial/plan/execution` and W18-C shipped the block
that renders it, in different worktrees, against a contract the coordinator
wrote — not against each other's code. That is the exact shape of change where a
rename survives review: the server stops sending `last_apply`, the panel keeps
asking for it, and every test on both sides stays green because each only ever
talked to its own half. These tests read both files and refuse that.

Nothing here calls the pipeline or the executor: the endpoint is read over HTTP
against an app with no scheduler, and the panel is read as the text the server
serves. The shared session database is only ever asked for counts it already
has, and no row is written.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tgfarm import plan_execution, s4_generation
from tgfarm.core.apiauth import ensure_token

ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "tgfarm" / "ui" / "app.js"
API_PY = ROOT / "tgfarm" / "api.py"

ENDPOINT = "/api/editorial/plan/execution"

#: Where each half of the seam starts and stops, as literal text.
UI_BLOCK_START = "// ── Исполнитель плана"
UI_BLOCK_END = "async function loadPlanExecution"
API_SECTION_START = "#: The plan consumer's observable state."
API_SECTION_END = f'@app.get("{ENDPOINT}")'

#: The object each prefix in the panel refers to, in the contract's terms.
UI_PREFIXES = {
    "data": "root",
    "job": "jobs[]",
    "la": "last_apply",
    "pl": "plan",
}

CONTRACT_KEYS = {
    "root": {"enabled", "jobs", "last_apply", "plan", "note"},
    "jobs[]": {"id", "registered", "next_run_at"},
    "last_apply": {"at", "plan_id", "applied", "held", "reasons"},
    "plan": {"id", "created_at", "total", "produce", "schedule", "hold", "coverage"},
}


def _slice(source: str, start: str, end: str) -> str:
    """The one block under test, cut out by its own anchors.

    Anchored rather than line-numbered: a line number is a claim about the rest
    of the file, and both of these files are large and actively edited.
    """
    begin = source.index(start)
    stop = source.index(end, begin)
    return source[begin:stop]


def _ui_block() -> str:
    return _slice(APP_JS.read_text(encoding="utf-8"), UI_BLOCK_START, UI_BLOCK_END)


def _api_section() -> str:
    return _slice(API_PY.read_text(encoding="utf-8"), API_SECTION_START, API_SECTION_END)


def _panel_reads(block: str) -> dict[str, set[str]]:
    """Every `prefix.key` the panel dereferences, grouped by which object it is."""
    found: dict[str, set[str]] = {prefix: set() for prefix in UI_PREFIXES}
    for prefix, key in re.findall(
        r"(?<![A-Za-z0-9_$])(data|job|la|pl)\.([a-z][a-z0-9_]*)", block
    ):
        found[prefix].add(key)
    return found


def _payload() -> dict[str, Any]:
    """The real response of a served panel: same process, no scheduler.

    `create_app(with_scheduler=False)` without a `with:` block never runs the
    lifespan, so `pipeline._scheduler` stays unset — which is the state a reader
    must never be allowed to describe as "the jobs are not registered".
    """
    from tgfarm.api import create_app

    client = TestClient(create_app(with_scheduler=False), base_url="http://127.0.0.1",
                        headers={"X-TGFarm-Token": ensure_token()})
    response = client.get(ENDPOINT)
    assert response.status_code == 200, response.text
    return response.json()


def test_both_halves_use_the_one_path_literal() -> None:
    """A path typed twice is a contract that can drift by one character."""
    assert f'@app.get("{ENDPOINT}")' in API_PY.read_text(encoding="utf-8")
    assert f'api("{ENDPOINT}")' in APP_JS.read_text(encoding="utf-8")


def test_the_panel_only_reads_keys_the_contract_declares() -> None:
    """Every object the block touches exists in the contract, key by key.

    Checked against the contract and against the server's own source text, not
    only against one live payload: `last_apply` is legitimately `null` before the
    executor has ever run, and a runtime-only check would pass by skipping it.
    """
    section = _api_section()
    reads = _panel_reads(_ui_block())
    for prefix, path in UI_PREFIXES.items():
        keys = reads[prefix]
        assert keys, f"the panel block no longer reads {path} at all"
        unexpected = keys - CONTRACT_KEYS[path]
        assert not unexpected, f"panel reads {path}.{sorted(unexpected)}: not in the contract"
        for key in keys:
            assert f'"{key}"' in section, (
                f"`{path}.{key}` is rendered by the panel but never produced by the "
                f"endpoint section in tgfarm/api.py — one side renamed a field"
            )


def test_the_live_payload_carries_every_top_level_contract_key() -> None:
    payload = _payload()
    assert set(payload) == CONTRACT_KEYS["root"], sorted(payload)
    assert isinstance(payload["enabled"], bool), "enabled is the configured flag, never unknown here"
    assert isinstance(payload["note"], str) and payload["note"], "the note is required"


def test_the_jobs_are_the_three_the_pipeline_can_register() -> None:
    """The panel lists consumer jobs by name; the names come from one place.

    The third entry is `w21a`'s booking pass. It is listed here rather than in
    that package's own file because the fact under test is the seam: an endpoint
    that knows two of the three jobs a flag-on pipeline registers is an endpoint
    the operator cannot trust to answer «почему план не бронируется».
    """
    payload = _payload()
    assert [job["id"] for job in payload["jobs"]] == [
        plan_execution.PLAN_BUILD_JOB_ID,
        plan_execution.PLAN_APPLY_JOB_ID,
        s4_generation.PLAN_BOOKING_JOB_ID,
    ], payload["jobs"]
    block = _ui_block()
    assert "execJobChips" in block and "job.registered" in block


def test_a_scheduler_this_process_cannot_see_is_not_reported_as_absent() -> None:
    """The whole point of `null` here, pinned on both sides at once."""
    payload = _payload()
    for job in payload["jobs"]:
        assert job["registered"] is None, job
        assert job["next_run_at"] is None, job
    block = _ui_block()
    assert "v === false ? \"не зарегистрировано\" : EXECUTION_UNKNOWN" in block, block[:400]


def test_nullable_fields_are_never_defaulted_into_numbers() -> None:
    """`applied: null` must not reach the operator as `0`.

    The panel's own formatter is allowed to render a measured zero, so the pin is
    on the call sites: exactly one numeric default may exist in the block and it
    may not sit on a consumer field.
    """
    block = _ui_block()
    assert "|| 0" not in block and "?? 0" not in block, block
    for field in ("applied", "held", "total", "produce", "schedule", "hold"):
        assert f"la.{field}" in block or f"pl.{field}" in block, field
        assert not re.search(rf"(la|pl)\.{field} \|\| ", block), field
    assert block.count("EXECUTION_UNKNOWN") >= 4, "unknown vocabulary has shrunk"


def test_unreadable_state_keeps_its_own_wording_apart_from_a_zero() -> None:
    """Three silences, three wordings: never ran, unread, and nothing held."""
    block = _ui_block()
    for phrase in ("ни одного пропуска", "не прочитан", "причины не записаны"):
        assert phrase in block, phrase
    assert '"last_apply" in data' in block, "an absent key was merged into a null one"


def test_stored_text_cannot_break_out_of_the_block() -> None:
    """The endpoint returns stored plan text raw, so the panel is the escape point."""
    block = _ui_block()
    assert "esc(data.note)" in block, "the note is server-authored text"
    interpolations = re.findall(r"\$\{([^}]*)\}", block)
    unsafe = [
        expr for expr in interpolations
        if re.search(r"\b(?:la|pl|job|data)\.[a-z_]+\b", expr)
        and "esc(" not in expr and "execChip" not in expr and "String(" not in expr
    ]
    assert not unsafe, f"unescaped interpolation of server fields: {unsafe}"


def test_the_panel_app_still_parses() -> None:
    """Cheap and total: a block the browser cannot parse renders nothing at all."""
    node = subprocess.run(  # noqa: S603 - fixed executable, no shell
        ["node", "--check", str(APP_JS)],
        capture_output=True,
        text=True,
    )
    if node.returncode != 0 and "not found" in (node.stderr or "").lower():
        pytest.skip("node is not on PATH in this environment")
    assert node.returncode == 0, node.stderr


def test_the_endpoint_answer_is_plain_json_of_the_contract_shape() -> None:
    """The shape the panel slices by is the shape the server actually sends."""
    payload = _payload()
    round_tripped = json.loads(json.dumps(payload))
    assert round_tripped == payload
    if payload["last_apply"] is not None:
        assert set(payload["last_apply"]) == CONTRACT_KEYS["last_apply"], payload["last_apply"]
    assert set(payload["plan"]) == CONTRACT_KEYS["plan"], payload["plan"]
