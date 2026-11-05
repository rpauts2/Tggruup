"""W22-B UI contract: the queue preview and the open-order card say which of four
states a waiting material is in.

Two residuals, both measured on ``dd75c42``:

* ``tgfarm/editorial.py`` has been answering «does this topic's plan-ordered draft
  still wait for a booked slot?» with ``awaiting_booking`` since W20-B
  (`editorial.queue` puts the key on every row, `/api/editorial` serves the rows
  unchanged), and `git grep -c awaiting_booking -- tgfarm/ui/app.js` printed **0**.
  The panel showed an operator nothing: «ждёт слота» and «никогда не был в плане»
  were the same sentence, and the one key the server measures to tell them apart
  had no reader.
* the distribution card rendered `queue_preview` rows as `title / channel / status /
  scheduled_at`. The booking columns are what make a queued post mean something, and
  a preview that cannot read them — the state of the live store today, which has no
  `posts.plan_slot_at` — printed the same four fields as a preview that can.

This file pins the four states the pair needs, and that they stay four:

===========================  =========================================
state                        read from
===========================  =========================================
«по плану»                   `plan_entry_id` names an entry
«не по плану»                the read happened, `plan_entry_id` is null
«ждёт слота плана»           `awaiting_booking` is true
«провенанс не прочитан»      `queue_provenance == "plan_columns_missing"`,
                             or the keys never arrived
===========================  =========================================

The last row is the project's standing rule — unknown is never rendered as a zero
and never as an absence — so the negative case is asserted twice: once from a
fixture, once from the row the endpoint actually serves today.

Slicing follows the convention of the other panel files: the text is fetched as
the browser fetches it (`/ui/app.js`), cut on full-line anchors, and the rendering
is executed under node, because only the produced markup can tell «провенанс не
прочитан» from «не по плану». The key names come from the contract the coordinator
froze for W22-A and are restated here on purpose: the test that goes red when one
side renames is worth more than the comment that explains why they must match.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from tgfarm.core.apiauth import ensure_token
from tgfarm.db.models import Channel, Post
from tgfarm.db.session import session_scope

ROOT = Path(__file__).resolve().parent.parent
UI_DIR = ROOT / "tgfarm" / "ui"
APP_CSS = UI_DIR / "app.css"

#: The frozen W22-A contract: three row keys, one platform key, two codes.
PROVENANCE_ROW_KEYS = ("plan_entry_id", "plan_slot_at", "plan_public_id")
PROVENANCE_ITEM_KEY = "queue_provenance"
PROVENANCE_CODES = ("measured", "plan_columns_missing")

#: The four states, in the order the block declares them.
STATE_CODES = ("planned", "unplanned", "awaiting", "unread")
STATE_WORDS = {
    "planned": "по плану",
    "unplanned": "не по плану",
    "awaiting": "ждёт слота плана",
    "unread": "провенанс не прочитан",
}

# Anchors in full: a short reused anchor silently slices the wrong body, and the
# region above this one belongs to the plan-execution and booking cards.
HELPERS_START = "  const $ = (sel, root = document) => root.querySelector(sel);"
HELPERS_END = "  // The admin API can force-publish"
EXEC_START = "  // ── Исполнитель плана: работает ли потребитель и что он уже сделал ───────"
EXEC_END = "  async function loadPlanExecution() {"
DIST_START = "  function renderDistributionPlan(payload) {"
QUEUE_START = "  // ── Провенанс строки очереди: четыре состояния — четыре формулировки ──────"
QUEUE_END = "  window.tgfarmSetRuntimeMode = async (mode) => {"
PLAN_LOAD_START = "  async function loadEditorialPlan(force = false) {"


@pytest.fixture(scope="module")
def client():
    from tgfarm.api import create_app

    return TestClient(
        create_app(with_scheduler=False),
        base_url="http://127.0.0.1",
        headers={"X-TGFarm-Token": ensure_token()},
    )


@pytest.fixture(scope="module")
def script(client) -> str:
    """Fetched the way the browser fetches it, so a drifted path cannot hide."""
    return client.get("/ui/app.js").text.replace("\r\n", "\n")


def _slice(text: str, start: str, end: str) -> str:
    assert text.count(start) == 1, f"якорь встречается не один раз: {start!r}"
    head = text.index(start)
    assert end in text[head:], f"конечный якорь не найден после start: {end!r}"
    return text[head : text.index(end, head)]


@pytest.fixture(scope="module")
def block(script) -> str:
    """This feature's own source: the state machine, the two cards, the loader."""
    return _slice(script, QUEUE_START, QUEUE_END)


@pytest.fixture(scope="module")
def distribution(script) -> str:
    """The queue preview call site lives inside `renderDistributionPlan`."""
    return _slice(script, DIST_START, QUEUE_START)


@pytest.fixture(scope="module")
def plan_loader(script) -> str:
    head = script.index(PLAN_LOAD_START)
    return script[head : script.index("\n  }\n", head)]


def _code_only(text: str) -> str:
    """The block without its comments: what the panel prints, not what it says about it."""
    return "\n".join(line.split("//", 1)[0] for line in text.splitlines())


def _statement_after(code: str, start: int) -> str:
    """From `start` to the end of the statement it opens — the first `;` that is not
    inside a template literal, a call or an object."""
    depth = 0
    for index in range(start, len(code)):
        char = code[index]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == ";" and depth <= 0:
            return code[start : index + 1]
    return code[start:]


