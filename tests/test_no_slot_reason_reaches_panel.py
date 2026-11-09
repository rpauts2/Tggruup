"""The builder's two no-slot causes must reach the one reader who acts on them.

`tgfarm/editorial_plan.py` writes `payload.no_slot_reason` — `horizon_occupied`
when a later minute exists, `no_legal_hours` when the channel's window admits no
minute at all — and `tgfarm/plan_execution.py` already holds the entry under that
distinct code. The panel was the last hop and it dropped the fact twice: a
`schedule` entry with no slot printed «по готовности», which reads as "this will
publish as soon as it is ready" when the consumer will in fact hold it forever,
and the two causes were indistinguishable even where they were shown at all.

The two causes ask the operator for opposite actions — «поставить позже» versus
«настроить часы канала» — so this file pins three things:

* the panel renders each cause as its own sentence, and an unrecognised cause as
  its own verbatim code rather than borrowing a label we do understand;
* a `schedule` entry without a cause is said to have no recorded cause, never
  «по готовности» and never a guessed one;
* `produce` entries keep «по готовности», because for them the phrase is true.

Rendering is run for real under node against the file the browser fetches, so
only the produced string can tell a sentence from a token.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
UI_DIR = ROOT / "tgfarm" / "ui"
API_PY = ROOT / "tgfarm" / "api.py"

HELPERS_START = "  const $ = (sel, root = document) => root.querySelector(sel);"
HELPERS_END = "  // The admin API can force-publish"
PLAN_START = "  function renderEditorialPlan(plan, history) {"
PLAN_END = "  function renderDistributionPlan(payload) {"

NODES = ["#plan-meta", "#plan-summary", "#plan-entries", "#plan-history"]

HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const spec = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
function slice(pair) {
  const head = src.indexOf(pair[0]);
  if (head < 0) throw new Error("anchor missing: " + pair[0]);
  const tail = src.indexOf(pair[1], head);
  if (tail < 0) throw new Error("end anchor missing after start: " + pair[1]);
  return src.slice(head, tail);
}
const body = spec.slices.map(slice).join("\n");
const nodes = {};
for (const id of spec.ids) nodes[id] = { innerHTML: "", textContent: "" };
const document = {
  createElement: () => ({ style: {}, classList: { add() {}, remove() {} } }),
  addEventListener: () => {},
  querySelector: (sel) => (sel in nodes ? nodes[sel] : null),
};
const render = new Function("document", "window", body + "\n; return renderEditorialPlan;")(
  document,
  {},
);
render(spec.plan, spec.history);
process.stdout.write(JSON.stringify({ entries: nodes["#plan-entries"].innerHTML,
                                      meta: nodes["#plan-meta"].textContent }));
"""

SLICES = [
    [HELPERS_START, HELPERS_END],
    [PLAN_START, PLAN_END],
]


@pytest.fixture(scope="module")
def script() -> str:
    """Fetched the way a browser fetches it, so a drifted path cannot hide."""

    from tgfarm.api import create_app

    client = TestClient(create_app(with_scheduler=False), base_url="http://127.0.0.1")
    return client.get("/ui/app.js").text


