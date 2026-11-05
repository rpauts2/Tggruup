"""W20-C UI contract: «план забронировал слот — ферма опубликовала в него?».

`GET /api/editorial/plan/booking` subtracts `posts.plan_slot_at` (the reservation,
written once by `s4_generation.persist_draft`) from `posts.scheduled_at` (the slot
the queue ended up using, rewritten by every requeue, retry and unpublish) and from
`posts.published_at` (what the platform actually took). The panel's whole job here is
to keep those readings apart in words: six states, six sentences, and three of the
six are different silences that must not be read as one another — «брони нет» is not
a zero, «сервер ответил, что не мерил» is not «постов нет», and «запрос не дошёл» is
not either of them (P34).

Rendering is executed under node on the file the browser fetches, because only the
produced markup can tell a measured zero from a fabricated one and «не прочитано»
from a printed `0`. The block is sliced out of `tgfarm/ui/app.js` by full-line
anchors; a missing anchor raises `ValueError` in the harness, and this file turns
that into a red test rather than a skip — a card that silently stopped existing is
the worst way for this contract to fail.

`PUBLICATION_GATE`, `PROVENANCE_*`, `postCard` and `renderPosts` are not touched by
this feature and the last source tests pin that the booking block neither renames
nor reaches into them.
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
ENDPOINT = "/api/editorial/plan/booking"

#: The six codes the server may answer with (`tgfarm/api.py::BOOKING_STATES`).
#: Repeated here on purpose: the panel has to name them without importing Python,
#: and this file is where a rename on one side of the seam is caught.
STATE_CODES = ("no_booking", "honoured", "shifted", "refused", "awaiting_slot", "unread_row")

# Anchors in full: a short reused anchor silently slices the wrong body.
HELPERS_START = "  const $ = (sel, root = document) => root.querySelector(sel);"
HELPERS_END = "  // The admin API can force-publish"
EXEC_START = "  // ── Исполнитель плана: работает ли потребитель и что он уже сделал ───────"
EXEC_RENDER_END = "  async function loadPlanExecution() {"
BOOKING_START = "  // ── Бронь плана против факта: попал ли пост в зарезервированный слот ──────"
BOOKING_END = "  function renderDistributionPlan(payload) {"
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
    return client.get("/ui/app.js").text


def _slice(text: str, start: str, end: str) -> str:
    assert text.count(start) == 1, f"якорь встречается не один раз: {start!r}"
    head = text.index(start)
    assert end in text[head:], f"конечный якорь не найден после start: {end!r}"
    return text[head : text.index(end, head)]


@pytest.fixture(scope="module")
def booking(script) -> str:
    # The served file keeps its CRLF line endings; normalising them is what lets
    # every regex below anchor on `$` instead of guessing at `\r`.
    return _slice(script, BOOKING_START, BOOKING_END).replace("\r\n", "\n")


def _code_only(text: str) -> str:
    """The block without its comments: what the panel prints, not what it says about it.

    This feature's own comment names «заблокировано» to explain why it refuses to use
    the word, so a wording test that read the comments would fail on the very guard
    it is trying to pin.
    """
    return "\n".join(line.split("//", 1)[0] for line in text.splitlines())


@pytest.fixture(scope="module")
def plan_loader(script) -> str:
    """The one place the card is hung on the page: the plan tab's loader."""
    head = script.index(PLAN_LOAD_START)
    end = script.index("\n  }\n", head)
    return script[head:end]


# ------------------------------------------------------------------ source shape
def test_the_six_states_have_six_different_words(booking):
    """One sentence per state, and no state borrows another's."""
    words = re.findall(r'^\s{4}(?:no_booking|honoured|shifted|refused|awaiting_slot|unread_row): "(.*)",$',
                       _code_only(booking), re.MULTILINE)
    assert len(words) == 6, words
    assert len(set(words)) == 6, f"два состояния печатаются одними словами: {words}"
    for phrase in ("брони нет", "уважено", "сдвинуто", "отказано", "не читается"):
        assert any(phrase in word for word in words), phrase
    order = re.search(r"BOOKING_STATE_ORDER = \[(.*?)\];", booking, re.S)
    assert order, "the panel lost the list that fixes the order of the states"
    assert re.findall(r'"([a-z_]+)"', order.group(1)) == list(STATE_CODES), order.group(1)