def _bare_name_reasons(code: str, name: str, position: int) -> list[str]:
    """Why this `${name}` may be printed as it is, or nothing if it may not.

    Four justifications, and every one of them is read out of the source rather than
    granted by a list a mutant could edit:
      * the value is a literal of this file (`const x = "текст"`);
      * the value is built already escaped, somewhere in its own declaration;
      * the value is a parameter the panel itself calls with a literal word;
      * the interpolation sits inside an `esc(...)` that is still open at that point.
    """
    reasons: list[str] = []
    for match in re.finditer(rf"(?:const|let)\s+{name}\s*=", code):
        statement = _statement_after(code, match.end())
        if re.match(r'\s*"', statement):
            reasons.append(f"{name} — литерал этого файла")
        if "esc(" in statement:
            reasons.append(f"{name} — собирается уже escaped")
    if re.search(rf"\(([^()]*\b{name}\b[^()]*)\)\s*=>", code):
        reasons.append(f"{name} — параметр, который вызывает сама панель")
    line_start = code.rfind("\n", 0, position) + 1
    before = code[line_start:position]
    last_open = before.rfind("esc(")
    if last_open != -1 and before.count("(", last_open) - before.count(")", last_open) > 0:
        reasons.append(f"{name} — печатается внутри esc(")
    return reasons


# ------------------------------------------------------------------ source shape
def test_the_residual_is_closed_the_panel_reads_the_key(script, block):
    """`git grep -c awaiting_booking -- tgfarm/ui/app.js` printed 0 on `dd75c42`."""
    lines = [line for line in script.splitlines() if "awaiting_booking" in line]
    assert lines, "the key still has no reader in the panel"
    assert len(lines) >= 2, f"one mention is a comment, not a reader: {lines}"
    assert "src.awaiting_booking === true" in block
    assert "row.awaiting_booking === true" in block


def test_the_four_states_are_four_different_words(block):
    table = block[block.index("QUEUE_PROVENANCE_LABELS = {") :]
    table = table[: table.index("};")]
    entries = re.findall(r'^\s{4}([a-z_]+): (.+?),$', table, re.MULTILINE)
    assert [code for code, _ in entries] == list(STATE_CODES), entries
    unread = re.search(r'QUEUE_PROVENANCE_UNREAD = "(.*)";', block)
    assert unread, "the unread label is no longer a constant"
    words = [
        unread.group(1) if expr.strip() == "QUEUE_PROVENANCE_UNREAD" else expr.strip('"')
        for _, expr in entries
    ]
    assert len(set(words)) == 4, f"два состояния печатаются одними словами: {words}"
    assert sorted(words) == sorted(STATE_WORDS.values()), words
    # The declared order and the label table are one list, not two that drifted.
    order = re.search(r"QUEUE_PROVENANCE_ORDER = \[(.*?)\];", block)
    assert order and re.findall(r'"([a-z_]+)"', order.group(1)) == list(STATE_CODES)


def test_the_key_names_are_the_ones_the_contract_freezes(block):
    """W22-A writes these names server-side; a rename on either side turns this red."""
    keys = re.search(r"QUEUE_PROVENANCE_KEYS = \[(.*?)\];", block)
    assert keys and tuple(re.findall(r'"([a-z_]+)"', keys.group(1))) == PROVENANCE_ROW_KEYS
    code = _code_only(block)
    assert "owner.queue_provenance" in code, "the platform's own verdict is no longer read"
    assert '("queue_provenance" in owner)' in code, (
        "an absent verdict key was folded into the codes the server sends"
    )
    assert 'QUEUE_PROVENANCE_MEASURED = "measured"' in block
    assert 'QUEUE_PROVENANCE_NO_COLUMNS = "plan_columns_missing"' in block
    for key in (*PROVENANCE_ROW_KEYS, PROVENANCE_ITEM_KEY, "awaiting_booking"):
        assert key in block, f"the block no longer names {key}"


def test_the_preview_row_asks_the_helper_with_the_platform_as_carrier(distribution):
    """The row alone cannot answer the question: the verdict lives on the platform."""
    code = _code_only(distribution)
    assert "${queueProvenanceText(post, item)}" in code
    assert "${queueProvenanceLegend(item)}" in code
    # Inside the preview block, not somewhere in the card that would render anyway.
    preview = code[code.index("const queued = item.queue_preview") : code.index("const progress")]
    assert "queueProvenanceText(post, item)" in preview
    assert "queueProvenanceLegend(item)" in preview
    assert "В очереди пока нет готового материала" in preview, "the empty branch was kept"


def test_the_slot_of_a_queued_row_still_goes_through_the_future_aware_helper(block, distribution):
    """`dt()` answers "только что" for every future instant, and a booked slot is
    usually in the future — the defect `tests/test_w19c_ui_plan_provenance.py` fixed
    once is not allowed to come back with the provenance line."""
    assert "nextRunCell(post.scheduled_at)" in distribution
    assert "dt(post.scheduled_at)" not in distribution
    assert "nextRunCell(src.plan_slot_at)" in block
    assert re.search(r"\bdt\(\s*src\.plan", block) is None, "a plan slot read as elapsed time"
    assert "new Date(" not in _code_only(block)


def test_nothing_is_coerced_into_a_zero_the_server_refused_to_send(block):
    code = _code_only(block)
    for coerced in ("|| 0", "?? 0", "Number(src.", "parseInt(", "1970"):
        assert coerced not in code, f"{coerced} invents the answer the API refused"


