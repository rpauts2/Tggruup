"""UI contract for the plan-execution block.

`tgfarm/plan_execution.py` has been in the tree for a week and it is still, by
default, dead code: `PLAN_EXECUTION_ENABLED` is off, so `pipeline.py` registers
neither `editorial-plan-build` nor `editorial-plan-apply`, and `apply_plan`
returns before it reads a plan. An operator looking at the plan tab cannot tell
that apart from a consumer that runs and finds nothing to do — the plan chips
show the same totals either way.

`GET /api/editorial/plan/execution` answers the question, and this file pins the
two properties the rest of the panel keeps failing at:

* a value that cannot be measured renders as unknown, never as a healthy zero —
  `registered: null` is the API process being blind to the scheduler, an absent
  `last_apply` is a consumer that never ran, and neither may print «0»;
* every string in that payload is data the API echoed out of stored plan text,
  so it reaches the DOM through `esc()`.

Slicing is done on the file the browser actually fetches (`/ui/app.js`), and the
rendering is run for real under node: only the produced string can tell
«неизвестно» from a printed zero.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

UI_DIR = Path(__file__).resolve().parent.parent / "tgfarm" / "ui"
APP_CSS = UI_DIR / "app.css"

# Anchors in full: a short reused anchor silently slices the wrong body, and the
# first half of this file is the plan block another agent owns.
HELPERS_START = "  const $ = (sel, root = document) => root.querySelector(sel);"
HELPERS_END = "  // The admin API can force-publish"
EXEC_START = "  // ── Исполнитель плана: работает ли потребитель и что он уже сделал ───────"
EXEC_RENDER_END = "  async function loadPlanExecution() {"
EXEC_END = "  function renderDistributionPlan(payload) {"
LOAD_PLAN_START = "  async function loadEditorialPlan(force = false) {"
LOAD_PLAN_END = "  function renderEditorialPlan(plan, history) {"
EXEC_LOAD_START = "  async function loadPlanExecution() {"
EXEC_LOAD_END = "  function renderDistributionPlan(payload) {"

ROOT_ID = "#plan-execution"


@pytest.fixture(scope="module")
def client():
    from tgfarm.api import create_app

    with TestClient(create_app(with_scheduler=False), base_url="http://127.0.0.1") as test_client:
        yield test_client


@pytest.fixture(scope="module")
def script(client) -> str:
    """Fetched the way the browser fetches it, so a drifted path cannot hide."""
    return client.get("/ui/app.js").text


def _slice(text: str, start: str, end: str) -> str:
    assert text.count(start) == 1, f"якорь встречается не один раз: {start!r}"
    head = text.index(start)
    assert end in text[head:], f"конечный якорь не найден после start: {end!r}"
    return text[head : text.index(end, head)]


@pytest.fixture(scope="module")
def block(script) -> str:
    return _slice(script, EXEC_START, EXEC_RENDER_END)


@pytest.fixture(scope="module")
def loader(script) -> str:
    return _slice(script, EXEC_LOAD_START, EXEC_LOAD_END)


@pytest.fixture(scope="module")
def plan_loader(script) -> str:
    return _slice(script, LOAD_PLAN_START, LOAD_PLAN_END)


# ------------------------------------------------------------------- wiring
def test_the_execution_report_is_read_when_the_plan_view_opens(script, plan_loader, loader):
    assert 'api("/api/editorial/plan/execution")' in loader
    assert "renderPlanExecution(await api(\"/api/editorial/plan/execution\"), null);" in loader
    assert "renderPlanExecution(null, error);" in loader, (
        "an unreadable answer must reach the renderer as a failure, not as an empty block"
    )
    assert "loadPlanExecution();" in plan_loader
    assert plan_loader.index("loadPlanExecution();") < plan_loader.index(
        'api("/api/editorial/plan")'
    ), "a dead plan read may not swallow the consumer's own answer"


def test_the_block_has_its_own_mount_and_its_own_legend(client):
    html = client.get("/").text
    assert 'id="plan-execution"' in html
    assert html.index("plan-summary") < html.index("plan-execution") < html.index("plan-entries")
    assert "«неизвестно» здесь — это не «выключен» и не «ноль»" in html


def test_the_classes_the_block_emits_are_already_styled(block, loader):
    """No new stylesheet: an unstyled class name is an invisible change."""
    sheet = APP_CSS.read_text(encoding="utf-8")
    used = set()
    for fragment in (block, loader):
        for match in re.finditer(r'class="([a-z0-9 -]+)"', fragment):
            used.update(match.group(1).split())
    assert used, "the block emits no markup the test can check"
    for token in used:
        assert f".{token}" in sheet, f"{token} is rendered but never styled"


# ----------------------------------------------------------- consumer state
def test_the_consumer_word_comes_only_from_enabled(block):
    assert "data.enabled === true" in block
    assert "data.enabled === false" in block
    assert '<span class="tag tag-ok">включён</span>' in block
    assert '<span class="tag tag-warn">выключен</span>' in block
    # The third branch is not decoration: a payload without the field is unread.
    assert '<span class="tag tag-none">${esc(EXECUTION_UNKNOWN)}</span>' in block


def test_flag_off_states_the_consequence_and_names_the_flag(block):
    assert "PLAN_EXECUTION_ENABLED" in block
    assert "не регистрируются" in block
    assert "останутся пустыми" in block
    # Telling the operator what the flag does, not how to restart the farm.
    for instruction in ("systemctl", "docker", "sudo", "kill", "restart"):
        assert instruction not in block


def test_the_block_reads_the_flag_and_never_sets_it(block):
    """The panel reports a configured setting; a control here would be a second,
    unauthenticated way to start applying a plan."""
    for control in ("<button", "onclick", "addEventListener", "type=", "<input", "POST"):
        assert control not in block, f"{control} turns a report into a control"


# ----------------------------------------------------------------- the jobs
def test_an_unregistered_job_and_an_unseen_job_keep_different_words(block):
    assert 'const EXECUTION_UNKNOWN = "неизвестно";' in block
    assert "v === true ? \"зарегистрировано\"" in block
    assert "v === false ? \"не зарегистрировано\" : EXECUTION_UNKNOWN" in block, (
        "`registered: null` is the API process being blind to the scheduler; "
        "folding it into `не зарегистрировано` reports a verdict nobody measured"
    )


def test_a_missing_next_run_is_a_dash_not_a_date(block):
    guard = block[block.index("const nextRunCell") : block.index("const execCount")]
    assert 'typeof v !== "string"' in guard
    assert "dt(v)" in guard
    assert '"—"' in guard
    assert "Date.parse(v)" in guard, "an unparseable value must not reach Date"
    assert "new Date(v)" not in guard
    assert "|| 0" not in guard and "?? 0" not in guard


def test_an_unread_job_list_admits_it_is_unread(block):
    assert "список задач не прочитан" in block
    assert "Array.isArray(data.jobs)" in block


# ------------------------------------------------------------- last apply
def test_a_consumer_that_never_ran_is_said_plainly(block):
    guard = block[block.index("function execLastApplyBlock") : block.index("function execPlanBlock")]
    assert "ни одного пропуска" in guard
    assert "!la || typeof la !== \"object\"" in guard
    # The never-ran branch cannot print a count at all.
    assert "execChip(" not in guard[: guard.index("const chips")], (
        "an absent last_apply is not a run that applied zero"
    )


def test_a_payload_without_the_field_is_not_a_consumer_that_never_ran(block):
    """`last_apply: null` is a verdict from the server; a missing key is silence."""
    assert '"last_apply" in data' in block
    assert "отчёт о пропуске не прочитан" in block


def test_applied_and_held_are_rendered_only_behind_a_type_guard(block):
    assert 'execChip("исполнено", execCount(la.applied))' in block
    assert 'execChip("удержано", execCount(la.held))' in block
    count = block[block.index("const execCount") : block.index("const execChip")]
    assert 'typeof v === "number"' in count
    assert "Number.isFinite(v)" in count
    assert '"неизвестно"' in count or "EXECUTION_UNKNOWN" in count
    for coerced in ("|| 0", "?? 0", "Number(la.applied", "Number(la.held"):
        assert coerced not in block, f"{coerced} re-fabricates the zero the API refuses to send"


def test_the_block_never_hands_a_nullable_field_to_the_number_formatter(block):
    """`n()` ends in `|| 0`, and the helpers say so: a null handed to it prints a
    measured nothing. One call site in this block may reach it, behind a guard."""
    assert len(re.findall(r"(?<![A-Za-z0-9_$.])n\(", block)) == 1, (
        "execCount is the only place in this block allowed to format a number"
    )
    count = block[block.index("const execCount") : block.index("const execChip")]
    assert "n(v)" in count
    assert 'typeof v === "number"' in count and "Number.isFinite(v)" in count


def test_reasons_absent_and_reasons_empty_keep_different_words(block):
    guard = block[block.index("function execLastApplyBlock") : block.index("function execPlanBlock")]
    assert "причины не записаны" in guard
    assert "ни одной причины в отчёте" in guard
    assert "la.reasons && typeof la.reasons === \"object\"" in guard


def test_the_reason_histogram_is_escaped(block):
    histogram = block[block.index("function execReasonChips") : block.index("function execJobChips")]
    assert "Object.entries(reasons)" in histogram
    # The count always goes through the measured-number formatter, and the label
    # is chosen from a closed map — never interpolated from the server's key.
    assert "execCount(count)" in histogram
    assert "EXEC_REASON[code]" in histogram, (
        "the code must be looked up in the panel's own vocabulary, not printed as a sentence"
    )
    chip = block[block.index("const execChip") : block.index("function execReasonChips")]
    assert "${esc(label)}: ${esc(value)}" in chip, (
        "a reason code is stored plan text: an unescaped key is the XSS hole"
    )
    for raw in ("${code}", "${count}", "${key}", "${value}", "${label}"):
        assert raw not in block, f"raw interpolation of {raw!r}"


def test_both_no_slot_causes_are_named_and_kept_apart(block):
    """The builder holds a `schedule` entry for two opposite reasons.

    `horizon_occupied` means "try later", `no_legal_hours` means "the channel's
    window is the problem" — and until the cause was carried into the panel both
    printed the same friendly phrase. An unknown cause keeps its verbatim code,
    because a code nobody agreed on may not borrow a label we do understand.
    """
    labels = block[block.index("const EXEC_REASON") : block.index("function execReasonChips")]
    for code in ("horizon_occupied", "no_legal_hours", "no_slot_planned"):
        assert f"{code}:" in labels, code
    assert "освободится позже" in labels and "нет легальных часов" in labels
    assert "EXEC_REASON[code]" in block
    # Every code the executor can emit has a sentence, or the panel is a token list.
    from tgfarm import plan_execution

    emitted = {
        value
        for name, value in vars(plan_execution).items()
        if name.startswith("REASON_") and isinstance(value, str)
    }
    assert emitted, "no reason codes found in tgfarm/plan_execution.py"
    missing = sorted(code for code in emitted if f"{code}:" not in labels)
    assert not missing, f"codes with no sentence in the panel: {missing}"


def test_the_plan_sub_block_is_omitted_when_there_is_no_plan(block):
    plan = block[block.index("function execPlanBlock") : block.index("function renderPlanExecution")]
    assert 'if (!pl || typeof pl !== "object") return "";' in plan
    for field in ("pl.total", "pl.produce", "pl.schedule", "pl.hold", "pl.coverage"):
        assert field in plan
    assert "execCount(pl.total)" in plan


def test_the_note_and_the_failure_are_text_never_markup(block, loader):
    assert "esc(data.note)" in block
    assert "typeof data.note === \"string\"" in block
    assert "esc(reason)" in block
    for raw in ("${data.note}", "${failure}", "${reason}", "+ data.note"):
        assert raw not in block and raw not in loader, f"raw interpolation of {raw!r}"


# ------------------------------------------------- the neighbouring block lives
def test_the_plan_coverage_block_was_not_moved(script):
    """W18-B and W18-C both read `summary.coverage`; the anchors are their seam."""
    body = _slice(script, LOAD_PLAN_END, "  function renderDistributionPlan(payload) {")
    assert "Исполнено" in body
    assert "исход: " in body
    assert "const coverageChips" in body
    assert "? []" in body[body.index("const coverageChips") : body.index("const chipItems")]
    assert "const OUTCOME_LABELS" in body
    assert "summary.coverage_note" in body


# ------------------------------------------------------------------ real render
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
const document = {
  createElement: () => ({ style: {}, classList: { add() {}, remove() {} } }),
  addEventListener: () => {},
  querySelector: () => null,
};
const render = new Function("document", "window", body + "\n; return renderPlanExecution;")(
  document,
  {},
);
const out = [];
for (const item of spec.cases) {
  const nodes = {};
  for (const id of item.ids) {
    nodes[id] = { innerHTML: "", textContent: "" };
  }
  document.querySelector = (sel) => (sel in nodes ? nodes[sel] : null);
  render(item.payload, item.failure ? new Error(item.failure) : null);
  out.push({ name: item.name, nodes });
}
process.stdout.write(JSON.stringify(out));
"""