def test_a_refusal_keeps_the_gate_code_and_never_becomes_a_generic_block(booking):
    """«заблокировано» would be a second, vaguer word for a named fact.

    The scheduler refuses a booking under one registered gate code
    (`slot_outside_local_hours`, `d848eee`), and the ledger is the only thing that
    counted it, so the sentence carries the code and the attribution instead of a
    blanket "blocked" the operator could not act on.
    """
    code = _code_only(booking)
    assert "заблокировано" not in code, (
        "a generic block word replaced the gate code the ledger names"
    )
    assert "отказ по гейту" in code
    assert "refusal_gate_code" in code
    assert "не посту" in code, "the tick-vs-post limit was dropped from the sentence"


def test_the_states_the_panel_knows_are_the_states_the_server_serves(booking):
    """Renaming a state on the server turns this red, on the panel's own side."""
    from tgfarm import api

    assert set(STATE_CODES) == set(api.BOOKING_STATES), api.BOOKING_STATES
    for code in STATE_CODES:
        assert f'{code}: "' in booking, f"the panel cannot word {code}"


def test_the_slot_is_rendered_by_the_helper_that_knows_future_from_past(booking):
    """A booking is usually in the future and `dt()` answers "только что" for it.

    Its elapsed time is negative and every negative number is under a minute;
    `nextRunCell()` is this file's existing answer, so the card may not invent a
    second clock that repeats the defect `tests/test_w19c_ui_plan_provenance.py`
    pinned away.
    """
    assert "nextRunCell(" in booking
    for field in ("plan_slot_at", "scheduled_at", "published_at"):
        assert re.search(r"\bdt\(\s*r\." + field, booking) is None, f"dt() on {field}"


def test_no_zero_is_printed_where_the_server_sent_none(booking):
    """`bookingCount` is the only place a count reaches markup, and it has no `|| 0`.

    `n()` ends in `|| 0`, so handing it a null is how a card starts claiming a
    measurement that never happened; the guard is a finite-number test, not a cast.
    """
    code = "\n".join(line.split("//", 1)[0] for line in booking.splitlines())
    for coerced in ("|| 0", "?? 0", "Number(r.plan", "new Date(r.", "n(view.count)", "n(gate.count)"):
        assert coerced not in code, f"{coerced} invents the answer the API refused"
    assert "1970" not in code
    assert 'const bookingCount = (v) => (typeof v === "number"' in code, code[:400]


def test_the_block_touches_no_other_cards_vocabulary(booking):
    """`PUBLICATION_GATE` and `PROVENANCE_*` belong to other features and other worktrees."""
    for foreign in ("PUBLICATION_GATE", "PROVENANCE_UNKNOWN", "PROVENANCE_FIELDS", "postProvenance"):
        assert foreign not in booking, f"{foreign} was pulled into the booking block"


def test_the_classes_the_block_emits_are_already_styled(booking):
    """No new stylesheet: an unstyled class name is an invisible change."""
    sheet = APP_CSS.read_text(encoding="utf-8")
    used: set[str] = set()
    for match in re.finditer(r'class="([a-z0-9 -]+)"', booking):
        used.update(match.group(1).split())
    assert used, "the block emits no markup the test can check"
    for token in used:
        assert f".{token}" in sheet, f"{token} is rendered but never styled"