def test_the_states_are_resolved_by_precedence_the_reader_can_follow(block):
    """Four branches, and the order of them is the contract: the platform's verdict
    about the store outranks a row, an open order outranks a bare entry reference,
    and only a row that really carries the keys may be told apart by value."""
    code = block[block.index("function queueProvenanceState") : block.index("function queueProvenanceDetail")]
    positions = [
        code.index("QUEUE_PROVENANCE_NO_COLUMNS"),
        code.index('src.awaiting_booking === true'),
        code.index("QUEUE_PROVENANCE_KEYS.every"),
        code.index('src.plan_entry_id === null'),
    ]
    assert positions == sorted(positions), f"the reading order changed: {positions}"
    assert 'key in src' in code, "an absent key must not read as a null value"


def test_the_classes_the_block_emits_are_already_styled(block, distribution):
    """No new stylesheet: an unstyled class name is an invisible change."""
    sheet = APP_CSS.read_text(encoding="utf-8")
    used: set[str] = set()
    for fragment in (block, distribution):
        for match in re.finditer(r'class="([a-z0-9 -]+)"', fragment):
            used.update(match.group(1).split())
    assert used, "the block emits no markup the test can check"
    for token in used:
        assert f".{token}" in sheet, f"{token} is rendered but never styled"


def test_every_interpolated_server_field_leaves_through_esc(block, distribution):
    """The defect class wave 20 found: a value that came from the server reaching the
    markup without ever meeting `esc()`.

    Scoped to the lines this package wrote — the block plus the preview rows it now
    decorates — and checked structurally rather than by a list of call sites, so the
    next field added here is covered without editing this test.
    """
    preview = distribution[
        distribution.index("const queued = item.queue_preview") : distribution.index("const progress")
    ]
    offenders: list[str] = []
    for fragment in (block, preview):
        for expr in re.findall(r"\$\{([^}]*)\}", _code_only(fragment)):
            reads_field = re.search(r"\b(?:src|row|post|item|owner|payload)\.[A-Za-z_][A-Za-z0-9_]*", expr)
            if reads_field and "esc(" not in expr and "String(" not in expr:
                offenders.append(expr.strip())
    assert not offenders, f"unescaped interpolation of a server field: {offenders}"
    # The second half of the same defect class: `${name}` where a field was parked in
    # a local on the way to the markup. Checked as a rule over every bare identifier
    # in the block, so the next local added here is judged by the source, not by a
    # list of names this file happens to remember.
    code = _code_only(block)
    bare = [(match.group(1), match.start()) for match in re.finditer(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", code)]
    assert len(bare) >= 6, f"блок больше не интерполирует ничего «голым» — проверка выключилась: {bare}"
    unjustified = [name for name, position in bare if not _bare_name_reasons(code, name, position)]
    assert not unjustified, f"голая интерполяция без объяснения: {sorted(set(unjustified))}"


def test_the_card_mounts_itself_and_hangs_on_the_plan_tab(script, block, plan_loader):
    """`index.html` is outside this feature's write area, so the node is created here."""
    assert "document.createElement" in block and "insertBefore" in block, (
        "the card has no mount and would render nothing at all"
    )
    assert "loadOpenOrderQueue();" in plan_loader
    assert plan_loader.count("load") >= 3, "the plan tab stopped reading its other two cards"
    assert script.count("loadOpenOrderQueue") == 2, "the loader is defined but never called"
    html = (UI_DIR / "index.html").read_text(encoding="utf-8")
    assert "plan-open-orders" not in html, "the node is created by the panel, not by the markup"


def test_the_endpoint_the_card_calls_is_served_and_names_the_key(client, block):
    """A card that fetches a route nobody serves is a permanent «ответ не пришёл»,
    and a card that reads a key the server dropped is a permanent «не прочитан»."""
    assert 'OPEN_ORDERS_ENDPOINT = "/api/editorial"' in block
    api_source = (ROOT / "tgfarm" / "api.py").read_text(encoding="utf-8")
    assert '@app.get("/api/editorial")' in api_source
    response = client.get("/api/editorial")
    assert response.status_code == 200, response.text
    assert isinstance(response.json().get("queue"), list)
    editorial_source = (ROOT / "tgfarm" / "editorial.py").read_text(encoding="utf-8")
    for key in ("awaiting_booking", "cluster_public_id", "label", "niche", "heat", "score"):
        assert f'"{key}"' in editorial_source, f"the queue no longer serves {key}"
    for key in re.findall(r"\bsrc\.([a-z_]+)", block[block.index("function openOrderRow") :]):
        assert f'"{key}"' in editorial_source, f"панель читает {key}, очередь его не присылает"


# ------------------------------------------------------------------- real render
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
let store = {};
function node(id) {
  return { id: id, innerHTML: "", textContent: "", parentNode: null };
}
const document = {
  createElement: () => node(""),
  querySelector: (sel) => store[String(sel).replace(/^#/, "")] || null,
  querySelectorAll: () => [],
  addEventListener: () => {},
};
// `renderDistributionPlan` ends by caching the payload on the shared state object,
// which lives above the helpers this file slices; the stub is the only part of it
// the card touches.
const body = "const state = { distributionPlan: null };\n"
  + spec.slices.map(slice).join("\n");
const panel = new Function("document", "window", body
  + "\n; return { text: queueProvenanceText, card: renderDistributionPlan, orders: renderOpenOrderQueue };")(
  document, {});
function failureOf(item) {
  const f = item.failure;
  if (f === undefined || f === null) return null;
  if (typeof f === "string") return f;
  if (f.kind === "error") return new Error(f.message);
  return f;
}
const out = [];
for (const item of spec.cases) {
  store = {};
  let html = "";
  let thrown = null;
  try {
    if (item.kind === "text") {
      html = panel.text(item.row, item.carrier);
    } else if (item.kind === "card") {
      ["distribution-mode-state", "distribution-notice", "distribution-primary-grid",
        "distribution-crosspost-grid"].forEach((id) => { store[id] = node(id); });
      panel.card({ primary_platforms: item.platforms || [], platforms: [] });
      html = store["distribution-primary-grid"].innerHTML;
    } else {
      if (!item.mount_missing) {
        const anchor = node("plan-entries");
        anchor.parentNode = { insertBefore: (built) => { store[built.id] = built; } };
        store["plan-entries"] = anchor;
      }
      panel.orders(item.payload, failureOf(item));
      const mount = store["plan-open-orders"] || null;
      html = mount ? mount.innerHTML : "";
      if (!mount && !item.mount_missing) thrown = "card rendered without a mount";
    }
  } catch (error) {
    thrown = String((error && error.message) || error);
  }
  out.push({ name: item.name, html, thrown });
}
process.stdout.write(JSON.stringify(out));
"""

SLICES = [
    [HELPERS_START, HELPERS_END],
    [EXEC_START, EXEC_END],
    [DIST_START, QUEUE_END],
]


def _run(tmp_path: Path, cases: list[dict]) -> dict[str, dict[str, str]]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node недоступен — поведение панели не проверить")
    harness = tmp_path / "harness.cjs"
    harness.write_text(HARNESS, encoding="utf-8")
    spec_path = tmp_path / "cases.json"
    spec_path.write_text(
        json.dumps({"slices": SLICES, "cases": cases}, ensure_ascii=False), encoding="utf-8"
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
    return {item["name"]: item for item in json.loads(done.stdout)}


def _texts(tmp_path: Path, cases: list[tuple[str, dict, dict]]) -> dict[str, str]:
    """Render the state sentence alone — the four states, with nothing around them."""
    spec = [
        {"name": name, "kind": "text", "row": row, "carrier": carrier}
        for name, row, carrier in cases
    ]
    out = _run(tmp_path, spec)
    for item in spec:
        name = item["name"]
        assert out[name]["thrown"] is None, f"{name}: панель упала — {out[name]['thrown']}"
        assert out[name]["html"], f"{name} не напечатала ничего"
    return {name: out[name]["html"] for name in out}


def _cards(tmp_path: Path, cases: list[dict]) -> dict[str, dict[str, str]]:
    out = _run(tmp_path, [{**case, "kind": "card"} for case in cases])
    for case in cases:
        name = case["name"]
        assert out[name]["thrown"] is None, f"{name}: панель упала — {out[name]['thrown']}"
        assert out[name]["html"], f"{name} не напечатала ни одной карточки"
    return out


def _orders(tmp_path: Path, cases: list[dict]) -> dict[str, dict[str, str]]:
    out = _run(tmp_path, [{**case, "kind": "orders"} for case in cases])
    for case in cases:
        name = case["name"]
        assert out[name]["thrown"] is None, f"{name}: панель упала — {out[name]['thrown']}"
        assert out[name]["html"], f"{name} не напечатала ничего под существующим mount"
    return out


def _preview_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "post_public_id": "post0001",
        "title": "Как ферма считает тепло",
        "channel": "MAIN",
        "status": "scheduled",
        "scheduled_at": "2096-11-05T09:00:00+00:00",
    }
    row.update(overrides)
    return row


def _measured_row(**overrides: Any) -> dict[str, Any]:
    row = _preview_row(plan_entry_id=41, plan_slot_at="2096-11-05T09:00:00+00:00",
                       plan_public_id="pl0aaaabbbb")
    row.update(overrides)
    return row


def _platform(rows: list[dict], **overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "platform": "telegram",
        "name": "Telegram",
        "kind": "primary",
        "enabled": True,
        "configured": True,
        "automation": "primary",
        "daily_cap": 6,
        "daily_sent": 1,
        "remaining": 5,
        "channels": ["MAIN"],
        "topics": ["технологии"],
        "queue_preview": rows,
        "reason": "Автоматическая очередь основной площадки активна.",
        PROVENANCE_ITEM_KEY: "measured",
    }
    item.update(overrides)
    return item


def _queue_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "cluster_public_id": "cl0aaaabbbb",
        "label": "Тема из очереди",
        "niche": "технологии",
        "heat": 0.74,
        "sources": 3,
        "age_hours": 4.0,
        "cooling": False,
        "score": 0.51,
        "ai_relevance": 0.4,
        "ai_quality": 0.6,
        "is_news": False,
        "awaiting_booking": True,
    }
    row.update(overrides)
    return row


# --------------------------------------------------------- the four states apart
def test_the_four_states_print_four_different_sentences(tmp_path):
    """One row shape, four payloads, four texts — and no two texts equal."""
    rendered = _texts(tmp_path, [
        ("planned", _measured_row(), {PROVENANCE_ITEM_KEY: "measured"}),
        ("unplanned", _measured_row(plan_entry_id=None), {PROVENANCE_ITEM_KEY: "measured"}),
        ("awaiting", _measured_row(awaiting_booking=True), {PROVENANCE_ITEM_KEY: "measured"}),
        ("unread_columns", _preview_row(), {PROVENANCE_ITEM_KEY: "plan_columns_missing"}),
        ("unread_no_keys", _preview_row(), {}),
    ])
    by_state = {
        "planned": rendered["planned"],
        "unplanned": rendered["unplanned"],
        "awaiting": rendered["awaiting"],
        "unread": rendered["unread_columns"],
    }
    assert len({*by_state.values()}) == 4, by_state
    assert STATE_WORDS["planned"] in by_state["planned"]
    assert STATE_WORDS["unplanned"] in by_state["unplanned"]
    assert STATE_WORDS["awaiting"] in by_state["awaiting"]
    assert STATE_WORDS["unread"] in by_state["unread"]
    # A state never prints another state's sentence. «по плану» is a substring of
    # «не по плану», so the comparison is on the labelled form the chip prints.
    assert "провенанс: по плану" not in by_state["unplanned"], by_state["unplanned"]
    assert "провенанс: по плану" not in by_state["awaiting"]
    assert STATE_WORDS["awaiting"] not in by_state["planned"]
    assert STATE_WORDS["awaiting"] not in by_state["unplanned"]
    for word in ("провенанс: по плану", "не по плану", STATE_WORDS["awaiting"]):
        assert word not in by_state["unread"], f"{word} печатается там, где ничего не прочитано"
    # The two silences stay two silences: the store has no columns, the row has no keys.
    assert rendered["unread_columns"] != rendered["unread_no_keys"]
    assert "plan_columns_missing" in rendered["unread_columns"]
    assert "plan_columns_missing" not in rendered["unread_no_keys"]


def test_a_row_without_the_keys_is_never_read_as_not_by_plan(tmp_path):
    """The negative case of the whole feature.

    Today the server sends `queue_preview` rows with no provenance keys at all
    (`W22-A` adds them), and the store answers `plan_columns_missing`. Printing
    «не по плану» there would be a verdict built out of silence.
    """
    rendered = _texts(tmp_path, [
        ("no_item_key", _preview_row(), {}),
        ("columns_missing", _preview_row(), {PROVENANCE_ITEM_KEY: "plan_columns_missing"}),
        ("measured_null", _measured_row(plan_entry_id=None), {PROVENANCE_ITEM_KEY: "measured"}),
    ])
    assert "не по плану" not in rendered["no_item_key"], rendered["no_item_key"]
    assert "не по плану" not in rendered["columns_missing"], rendered["columns_missing"]
    assert STATE_WORDS["unread"] in rendered["no_item_key"]
    assert STATE_WORDS["unread"] in rendered["columns_missing"]
    # ... and the row that really was measured as unplanned keeps its own words.
    assert STATE_WORDS["unplanned"] in rendered["measured_null"]
    assert STATE_WORDS["unread"] not in rendered["measured_null"]


def test_an_unread_verdict_invalidates_the_fields_under_it(tmp_path):
    """`plan_columns_missing` means the read never happened, so a row that still
    carries an entry id under it may not print «по плану»."""
    html = _texts(tmp_path, [(
        "contradiction",
        _measured_row(),
        {PROVENANCE_ITEM_KEY: "plan_columns_missing"},
    )])["contradiction"]
    assert STATE_WORDS["unread"] in html
    assert STATE_WORDS["planned"] not in html
    assert "41" not in html and "pl0aaaabbbb" not in html, "a field nobody read was printed"


def test_a_code_the_panel_does_not_know_is_admitted_not_guessed(tmp_path):
    """An out-of-contract verdict is silence with the code attached, never a state."""
    html = _texts(tmp_path, [("odd", _measured_row(), {PROVENANCE_ITEM_KEY: "recalculating"})])["odd"]
    assert STATE_WORDS["unread"] in html
    assert "recalculating" in html, "the code the server sent was swallowed"
    for word in (STATE_WORDS["planned"], STATE_WORDS["unplanned"], STATE_WORDS["awaiting"]):
        assert word not in html


def test_a_reference_that_arrived_without_an_answer_behind_it(tmp_path):
    """`plan_entry_id: 0` is a row id; `""`, `{}` and a float `NaN` are not; and a
    measured `null` is the fourth case again, never a fifth spelling of unread."""
    rendered = _texts(tmp_path, [
        ("zero", _measured_row(plan_entry_id=0), {PROVENANCE_ITEM_KEY: "measured"}),
        ("empty", _measured_row(plan_entry_id=""), {PROVENANCE_ITEM_KEY: "measured"}),
        ("object", _measured_row(plan_entry_id={}), {PROVENANCE_ITEM_KEY: "measured"}),
        ("null", _measured_row(plan_entry_id=None), {PROVENANCE_ITEM_KEY: "measured"}),
    ])
    assert "запись 0" in rendered["zero"], rendered["zero"]
    assert STATE_WORDS["planned"] in rendered["zero"]
    for name in ("empty", "object"):
        assert STATE_WORDS["unread"] in rendered[name], rendered[name]
        assert "не читается" in rendered[name], rendered[name]
    assert STATE_WORDS["unplanned"] in rendered["null"], "a measured null is not an unread field"
    assert STATE_WORDS["unread"] not in rendered["null"]


def test_a_planned_row_names_the_plan_the_entry_and_the_slot(tmp_path):
    """The state word is not the sentence: the operator needs the three references."""
    html = _texts(tmp_path, [("full", _measured_row(), {PROVENANCE_ITEM_KEY: "measured"})])["full"]
    assert "план pl0aaaabbbb" in html
    assert "запись 41" in html
    assert "слот " in html and "только что" not in html, "a 2096 booking read as an elapsed time"
    missing = _texts(tmp_path, [(
        "unresolved",
        _measured_row(plan_public_id=None, plan_slot_at=None),
        {PROVENANCE_ITEM_KEY: "measured"},
    )])["unresolved"]
    assert "план не назван" in missing and "слот не записан" in missing
    assert "null" not in missing and "undefined" not in missing


# ------------------------------------------------------------ the card, rendered
def test_the_queue_preview_row_carries_the_state_and_the_empty_branch_stays(tmp_path):
    out = _cards(tmp_path, [
        {"name": "planned", "platforms": [_platform([_measured_row()])]},
        {"name": "unread", "platforms": [_platform([_preview_row()],
                                                   **{PROVENANCE_ITEM_KEY: "plan_columns_missing"})]},
        {"name": "empty", "platforms": [_platform([])]},
    ])
    assert "Как ферма считает тепло" in out["planned"]["html"], "escaping is not hiding"
    assert STATE_WORDS["planned"] in out["planned"]["html"]
    assert STATE_WORDS["unread"] in out["unread"]["html"]
    assert STATE_WORDS["planned"] not in out["unread"]["html"]
    assert "провенанс очереди не прочитан" in out["unread"]["html"], (
        "the platform-level sentence about the store is missing"
    )
    assert "В очереди пока нет готового материала" in out["empty"]["html"]
    assert "провенанс" not in out["empty"]["html"], "no rows means no per-row verdicts"


def test_the_card_without_the_platform_key_says_the_key_is_absent(tmp_path):
    """W22-A has not landed yet: today's payload has no `queue_provenance` at all."""
    item = _platform([_preview_row()])
    del item[PROVENANCE_ITEM_KEY]
    html = _cards(tmp_path, [{"name": "nok", "platforms": [item]}])["nok"]["html"]
    assert "ключа queue_provenance в ответе нет" in html
    assert STATE_WORDS["unread"] in html
    assert "не по плану" not in html


def test_the_preview_list_stays_a_sample_and_says_so(tmp_path):
    """`distribution.py` reads five rows and the card shows three."""
    rows = [
        _measured_row(post_public_id=f"p{index}", plan_entry_id=index, title=f"Запись {index}")
        for index in range(5)
    ]
    html = _cards(tmp_path, [{"name": "five", "platforms": [_platform(rows)]}])["five"]["html"]
    assert "В очереди: 5+" in html
    assert html.count('<div class="distribution-pick">') == 3
    assert html.count("distribution-picks") == 1, "one wrapper per preview, not one per row"
    assert "Запись 2" in html, "the sample is cut after the third row"
    assert "Запись 3" not in html and "Запись 4" not in html


# ------------------------------------------------------------- the open-order card
def test_the_open_orders_card_counts_the_three_answers_separately(tmp_path):
    """`true`, `false` and an absent key are three facts about three different rows."""
    payload = {"queue": [
        _queue_row(),
        _queue_row(cluster_public_id="cl0ccccccc1", awaiting_booking=False),
        {"cluster_public_id": "cl0cccccc9", "label": "Строка без ключа", "heat": 0.2, "score": 0.1},
    ]}
    html = _orders(tmp_path, [{"name": "three", "payload": payload}])["three"]["html"]
    assert "прочитано тем: 3" in html
    assert "ждут слота плана: 1" in html
    assert "слота плана не ждут: 1" in html
    assert "ключа о брони не прислано: 1" in html, "the silent row was folded into a counted one"
    assert STATE_WORDS["awaiting"] in html
    assert "показаны все 1 ждущих" in html, "a waiting row is what this card lists"


def test_a_silent_row_is_listed_and_worded_as_silence_when_nobody_waits(tmp_path):
    """With no open order the card falls back to the rows it did read, and a row
    whose key never came still prints unread rather than «не по плану»."""
    payload = {"queue": [
        {"cluster_public_id": "cl0cccccc9", "label": "Строка без ключа", "heat": 0.2, "score": 0.1},
        _queue_row(cluster_public_id="cl0cccccc8", label="Ответила «не жду»", awaiting_booking=False),
    ]}
    html = _orders(tmp_path, [{"name": "silent", "payload": payload}])["silent"]["html"]
    body = html.split("</h3>", 1)[1]
    assert "Строка без ключа" in body and "Ответила «не жду»" in body
    assert STATE_WORDS["unread"] in body
    assert "не по плану" not in body, f"silence printed as a verdict: {body}"
    assert "слота плана эта тема не ждёт" in body, "the measured `false` was thrown away"
    assert "ключа о брони не прислано: 1" in html


def test_a_waiting_draft_is_not_printed_like_an_unqueued_one(tmp_path):
    """The residual this package was opened for, in one pair of assertions."""
    rendered = _orders(tmp_path, [
        {"name": "waiting", "payload": {"queue": [_queue_row(label="Ждёт брони")]}},
        {"name": "plain", "payload": {"queue": [
            _queue_row(cluster_public_id="clnorm0001", label="Просто в очереди", awaiting_booking=False),
        ]}},
    ])
    waiting = rendered["waiting"]["html"].split("</h3>", 1)[1]
    plain = rendered["plain"]["html"].split("</h3>", 1)[1]
    # The chip's labelled form is compared, not the bare phrase: the card's own
    # explanatory sentence also contains «не ждёт слота плана».
    assert "провенанс: ждёт слота плана" in waiting
    assert "чернов уже написан" in waiting
    assert STATE_WORDS["unread"] in plain and "не ждёт" in plain
    assert "провенанс: ждёт слота плана" not in plain, (
        "an unqueued topic borrowed the open order's words"
    )
    assert "ждут слота плана: 1" in rendered["waiting"]["html"]
    assert "ждут слота плана: 0" in rendered["plain"]["html"]


def test_the_measured_zero_and_the_silence_are_still_different_rows(tmp_path):
    """An empty queue is an answer; a queue that never came is not."""
    rendered = _orders(tmp_path, [
        {"name": "empty", "payload": {"queue": []}},
        {"name": "nokey", "payload": {}},
        {"name": "network", "payload": None, "failure": {"kind": "error", "message": "fetch failed"}},
        {"name": "http", "payload": None, "failure": "/api/editorial -> 500"},
    ])
    empty = rendered["empty"]["html"]
    assert "ждут слота плана: 0" in empty, "a measured nothing prints as a zero"
    assert "Ни одна прочитанная тема не ждёт слота плана" in empty
    assert "ключа о брони не прислано" not in empty
    for name in ("nokey", "network", "http"):
        assert "ждут слота плана: 0" not in rendered[name]["html"], f"{name} printed a zero nobody measured"
    assert "Очередь не прочитана" in rendered["nokey"]["html"] or "очередь не прочитана" in rendered["nokey"]["html"]
    assert "Ключ queue не пришёл" in rendered["nokey"]["html"]
    assert "ответ не пришёл" in rendered["network"]["html"]
    assert "Это не «ждущих нет»" in rendered["network"]["html"]
    assert "fetch failed" in rendered["network"]["html"]
    assert "500" in rendered["http"]["html"]
    # The network silence and the server silence never share a sentence.
    assert len({rendered["empty"]["html"], rendered["nokey"]["html"],
                rendered["network"]["html"], rendered["http"]["html"]}) == 4


def test_the_card_lists_the_waiting_rows_and_admits_the_window(tmp_path):
    rows = [_queue_row(cluster_public_id=f"cl{index:09d}") for index in range(10)]
    html = _orders(tmp_path, [{"name": "ten", "payload": {"queue": rows}}])["ten"]["html"]
    assert "ждут слота плана: 10" in html
    assert "показаны 8 из 10 ждущих" in html
    assert "не больше 8 строк за раз" in html
    assert html.count("plan-entry ") + html.count('class="plan-entry"') == 8


def test_a_card_with_no_place_to_mount_is_not_an_error(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node недоступен — поведение панели не проверить")
    out = _run(tmp_path, [{"name": "nomount", "kind": "orders", "payload": {"queue": [_queue_row()]},
                           "mount_missing": True}])
    assert out["nomount"]["thrown"] is None, out["nomount"]["thrown"]
    assert out["nomount"]["html"] == ""


# ------------------------------------------------------------------------ escaping
def test_hostile_stored_text_stays_text(tmp_path):
    hostile = '<img src=x onerror="fetch(/evil)">'
    rendered = _run(tmp_path, [
        {"name": "text", "kind": "text",
         "row": _measured_row(plan_public_id=hostile, plan_entry_id=hostile),
         "carrier": {PROVENANCE_ITEM_KEY: hostile}},
        {"name": "card", "kind": "card",
         "platforms": [_platform([_measured_row(title=hostile, plan_public_id=hostile)])]},
        {"name": "orders", "kind": "orders",
         "payload": {"queue": [_queue_row(label=hostile, cluster_public_id=hostile, niche=hostile)]}},
    ])
    for case in rendered.values():
        assert case["thrown"] is None, case["thrown"]
        assert hostile not in case["html"]
        assert "<img" not in case["html"], "an image tag was built out of stored text"
        assert "&lt;img" in case["html"], "the stored text is shown, as data"
    assert "&quot;" in rendered["text"]["html"] or "&#39;" in rendered["text"]["html"]


def test_a_hostile_provenance_code_is_escaped_and_still_unread(tmp_path):
    hostile = '<script>alert(1)</script>'
    html = _texts(tmp_path, [("odd", _measured_row(), {PROVENANCE_ITEM_KEY: hostile})])["odd"]
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert STATE_WORDS["unread"] in html


# ------------------------------------------------------------------ the live seam
@pytest.fixture()
def seeded_preview(client):
    """One gated post in a real enabled channel, so the preview has a row to serve.

    The platform is read out of the live answer rather than invented: the endpoint
    reports only `telegram / vk / dzen`, and a channel filed under any other id is a
    channel no card ever asks about. Taking the id from the payload keeps the fixture
    true when the catalog changes and fails in exactly one case — a fleet with no
    primary platform at all — which the assertion below names.
    """
    plan = client.get("/api/distribution/plan")
    assert plan.status_code == 200, plan.text
    primary = [item.get("platform") for item in plan.json().get("primary_platforms", [])]
    assert primary, "в ответе нет ни одной основной площадки — проверять сейм не на чём"
    platform = str(primary[0])
    channel_id = f"w22bchan{uuid4().hex[:10]}"
    post_id = f"w22bpost{uuid4().hex[:10]}"
    with session_scope() as session:
        session.add(
            Channel(
                id=channel_id, title="W22-B seam", niche="test", platform=platform,
                timezone="UTC", enabled=True, posts_per_day=4,
            )
        )
        session.add(
            Post(
                public_id=post_id, channel_id=channel_id, hook="seam", body="seam",
                status="gated", scheduled_at=datetime.now(UTC) + timedelta(hours=3),
                created_at=datetime.now(UTC),
            )
        )
    try:
        yield platform, post_id
    finally:
        with session_scope() as session:
            session.execute(delete(Post).where(Post.public_id == post_id))
            session.execute(delete(Channel).where(Channel.id == channel_id))


def test_the_rows_the_endpoint_serves_today_render_as_unread(client, tmp_path, seeded_preview):
    """The invariant this package exists to install, checked against the payload.

    Today `/api/distribution/plan` sends `queue_preview` rows without any of the
    contract keys; after W22-A the rows carry them and the platform carries the
    verdict. Both halves are covered by one rule the merge may not break: a row may
    print «не по плану» only when it really carries a null `plan_entry_id`, and a
    platform whose read was not measured may never print a state word at all.
    """
    platform_id, post_id = seeded_preview
    payload = client.get("/api/distribution/plan")
    assert payload.status_code == 200, payload.text
    items = [
        item for item in payload.json().get("primary_platforms", [])
        if item.get("queue_preview")
    ]
    rows = [row for item in items for row in item["queue_preview"]]
    assert rows, f"ни одна площадка не прислала queue_preview — {platform_id} не о чём проверить"
    served = next(
        (item["queue_preview"] for item in items if item.get("platform") == platform_id), None
    )
    assert served is not None, f"площадка {platform_id} пропала из primary_platforms"
    if post_id not in {row.get("post_public_id") for row in served}:
        # `queue_preview` is a five-row window ordered by the scheduled moment, and a
        # suite that ran before this test owns the earlier rows. A full window is the
        # only honest reason the seeded post is not in it; a short one is a defect.
        assert len(served) >= 5, (
            f"{post_id} не в окне из {len(served)} строк, и окно не заполнено: очередь "
            "не прочитала пост, который только что появился в базе"
        )
    # The half of the contract this package does not own: a row may not pretend to
    # carry the plan fields unless the platform says the read was measured. Today the
    # rows carry nothing and the verdict is absent, which is the «не прочитан» case;
    # after W22-A the verdict arrives. A payload that sends only one of the two is a
    # half-merged contract, and this is the test that says so.
    assert any(PROVENANCE_ITEM_KEY in item for item in items) or all(
        key not in row for row in rows for key in PROVENANCE_ROW_KEYS
    ), "строки с полями плана присланы без вердикта площадки"
    cases = [
        {"name": f"row{index}", "kind": "text", "row": row, "carrier": item}
        for index, item in enumerate(items)
        for row in item["queue_preview"]
    ]
    rendered = _run(tmp_path, cases)
    for case in cases:
        row, item = case["row"], case["carrier"]
        html = rendered[case["name"]]["html"]
        assert html, f"{case['name']} не напечатала ничего"
        columns_missing = item.get(PROVENANCE_ITEM_KEY) == PROVENANCE_CODES[1]
        if columns_missing or PROVENANCE_ROW_KEYS[0] not in row:
            assert STATE_WORDS["unread"] in html, f"непрочитанная строка печатает вердикт: {html}"
            assert "не по плану" not in html, f"молчание напечатано как вердикт: {html}"
        elif row[PROVENANCE_ROW_KEYS[0]] is None:
            assert STATE_WORDS["unplanned"] in html, html
            assert STATE_WORDS["unread"] not in html, html
        else:
            assert STATE_WORDS["planned"] in html, html


# --------------------------------------------------------- the untouched neighbours
def test_the_post_provenance_block_was_not_rewritten(script):
    """W19-C answers this question for a post row; this package answers it for a
    queue row. Sharing one table across the two was the tempting shortcut, and it
    would have renamed the older contract under the operator's feet."""
    provenance = _slice(
        script,
        "  // ── Провенанс поста: из какого решения плана этот пост",
        "  function postCard(p) {",
    )
    assert 'const PROVENANCE_UNKNOWN = "неизвестно";' in provenance
    assert "создан без плана" in provenance
    assert "QUEUE_PROVENANCE_LABELS" not in provenance
    for marker in ("postProvenanceChip(p)",):
        assert marker in script


def test_the_plan_execution_and_booking_blocks_still_own_their_own_words(script):
    booking = _slice(
        script,
        "  // ── Бронь плана против факта: попал ли пост в зарезервированный слот ──────",
        DIST_START,
    )
    assert 'const BOOKING_UNREAD = "не прочитано";' in booking
    assert STATE_WORDS["unread"] not in booking, "the new vocabulary leaked into the booking card"
    assert "QUEUE_PROVENANCE_LABELS" not in booking


def test_the_script_is_still_parseable_javascript():
    node = shutil.which("node")
    if not node:  # pragma: no cover - CI installs node
        pytest.skip("node is not on PATH")
    done = subprocess.run(  # noqa: S603
        [node, "--check", str(UI_DIR / "app.js")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