def _render(tmp_path: Path, plan: dict) -> dict[str, str]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node недоступен — поведение панели не проверить")
    harness = tmp_path / "plan_harness.cjs"
    harness.write_text(HARNESS, encoding="utf-8")
    spec_path = tmp_path / "plan_case.json"
    spec_path.write_text(
        json.dumps({"slices": SLICES, "ids": NODES, "plan": plan, "history": []},
                   ensure_ascii=False),
        encoding="utf-8",
    )
    app_js = tmp_path / "app.js"
    app_js.write_text((UI_DIR / "app.js").read_text(encoding="utf-8"), encoding="utf-8")
    done = subprocess.run(  # noqa: S603 - the interpreter and two files in tmp_path
        [node, str(harness), str(app_js), str(spec_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def _entry(**overrides) -> dict:
    entry = {
        "cluster_public_id": "cl-panel",
        "title": "Тема",
        "niche": "tech",
        "action": "schedule",
        "platform": "telegram",
        "channel_id": "ch-1",
        "channel": "Основной",
        "format": "short",
        "priority": 1,
        "confidence": 0.5,
        "reasons": ["температура 1.0"],
        "slot_at": None,
        "payload": {"needs_media": True, "channel_title": "Основной"},
        "outcome": {"state": "not_yet", "post_status": "gated",
                    "scheduled_at": None, "checked_at": "2026-11-09T00:00:00+00:00"},
    }
    entry.update(overrides)
    return entry


def _plan(entries: list[dict]) -> dict:
    return {
        "public_id": "pl0panel",
        "created_at": "2026-11-09T00:00:00+00:00",
        "horizon_days": 7,
        "status": "active",
        "summary": {"total": len(entries), "produce": 0, "schedule": len(entries), "hold": 0},
        "entries": entries,
    }


def test_the_two_causes_render_as_two_different_sentences(tmp_path):
    rendered = _render(tmp_path, _plan([
        _entry(cluster_public_id="cl-horizon", payload={
            "needs_media": True, "no_slot_reason": "horizon_occupied"}),
        _entry(cluster_public_id="cl-hours", payload={
            "needs_media": True, "no_slot_reason": "no_legal_hours"}),
    ]))
    entries = rendered["entries"]

    assert "горизонт занят" in entries and "освободится позже" in entries
    assert "нет легальных часов" in entries
    # The two sentences are the point: one string for both is the defect.
    assert entries.count("слот не забронирован") == 2, entries
    assert "по готовности" not in entries, (
        "a `schedule` entry with no booked minute is held by the consumer; "
        "«по готовности» promises a publication that will not happen"
    )


def test_an_unknown_cause_keeps_its_own_code(tmp_path):
    rendered = _render(tmp_path, _plan([
        _entry(payload={"needs_media": True, "no_slot_reason": "some_new_cause"}),
    ]))
    entries = rendered["entries"]
    assert "some_new_cause" in entries, "an unrecognised cause must stay readable as itself"
    assert "горизонт занят" not in entries and "легальных часов" not in entries


def test_a_schedule_entry_without_a_cause_does_not_guess_one(tmp_path):
    rendered = _render(tmp_path, _plan([
        _entry(payload={"needs_media": True}),
        _entry(cluster_public_id="cl-null", payload=None),
    ]))
    entries = rendered["entries"]
    assert entries.count("причина не записана") == 2, entries
    assert "горизонт занят" not in entries and "легальных часов" not in entries


def test_produce_entries_keep_the_phrase_that_is_true_for_them(tmp_path):
    rendered = _render(tmp_path, _plan([
        _entry(action="produce", slot_at=None,
               payload={"needs_media": True, "no_slot_reason": None}),
    ]))
    assert "по готовности" in rendered["entries"], (
        "a produce entry is not waiting for a booked minute: it has none by design"
    )


def test_a_booked_minute_is_still_a_moment_on_the_clock(tmp_path):
    rendered = _render(tmp_path, _plan([
        _entry(slot_at="2026-11-10T09:30:00+00:00",
               payload={"needs_media": True, "no_slot_reason": "horizon_occupied"}),
    ]))
    entries = rendered["entries"]
    assert "слот не забронирован" not in entries, (
        "the cause is a leftover of the build, not a statement about a booked entry"
    )
    # Rendered in the operator's own zone, so only the fact of a date is pinned.
    assert "10.11.2026" in entries, entries


def test_the_server_actually_sends_the_cause_with_each_schedule_entry():
    """The seam: a label the panel can render is worthless if nothing produces it."""

    source = API_PY.read_text(encoding="utf-8")
    assert '"/api/editorial/plan"' in source
    from tgfarm import editorial_plan

    builder = Path(editorial_plan.__file__).read_text(encoding="utf-8")
    assert '"no_slot_reason": no_slot_reason' in builder, (
        "the builder stopped writing the cause the panel now renders"
    )
    assert '"horizon_occupied" if legal is not None else "no_legal_hours"' in builder