def test_the_card_is_loaded_by_the_plan_tab_and_mounts_without_index_html(script, booking):
    """`index.html` is outside this feature's write area, so the node is created here."""
    assert "loadBookingFidelity();" in script
    assert script.count("loadBookingFidelity") >= 2, "defined but never called"
    assert "document.createElement" in booking and "insertBefore" in booking, (
        "the card has no mount and would render nothing at all"
    )


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
const body = spec.slices.map(slice).join("\n");
// A mutable store so one compiled panel can render every case: `bookingMount`
// looks the node up by id, exactly as the browser does.
let store = {};
const document = {
  createElement: () => ({ id: "", innerHTML: "", style: {}, classList: { add() {}, remove() {} } }),
  querySelector: (sel) => store[String(sel).replace(/^#/, "")] || null,
  querySelectorAll: () => [],
  addEventListener: () => {},
};
const build = new Function("document", "window",
  body + "\n; return { render: renderBookingFidelity, row: bookingRowLine, chip: bookingStateChip };");
const panel = build(document, {});
function freshStore(mountMissing) {
  const next = {};
  const anchor = { id: "plan-execution", innerHTML: "", parentNode: null };
  anchor.parentNode = { insertBefore(node) { next[node.id] = node; } };
  if (!mountMissing) next["plan-execution"] = anchor;
  return next;
}
function failureOf(item) {
  const f = item.failure;
  if (f === undefined || f === null) return null;
  if (typeof f === "string") return f;
  if (f.kind === "error") return new Error(f.message);
  return f;
}
const out = [];
for (const item of spec.cases) {
  store = freshStore(!!item.mount_missing);
  let html = "";
  let thrown = null;
  try {
    if (item.kind === "row") html = panel.row(item.row);
    else if (item.kind === "chip") html = panel.chip(item.code, item.state);
    else {
      panel.render(item.data, failureOf(item));
      const node = store["plan-booking"] || null;
      html = node ? node.innerHTML : "";
      if (!node && !item.mount_missing) thrown = "card rendered without a mount";
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
    [EXEC_START, EXEC_RENDER_END],
    [BOOKING_START, BOOKING_END],
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


def _card(**overrides) -> dict[str, Any]:
    card: dict[str, Any] = {
        "readable": True,
        "reason": None,
        "reason_detail": None,
        "precision": "second",
        "gate": {
            "code": "slot_outside_local_hours",
            "count": None,
            "attribution": "tick",
            "tick_reason": None,
        },
        "population": {
            "posts": 1226, "booked": 3, "unbooked": 1223,
            "scanned": 3, "truncated": False, "limit": 400,
        },
        "states": {code: {"count": 1, "basis": "population", "single_row": True} for code in STATE_CODES},
        "shift": {"later": 1, "earlier": 0, "largest_abs_minutes": 90.0},
        "samples": [],
        "note": "Прочитано 3 броней из 3. Состояния измерены по всем строкам.",
    }
    card.update(overrides)
    return card


def _row(**overrides) -> dict[str, Any]:
    row: dict[str, Any] = {
        "post_id": "post0001",
        "status": "scheduled",
        "platform": "telegram",
        "timezone": "UTC",
        "plan_slot_at": "2096-11-05T09:00:00+00:00",
        "scheduled_at": "2096-11-05T09:00:00+00:00",
        "published_at": None,
        "state": "honoured",
        "direction": None,
        "refusal_gate_code": None,
        "refusal_basis": None,
        "shift_seconds": 0,
        "shift_minutes": 0,
        "published_shift_seconds": None,
        "published_shift_minutes": None,
    }
    row.update(overrides)
    return row


def _cards(tmp_path: Path, cases: list[tuple[str, Any, Any]]) -> dict[str, dict[str, str]]:
    """Render whole cards and refuse a silent failure: every case must produce markup.

    A card that threw inside node would return an empty string, and an empty string
    satisfies `x not in html` for every x — which is how a render test goes green on
    a broken panel.
    """
    spec = [
        {"name": name, "kind": "card", "data": data, "failure": failure}
        for name, data, failure in cases
    ]
    out = _run(tmp_path, spec)
    for item in spec:
        name = item["name"]
        assert out[name]["thrown"] is None, f"{name}: панель упала — {out[name]['thrown']}"
        assert out[name]["html"], f"{name} не напечатала ничего"
    return out


def test_every_case_renders_and_no_case_throws(tmp_path):
    """The harness is only evidence if the card actually produced markup."""
    out = _cards(tmp_path, [
        ("honoured", _card(samples=[_row()]), None),
        ("unread", {"readable": False, "reason": "plan_columns_missing", "note": "нечего мерить"}, None),
        ("empty", _card(
            population={"posts": 1226, "booked": 0, "unbooked": 1226, "scanned": 0,
                        "truncated": False, "limit": 400},
            states={code: {"count": 0, "basis": "population", "single_row": False} for code in STATE_CODES},
        ), None),
        ("transport", None, {"kind": "error", "message": "fetch failed"}),
    ])
    assert len(out) == 4


def test_the_three_silences_are_three_different_sentences(tmp_path):
    """«сеть не ответила», «сервер ответил, что не мерил», «постов с бронью нет».

    The brief's P34 rule, drawn where an operator reads it: one of the three may
    never be printed with another's words, because each asks for a different fix.
    """
    out = _cards(tmp_path, [
        ("transport", None, {"kind": "error", "message": "fetch failed"}),
        ("unread", {"readable": False, "reason": "plan_columns_missing",
                    "note": "Колонки брони ещё не созданы: сравнивать нечего, измерение невозможно."}, None),
        ("empty", _card(
            population={"posts": 1226, "booked": 0, "unbooked": 1226, "scanned": 0,
                        "truncated": False, "limit": 400},
            states={code: {"count": 0, "basis": "population", "single_row": False} for code in STATE_CODES},
            samples=[],
            note="Брони плана нет ни у одного поста.",
        ), None),
    ])
    transport, unread, empty = out["transport"]["html"], out["unread"]["html"], out["empty"]["html"]
    assert "ответ не пришёл" in transport and "ответа не было" in transport, transport
    assert "не прочитано" not in transport, "a failed request was worded as an unread store"
    assert "Бронированных постов нет" not in transport
    assert "причина: plan_columns_missing" in unread, unread
    assert "Запрос состояния брони не удался" not in unread
    assert "Бронированных постов нет" in empty, empty
    assert "не о чём спрашивать" in empty, empty
    assert "причина:" not in empty, "an empty store was reported as a broken reader"
    assert "Запрос состояния брони не удался" not in empty
    assert len({transport, unread, empty}) == 3


def test_a_card_that_measured_nothing_prints_no_digits(tmp_path):
    """The live base today: no `plan_slot_at` column, so the read cannot happen.

    Every count under an unreadable read is `None` (`_booking_unread`), and a card
    that printed `0` there would report a schedule nobody measured as one that
    matched everything it was asked about.
    """
    payload = {
        "readable": False,
        "reason": "plan_columns_missing",
        "reason_detail": "no such column: posts.plan_slot_at",
        "precision": "second",
        "gate": {"code": "slot_outside_local_hours", "count": None, "attribution": "tick",
                 "tick_reason": None},
        "population": {"posts": None, "booked": None, "unbooked": None, "scanned": None,
                       "truncated": None, "limit": 400},
        "states": {code: {"count": None, "basis": None, "single_row": None} for code in STATE_CODES},
        "shift": {"later": None, "earlier": None, "largest_abs_minutes": None},
        "samples": None,
        "note": "Колонки брони ещё не созданы в читаемом хранилище: до этого момента "
                "сравнивать нечего. Это не «брони нет» и не «совпало» — измерение невозможно.",
    }
    html = _cards(tmp_path, [("unread", payload, None)])["unread"]["html"]
    assert "не прочитано" in html
    assert "измерение невозможно" in html
    for state_word in ("уважено", "сдвинуто", "отказано"):
        assert state_word not in html, f"{state_word} printed for a read that did not happen"
    assert not re.search(r":\s*\d", html), f"a fabricated number in an unread card: {html}"


def test_the_six_states_are_printed_side_by_side_in_words(tmp_path):
    """A readable card names every state it was given, each with its own count."""
    counts = {"no_booking": 223, "honoured": 2, "shifted": 1, "refused": 4,
              "awaiting_slot": 5, "unread_row": 0}
    states = {code: {"count": count, "basis": "population", "single_row": count == 1}
              for code, count in counts.items()}
    payload = _card(states=states, population={"posts": 235, "booked": 12, "unbooked": 223,
                                               "scanned": 12, "truncated": False, "limit": 400})
    html = _cards(tmp_path, [("card", payload, None)])["card"]["html"]
    for code, count in counts.items():
        word = {
            "no_booking": "брони нет", "honoured": "уважено", "shifted": "сдвинуто",
            "refused": "отказано", "awaiting_slot": "слот ещё не выдан", "unread_row": "не читается",
        }[code]
        assert f"{word}: {count}" in html, f"{code} ({word}) не напечатано со своим числом"
    # One row is an observation, not a finding, and the flag comes from the server.
    assert "одна строка — не вывод" in html, html


def test_a_sample_of_the_window_says_it_is_a_window(tmp_path):
    """`basis: "sample"` counts the read rows, not the table: the card must not round it up."""
    states = {code: {"count": 1, "basis": "sample", "single_row": True} for code in STATE_CODES}
    payload = _card(states=states,
                    population={"posts": 900, "booked": 888, "unbooked": 12, "scanned": 400,
                                "truncated": True, "limit": 400})
    html = _cards(tmp_path, [("window", payload, None)])["window"]["html"]
    assert "по прочитанному окну" in html, html
    assert "окно ограничено: 400 строк" in html, html


def test_a_shift_is_printed_signed_and_in_the_units_measured(tmp_path):
    """+1 ч 30 мин and −45 мин are different facts; a magnitude alone is neither."""
    out = _run(tmp_path, [
        {"name": "later", "kind": "row", "row": _row(
            state="shifted", direction="later",
            scheduled_at="2096-11-05T10:30:00+00:00", shift_seconds=5400, shift_minutes=90.0)},
        {"name": "earlier", "kind": "row", "row": _row(
            state="shifted", direction="earlier",
            scheduled_at="2096-11-05T08:15:00+00:00", shift_seconds=-2700, shift_minutes=-45.0)},
        {"name": "seconds", "kind": "row", "row": _row(
            state="shifted", direction="later",
            scheduled_at="2096-11-05T09:00:30+00:00", shift_seconds=30, shift_minutes=0.5)},
        {"name": "matched", "kind": "row", "row": _row()},
        {"name": "unmeasured", "kind": "row", "row": _row(
            state="awaiting_slot", scheduled_at=None, shift_seconds=None, shift_minutes=None)},
    ])
    assert "сдвиг +1 ч 30 мин позже брони" in out["later"]["html"], out["later"]["html"]
    assert "−45 мин раньше брони" in out["earlier"]["html"], out["earlier"]["html"]
    assert "+" not in out["earlier"]["html"].split("сдвиг")[1][:8], "a pull-back printed a plus"
    # A 30-second move may not be rounded into "nothing moved" (P34, inverted).
    assert "сдвиг +30 с позже брони" in out["seconds"]["html"], out["seconds"]["html"]
    assert "0 мин" not in out["seconds"]["html"]
    # The measured zero is not the unmeasured one: both sentences must exist.
    assert "сдвиг 0 с — слот совпал с бронью" in out["matched"]["html"], out["matched"]["html"]
    assert "сдвиг не измерен" in out["unmeasured"]["html"], out["unmeasured"]["html"]
    assert out["matched"]["html"] != out["unmeasured"]["html"]
    assert "только что" not in out["later"]["html"], "a booking in 2096 read as an elapsed time"


def test_a_refused_row_names_the_gate_and_a_missing_code_names_nothing(tmp_path):
    """The state is only as specific as the ledger behind it."""
    refused = _row(state="refused", status="gated", scheduled_at=None, shift_seconds=None,
                   shift_minutes=None, refusal_gate_code="slot_outside_local_hours",
                   refusal_basis="rule_and_publish_tick_ledger")
    out = _run(tmp_path, [
        {"name": "refused", "kind": "row", "row": refused},
        {"name": "headless", "kind": "row",
         "row": dict(refused, refusal_gate_code=None)},
        {"name": "waiting", "kind": "row",
         "row": _row(state="awaiting_slot", status="gated", scheduled_at=None,
                     shift_seconds=None, shift_minutes=None)},
    ])
    assert "отказ по гейту slot_outside_local_hours" in out["refused"]["html"], out["refused"]["html"]
    assert "тику, не посту" in out["refused"]["html"], out["refused"]["html"]
    assert "заблокировано" not in out["refused"]["html"]
    assert "код гейта не прислан" in out["headless"]["html"], out["headless"]["html"]
    assert "slot_outside_local_hours" not in out["headless"]["html"], (
        "the panel invented the code the server did not send"
    )
    assert out["headless"]["html"] != out["refused"]["html"]
    # Waiting is not refused, and must not borrow the refusal's words.
    assert "отказ" not in out["waiting"]["html"], out["waiting"]["html"]
    assert "слот ещё не выдан" in out["waiting"]["html"]


def test_a_sent_row_adds_the_fact_without_replacing_the_slot(tmp_path):
    """`published_at` answers «а когда реально ушло» beside the booking, not instead of it."""
    out = _run(tmp_path, [
        {"name": "sent", "kind": "row", "row": _row(
            state="honoured", status="published", published_at="2096-11-05T09:10:00+00:00",
            published_shift_seconds=600, published_shift_minutes=10.0)},
        {"name": "exact", "kind": "row", "row": _row(
            state="honoured", status="published", published_at="2096-11-05T09:00:00+00:00",
            published_shift_seconds=0, published_shift_minutes=0)},
        {"name": "unsent", "kind": "row", "row": _row()},
    ])
    assert "отправлен" in out["sent"]["html"] and "+10 мин позже брони" in out["sent"]["html"]
    assert "бронь" in out["sent"]["html"] and "слот" in out["sent"]["html"], (
        "the send replaced the comparison instead of joining it"
    )
    assert "ровно по брони" in out["exact"]["html"], out["exact"]["html"]
    assert "не отправлен" in out["unsent"]["html"]
    assert "дельта к брони не измерена" not in out["sent"]["html"]


def test_an_unknown_state_word_is_admitted_rather_than_guessed(tmp_path):
    """A code the panel does not know prints itself as unknown, on both sides."""
    out = _run(tmp_path, [
        {"name": "chip", "kind": "chip", "code": "rescheduled_by_hand",
         "state": {"count": 7, "basis": "population", "single_row": False}},
        {"name": "row", "kind": "row", "row": _row(state="rescheduled_by_hand")},
        {"name": "nochip", "kind": "chip", "code": "honoured", "state": None},
    ])
    assert "не описано" in out["chip"]["html"], out["chip"]["html"]
    assert "rescheduled_by_hand" in out["chip"]["html"]
    assert "не прочитано" in out["row"]["html"], out["row"]["html"]
    assert "уважено" not in out["row"]["html"], "an unknown state borrowed a known word"
    # A state the server never counted is «не прочитано», never a zero.
    assert "не прочитано" in out["nochip"]["html"], out["nochip"]["html"]
    assert not re.search(r":\s*\d", out["nochip"]["html"]), out["nochip"]["html"]


def test_a_card_with_a_missing_section_still_admits_it(tmp_path):
    """Half a payload is not a whole one: absent blocks print unread, never empty."""
    payload = {"readable": True, "samples": [_row()]}
    html = _cards(tmp_path, [("half", payload, None)])["half"]["html"]
    assert "не прочитано" in html, html
    assert not re.search(r"(?:бронью|брони|строк): \d", html), html


def test_a_card_without_a_place_to_mount_is_not_an_error(tmp_path):
    """The node is created next to the consumer block; without it the card stays silent."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node недоступен — поведение панели не проверить")
    out = _run(tmp_path, [{"name": "nomount", "kind": "card", "data": _card(),
                           "failure": None, "mount_missing": True}])
    assert out["nomount"]["thrown"] is None, out["nomount"]["thrown"]
    assert out["nomount"]["html"] == ""


def test_hostile_stored_text_stays_text(tmp_path):
    """Every string below is stored text the API echoes raw."""
    hostile = '<img src=x onerror="fetch(/evil)">'
    out = _run(tmp_path, [
        {"name": "row", "kind": "row", "row": _row(post_id=hostile, platform=hostile,
                                                   refusal_gate_code=hostile, state="refused")},
        {"name": "card", "kind": "card", "data": _card(note=hostile, samples=[_row(post_id=hostile)]),
         "failure": None},
    ])
    for html in (out["row"]["html"], out["card"]["html"]):
        assert hostile not in html
        assert "&lt;img" in html, "the stored text is shown, as data"
        assert "<img" not in html


# ---------------------------------------------------------------- the seam test
@pytest.fixture()
def seeded_booking():
    """One booked post, written and read back through the endpoint the panel calls."""
    channel_id = f"w20cseamchan{uuid4().hex[:8]}"
    post_id = f"w20cseampost{uuid4().hex[:8]}"
    slot = (datetime.now(UTC) + timedelta(days=1)).replace(microsecond=0, second=0, minute=0)
    with session_scope() as session:
        session.add(
            Channel(
                id=channel_id, title="W20-C seam", niche="test", platform="telegram",
                timezone="UTC", enabled=True, posts_per_day=6,
            )
        )
        session.add(
            Post(
                public_id=post_id, channel_id=channel_id, hook="seam", body="seam",
                status="scheduled", plan_slot_at=slot, scheduled_at=slot + timedelta(minutes=20),
                created_at=datetime.now(UTC),
            )
        )
    try:
        yield post_id
    finally:
        with session_scope() as session:
            session.execute(delete(Post).where(Post.public_id == post_id))
            session.execute(delete(Channel).where(Channel.id == channel_id))


def _served_row(client: TestClient, post_id: str) -> dict[str, Any]:
    response = client.get(ENDPOINT)
    assert response.status_code == 200, response.text
    rows = [item for item in response.json()["samples"] if item["post_id"] == post_id]
    assert len(rows) == 1, (
        f"{post_id} не названа ровно одной строкой: "
        f"{[item['post_id'] for item in response.json()['samples']]}"
    )
    return rows[0]


def test_the_panel_only_dereferences_keys_the_server_sends(script, booking, client, seeded_booking):
    """One contract, checked against the live payload rather than a fixture.

    `tgfarm/api.py` and `tgfarm/ui/app.js` are edited in different worktrees, so the
    rename that survives review is the one each half's own test cannot see: this
    test reads the fields the block actually dereferences and requires each of them
    in the row and the card the endpoint actually sends.
    """
    row = _served_row(client, seeded_booking)
    payload = client.get(ENDPOINT).json()
    read = {
        "row": set(re.findall(r"(?<![A-Za-z0-9_$])r\.([a-z][a-z0-9_]*)", booking)),
        "card": set(re.findall(r"(?<![A-Za-z0-9_$])data\.([a-z][a-z0-9_]*)", booking)),
        "population": set(re.findall(r"(?<![A-Za-z0-9_$])population\.([a-z][a-z0-9_]*)", booking)),
        "gate": set(re.findall(r"(?<![A-Za-z0-9_$])gate\.([a-z][a-z0-9_]*)", booking)),
        "state": set(re.findall(r"(?<![A-Za-z0-9_$])view\.([a-z][a-z0-9_]*)", booking)),
    }
    assert read["row"], "the block names no row field at all"
    for key in read["row"]:
        assert key in row, f"панель читает r.{key}, эндпоинт его не присылает"
    for key in read["card"]:
        assert key in payload, f"панель читает data.{key}, карточки его не имеет"
    for key in read["population"]:
        assert key in payload["population"], f"панель читает population.{key}"
    for key in read["gate"]:
        assert key in payload["gate"], f"панель читает gate.{key}"
    assert read["state"] <= {"count", "basis", "single_row"}, read["state"]
    # The shifted row this fixture wrote is the row the assertions above ran on.
    assert row["state"] == "shifted" and row["shift_seconds"] == 1200, row


def test_the_endpoint_the_panel_calls_exists_in_the_api_source(script, client):
    """A card that fetches a route nobody serves is a permanent «ответ не пришёл».

    Checked in three places at once because the panel's own test can only see the
    string it typed: the markup asks for the path, `api.py` declares it, and the
    running app answers it.
    """
    assert '"/api/editorial/plan/booking"' in script
    api_source = (ROOT / "tgfarm" / "api.py").read_text(encoding="utf-8")
    assert '@app.get("/api/editorial/plan/booking")' in api_source
    assert client.get(ENDPOINT).status_code == 200


def test_the_panel_app_still_parses() -> None:
    """Cheap and total: a panel the browser cannot parse renders nothing at all."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node недоступен — панель не проверить")
    done = subprocess.run(  # noqa: S603 - fixed executable, no shell
        [node, "--check", str(UI_DIR / "app.js")],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