SLICES = [
    [HELPERS_START, HELPERS_END],
    [EXEC_START, EXEC_RENDER_END],
]


def _render_cases(tmp_path: Path, cases: list[dict]) -> dict[str, str]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node недоступен — поведение панели не проверить")
    harness = tmp_path / "harness.cjs"
    harness.write_text(HARNESS, encoding="utf-8")
    spec_path = tmp_path / "cases.json"
    spec_path.write_text(
        json.dumps(
            {"slices": SLICES, "cases": [
                {"name": case["name"], "ids": [ROOT_ID],
                 "payload": case.get("payload"), "failure": case.get("failure")}
                for case in cases
            ]},
            ensure_ascii=False,
        ),
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
    return {item["name"]: item["nodes"][ROOT_ID]["innerHTML"] for item in json.loads(done.stdout)}


def _execution(**overrides) -> dict:
    payload = {
        "enabled": True,
        "jobs": [
            {"id": "editorial-plan-build", "registered": True,
             "next_run_at": "2026-11-05T09:00:00+00:00"},
            {"id": "editorial-plan-apply", "registered": True,
             "next_run_at": "2026-11-05T09:15:00+00:00"},
        ],
        "last_apply": {
            "at": "2026-11-05T08:15:00+00:00",
            "plan_id": "pl0aaaabbbb",
            "applied": 3,
            "held": 2,
            "reasons": {"slot_not_due": 2, "daily_cap": 1},
        },
        "plan": {
            "id": "pl0aaaabbbb",
            "created_at": "2026-11-05T06:00:00+00:00",
            "total": 18,
            "produce": 5,
            "schedule": 9,
            "hold": 4,
            "coverage": {"measured": 18, "materialized": 3},
        },
        "note": "последний пропуск выполнен штатно",
    }
    payload.update(overrides)
    return payload


def test_rendered_dead_consumer_says_dead_and_shows_no_counts(tmp_path):
    dead = _execution(
        enabled=False,
        jobs=[
            {"id": "editorial-plan-build", "registered": None, "next_run_at": None},
            {"id": "editorial-plan-apply", "registered": None, "next_run_at": None},
        ],
        last_apply=None,
        plan=None,
        note="флаг PLAN_EXECUTION_ENABLED выключен",
    )
    html = _render_cases(tmp_path, [{"name": "off", "payload": dead}])["off"]

    assert 'class="tag tag-warn">выключен' in html
    assert "Флаг выключен — задачи исполнителя не регистрируются" in html
    assert "PLAN_EXECUTION_ENABLED не включат" in html
    assert "не зарегистрировано" not in html, "an unseen scheduler is not an absent job"
    assert "editorial-plan-build: неизвестно" in html
    assert "editorial-plan-apply · далее —" in html
    assert "ни одного пропуска" in html
    assert "исполнено" not in html and "удержано" not in html
    assert "0" not in html, "a switched-off consumer prints no counts, measured or invented"
    assert "флаг PLAN_EXECUTION_ENABLED выключен" in html


def test_rendered_full_report_prints_the_measured_numbers(tmp_path):
    html = _render_cases(tmp_path, [{"name": "full", "payload": _execution()}])["full"]

    assert 'class="tag tag-ok">включён' in html
    assert "editorial-plan-build: зарегистрировано" in html
    assert "editorial-plan-apply: зарегистрировано" in html
    assert "далее " in html and "далее —" not in html
    assert "исполнено: 3" in html
    assert "удержано: 2" in html
    assert "по плану: pl0aaaabbbb" in html
    assert "slot_not_due: 2" in html and "daily_cap: 1" in html
    assert "всего тем: 18" in html and "в работу: 5" in html
    assert "в расписание: 9" in html and "отложено: 4" in html
    assert "measured: 18" in html
    assert "последний пропуск выполнен штатно" in html
    assert "выключен" not in html and "ни одного пропуска" not in html


def test_rendered_measured_nothing_is_a_zero_and_an_unread_count_is_not(tmp_path):
    rendered = _render_cases(tmp_path, [
        {"name": "quiet", "payload": _execution(
            last_apply={"at": "2026-11-05T08:15:00+00:00", "plan_id": "pl0aaaabbbb",
                        "applied": 0, "held": 0, "reasons": {}})},
        {"name": "unread", "payload": _execution(
            last_apply={"at": None, "plan_id": None,
                        "applied": None, "held": None, "reasons": None})},
    ])

    assert "исполнено: 0" in rendered["quiet"] and "удержано: 0" in rendered["quiet"]
    assert "ни одной причины в отчёте" in rendered["quiet"]
    assert "неизвестно" not in rendered["quiet"]
    assert "исполнено: неизвестно" in rendered["unread"]
    assert "удержано: неизвестно" in rendered["unread"]
    assert "пропуск: неизвестно" in rendered["unread"]
    assert "причины не записаны" in rendered["unread"]
    assert "исполнено: 0" not in rendered["unread"], "an unread count is not a measured nothing"
    assert "по плану: неизвестно" in rendered["unread"]


def test_rendered_blind_scheduler_and_absent_plan_are_not_renamed(tmp_path):
    html = _render_cases(tmp_path, [{
        "name": "blind",
        "payload": _execution(
            jobs=[{"id": "editorial-plan-apply", "registered": False, "next_run_at": None}],
            plan=None,
        ),
    }])["blind"]

    assert "editorial-plan-apply: не зарегистрировано" in html
    assert "editorial-plan-apply · далее —" in html
    assert "неизвестно" not in html.replace("пропуск: неизвестно", ""), html
    assert "всего тем" not in html, "no plan in the answer means no plan sub-block"


def test_the_three_job_states_render_three_different_words(tmp_path):
    payload = _execution(
        jobs=[
            {"id": "editorial-plan-build", "registered": True,
             "next_run_at": "2026-11-05T09:00:00+00:00"},
            {"id": "editorial-plan-apply", "registered": False, "next_run_at": None},
            {"id": "editorial-plan-other", "registered": None, "next_run_at": None},
        ],
        last_apply={"at": "2026-11-05T08:15:00+00:00", "plan_id": None,
                    "applied": 0, "held": None, "reasons": {"slot_not_due": None}},
    )
    html = _render_cases(tmp_path, [{"name": "three", "payload": payload}])["three"]

    assert "editorial-plan-build: зарегистрировано" in html
    assert "editorial-plan-apply: не зарегистрировано" in html
    assert "editorial-plan-other: неизвестно" in html
    assert "editorial-plan-apply · далее —" in html
    # A measured nothing and an unread number survive in the same render.
    assert "исполнено: 0" in html and "удержано: неизвестно" in html
    assert "slot_not_due: неизвестно" in html and "slot_not_due: 0" not in html
    assert "по плану: неизвестно" in html


def test_a_run_scheduled_ahead_is_not_reported_as_a_run_that_just_happened(tmp_path):
    """`dt()` answers "только что" for any future instant: the elapsed time is
    negative, and every negative number is under a minute. A job that has not run
    yet may not read as a job that just ran, and an unparseable moment stays "—"."""
    payload = _execution(jobs=[
        {"id": "editorial-plan-build", "registered": True,
         "next_run_at": "2096-11-05T09:00:00+00:00"},
        {"id": "editorial-plan-apply", "registered": True, "next_run_at": "не дата"},
    ])
    html = _render_cases(tmp_path, [{"name": "ahead", "payload": payload}])["ahead"]

    assert "только что" not in html
    labels = re.findall(r"· далее ([^<]+)", html)
    assert len(labels) == 2, labels
    assert labels[0] != "—" and ":" in labels[0], f"a booked run needs a moment: {labels[0]!r}"
    assert labels[1] == "—", "an unparseable instant is an unread moment, not a broken one"


def test_rendered_unread_answer_admits_it_measured_nothing(tmp_path):
    partial = _execution()
    del partial["last_apply"]
    rendered = _render_cases(tmp_path, [
        {"name": "network", "payload": None, "failure": "Failed to fetch"},
        {"name": "http", "payload": None, "failure": "/api/editorial/plan/execution -> 404"},
        {"name": "empty", "payload": {}},
        {"name": "partial", "payload": partial},
        {"name": "nullflag", "payload": _execution(enabled=None)},
    ])

    assert "Failed to fetch" in rendered["network"]
    # An unread answer may not claim a consumer state. The disclaimer legitimately
    # *quotes* «выключен» to say which claim it refuses to make, so the check is on
    # the state markup the block emits, not on the word as a substring.
    for name in ("network", "http"):
        assert 'class="tag tag-ok"' not in rendered[name], f"{name} claimed `включён`"
        assert 'class="tag tag-warn"' not in rendered[name], f"{name} claimed `выключен`"
        assert "потребитель плана: неизвестно" in rendered[name]
    assert "404" in rendered["http"]
    assert "Это не «выключен» и не «ничего не исполнено»" in rendered["http"]
    for name, html in rendered.items():
        assert "исполнено: 0" not in html, f"{name} invented a measured nothing"
        assert "ни одного пропуска" not in html, f"{name} claimed a verdict about the run"
    # A payload that carries the consumer but not its report says exactly that.
    assert "отчёт о пропуске не прочитан" in rendered["partial"]
    assert "editorial-plan-apply: зарегистрировано" in rendered["partial"]
    # A payload without `enabled` is not a payload saying the flag is off.
    assert 'class="tag tag-none">неизвестно' in rendered["empty"]
    # A null `enabled` is the same silence, not a verdict of `выключен`.
    assert 'class="tag tag-none">неизвестно' in rendered["nullflag"]
    assert 'class="tag tag-warn"' not in rendered["nullflag"]
    assert "Флаг выключен" not in rendered["nullflag"]
    # ... while the parts the server did answer stay readable.
    assert "исполнено: 3" in rendered["nullflag"]


def test_a_hostile_note_and_a_hostile_reason_key_stay_text(tmp_path):
    hostile = '<img src=x onerror="fetch(/evil)">'
    payload = _execution(
        note=hostile,
        last_apply={"at": "2026-11-05T08:15:00+00:00", "plan_id": hostile,
                    "applied": 1, "held": 1, "reasons": {hostile: 1}},
        plan={"id": hostile, "created_at": None, "total": None, "produce": None,
              "schedule": None, "hold": None, "coverage": {hostile: None}},
    )
    html = _render_cases(tmp_path, [{"name": "hostile", "payload": payload}])["hostile"]

    assert hostile not in html
    assert "&lt;img src=x onerror=&quot;fetch(/evil)&quot;&gt;" in html
    assert html.count("<img") == 0, "no image tag may be built out of stored plan text"
    assert "&lt;img" in html, "the hostile text is shown, as data"
    assert "исполнено: 1" in html and "удержано: 1" in html, "escaping is not hiding"


def test_rendered_failure_is_a_message_and_not_markup(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node недоступен — поведение панели не проверить")
    html = _render_cases(tmp_path, [{
        "name": "leak",
        "payload": None,
        "failure": '<b>авария</b> чтения',
    }])["leak"]

    assert "<b>авария</b>" not in html
    assert "&lt;b&gt;авария&lt;/b&gt;" in html


# --------------------------------------------------------------------- style
def test_the_script_is_still_parseable_javascript():
    node = shutil.which("node")
    if not node:  # pragma: no cover - CI installs node
        pytest.skip("node is not on PATH")
    result = subprocess.run(  # noqa: S603
        [node, "--check", str(UI_DIR / "app.js")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
