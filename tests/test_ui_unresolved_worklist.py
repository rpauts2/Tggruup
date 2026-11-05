"""The overview must name the unresolved publications it counts — and say how many.

`/api/backlog` answers «что происходит с очередью» with counts, and one of those
counts is `publish_unknown`: rows whose publication outcome nobody can prove. On
the live base (2026-11-05, read-only `sqlite3.connect("file:…?mode=ro", uri=True)`)
there were 51 of them, created 2026-10-29, ranked 7794…9368 newest-first out of
21124 posts. The overview queue renders `/api/posts?limit=40` — newest first — so
not one of those 51 could ever be shown there: the card printed the number and
hid every name beside it.

The endpoint now sends the ten oldest plus the aggregate that says how many exist,
and this file pins what the panel is allowed to do with it:

* a capped list is shown **with its denominator** — «показаны 10 из 51», never a
  truncated list presented as the whole set;
* `null` total is «не прочитано», `0` total is a measured nothing, and the two
  print in different words (`n()` ends in `|| 0`, so it may never be handed a null);
* every string from the database — hook, channel, redacted publish error — goes
  through `esc()`; a post's hook is untrusted text and is never an instruction;
* the block offers the reconcile action and performs none: its buttons hand over to
  `window.tgfarmReconcile`, the drawer's own function with its own `confirm()` gate
  and its own single POST to `/api/posts/{public_id}/reconcile` (§80).

Rendering is run for real under node, because only the rendered string can tell
«не прочитано» from a printed zero.
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

# Anchors in full, never as short substrings: slicing a function out of app.js with
# a reused two-word anchor silently returns the wrong body.
HELPERS_START = "  const $ = (sel, root = document) => root.querySelector(sel);"
HELPERS_END = "  // The admin API can force-publish"
HRS_START = "  const hrs = (v) =>"
HRS_END = "  // The ratio that makes the problem visible"
PLATFORMS_START = "  const PLATFORM_RU = {"
PLATFORMS_END = "  function postCard(p) {"
WORKLIST_START = "  function renderUnresolvedWorklist(payload, failure) {"
WORKLIST_END = "  const NICHE_COLOR = {"

ROOT_ID = "#overview-unresolved"


@pytest.fixture(scope="module")
def client():
    from tgfarm.api import create_app
    from tgfarm.core.apiauth import HEADER, ensure_token

    with TestClient(
        create_app(with_scheduler=False),
        base_url="http://127.0.0.1",
        headers={HEADER: ensure_token()},
    ) as test_client:
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
def worklist(script) -> str:
    return _slice(script, WORKLIST_START, WORKLIST_END)


# ------------------------------------------------------------------- wiring
def test_the_block_lives_under_the_queue_it_explains(client):
    html = client.get("/").text
    assert 'id="overview-queue"' in html
    assert 'id="overview-unresolved"' in html
    assert html.index("overview-queue") < html.index("overview-unresolved"), (
        "the named rows belong under the nearest-publications list they complete"
    )


def test_the_panel_renders_on_the_snapshot_the_counts_come_from(script, worklist):
    """Same payload as the queue card, re-rendered on the pass where it died."""
    assert ROOT_ID in worklist
    start = script.index("renderUnresolvedWorklist(", script.index(WORKLIST_END) + 1)
    call = script[start : start + 300]
    assert 'renderUnresolvedWorklist("backlog" in data ? data.backlog : null,' in call
    assert "reasons.backlog" in call, "the failure is handed to the renderer"
    assert 'new Error("очередь не получена")' in call


def test_the_offline_class_names_are_styled():
    """A class the JS adds and the stylesheet never defines is an invisible change."""
    sheet = APP_CSS.read_text(encoding="utf-8")
    for token in ("is-unresolved-list", "unresolved-head", "is-unresolved",
                  "unresolved-actions", "is-unresolved-id", "is-unresolved-error"):
        assert f".{token}" in sheet, f"{token} is rendered but never styled"


# --------------------------------------------------------- no truncated silence
def test_a_capped_list_prints_its_denominator(worklist):
    assert "показаны ${n(shown)} из ${n(total)} самых старых" in worklist
    assert "показаны все ${n(total)}" in worklist
    assert "сколько их всего — не прочитано" in worklist
    assert "неподтверждённых публикаций нет" in worklist


def test_no_number_helper_is_handed_a_null(worklist):
    """`n()` and `pct()` end in `|| 0`; a null must be caught before them."""
    code = "\n".join(
        line.split("//", 1)[0] for line in worklist.splitlines()
    )
    assert "Number(" not in code, "a fabricated zero is one `|| 0` away"
    assert 'typeof block.total === "number"' in code
    assert 'typeof block.shown === "number"' in code
    assert 'typeof row.attempts === "number"' in code
    assert 'typeof row.age_hours === "number"' in code


# ------------------------------------------------------------- data stays data
def test_every_database_string_reaches_the_dom_through_esc(worklist):
    for needle in (
        "esc(row.hook",
        "esc(channel)",
        "esc(row.error)",
        "esc(row.public_id)",
        "esc(denominator)",
        "esc(message)",
        "esc(data.error)",
    ):
        assert needle in worklist, f"{needle} comes from the database and must be escaped"


def test_the_offering_never_performs(script, worklist):
    """§80: this block hands the click to the drawer's own consented function.

    No fetch, no POST, no route of its own — the reconcile path stays exactly one
    (`window.tgfarmReconcile` → `/api/posts/{id}/reconcile`), and the gate that
    makes it an operator decision instead of a retry lives in that function.
    """
    assert "tgfarmReconcile" not in worklist, "the renderer draws, it does not act"
    assert "/api/" not in worklist, "no second write path may appear in the markup"
    assert "fetch(" not in worklist and "post(" not in worklist

    listener = _slice(
        script,
        f"  const unresolvedList = $(\"{ROOT_ID}\");",
        "  const trendsList = $(\"#trends-list\");",
    )
    assert "closest(" in listener and '("[data-unresolved-action]")' in listener
    assert "window.tgfarmReconcile(button.dataset.unresolvedId, " in listener
    assert "addEventListener" in listener, "one delegated listener, not one per row"
    assert "/api/" not in listener and "fetch(" not in listener

    gate = _slice(
        script,
        "  window.tgfarmReconcile = async (id, action) => {",
        "  window.tgfarmRecommendCommunityTarget = async",
    )
    assert 'if (!confirm("Подтверждаете, что пост уже появился на площадке?"))' in gate
    assert 'if (!confirm("Вы лично проверили площадку' in gate
    assert "body.confirm_not_published = true" in gate, (
        "the retry keeps the explicit confirmation the route demands"
    )
    assert "/reconcile" in gate, "and the existing route, never a new one"


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
const render = new Function("document", "window", body + "\n; return renderUnresolvedWorklist;")(
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
    [HRS_START, HRS_END],
    [PLATFORMS_START, PLATFORMS_END],
    [WORKLIST_START, WORKLIST_END],
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


def _item(**overrides) -> dict:
    row = {
        "public_id": "pk0aaaabbb",
        "hook": "Как собрать очередь без дублей",
        "channel_id": "ch_dzen",
        "channel_title": "Дзен канал",
        "platform": "dzen",
        "created_at": "2026-10-29T13:05:50.911985+00:00",
        "age_hours": 160.5,
        "attempts": 2,
        "error": "HTTP 401 {'access_token': '***'}",
    }
    row.update(overrides)
    return row


def _block(**overrides) -> dict:
    block = {
        "status": "publish_unknown",
        # The server's own shape: `shown` is the length of `items`, `total` is a
        # separate aggregate, `limit` is the cap the SQL carried.
        "total": 51,
        "shown": 2,
        "limit": 10,
        "items": [_item(), _item(public_id="pk1cccdddd", hook="Вторая строка", attempts=0)],
    }
    block.update(overrides)
    return block


def test_rendered_cap_is_shown_as_a_part_of_a_bigger_set(tmp_path):
    html = _render_cases(
        tmp_path, [{"name": "capped", "payload": {"unresolved": _block()}}]
    )["capped"]

    assert "показаны 2 из 51 самых старых" in html
    assert "карточка называет не больше 10 строк за раз" in html
    assert "Не подтверждено, ушло ли" in html
    assert "Сверка" in html, "the full list lives on the review tab; the card says so"
    assert "Как собрать очередь без дублей" in html and "Вторая строка" in html
    assert "Дзен канал" in html and "Дзен" in html
    assert "ждёт сверки 161 ч" in html, "hours, rounded: the unit the gates speak"
    assert "попыток 2" in html and "попыток 0" in html
    assert "pk0aaaabbb" in html and "pk1cccdddd" in html
    # both reconcile offers, on the row they address
    assert html.count('data-unresolved-action="confirm_published"') == 2
    assert html.count('data-unresolved-action="retry_after_check"') == 2
    assert html.count('data-unresolved-id="pk0aaaabbb"') == 2, "both buttons address the row"
    assert '<div class="item-meta is-unresolved-id">pk0aaaabbb</div>' in html, (
        "and the operator reads the id without opening the drawer"
    )


def test_rendered_measured_zero_is_not_an_unread_one(tmp_path):
    html = _render_cases(tmp_path, [{
        "name": "zero",
        "payload": {"unresolved": _block(total=0, shown=0, items=[])},
    }])["zero"]

    assert "неподтверждённых публикаций нет" in html
    assert "не прочитано" not in html, "a measured nothing must not borrow the failure wording"
    assert "data-unresolved-action" not in html


def test_rendered_unread_total_refuses_to_print_a_zero(tmp_path):
    html = _render_cases(tmp_path, [{
        "name": "unread",
        "payload": {"unresolved": _block(total=None, shown=0, items=[])},
    }])["unread"]

    assert "сколько их всего — не прочитано" in html
    assert "из 0" not in html and "показаны" not in html


def test_rendered_full_list_says_there_is_nothing_left_behind(tmp_path):
    html = _render_cases(tmp_path, [{
        "name": "all",
        "payload": {"unresolved": _block(total=1, shown=1, items=[_item()])},
    }])["all"]

    assert "показаны все 1 — ждать сверки больше некому" in html
    assert "из 1 самых старых" not in html


def test_rendered_three_silences_keep_three_wordings(tmp_path):
    rendered = _render_cases(tmp_path, [
        {"name": "network", "payload": None, "failure": "Failed to fetch"},
        {"name": "http", "payload": None, "failure": "/api/backlog -> 500 Internal Server Error"},
        {"name": "read", "payload": {"error": "no such table: posts", "unresolved": None}},
        {"name": "absent", "payload": {}},
    ])

    assert "Сеть не отвечает" in rendered["network"]
    assert "Failed to fetch" in rendered["network"]
    assert "Список неподтверждённых публикаций не прочитан" in rendered["http"]
    assert "Чтение очереди не удалось" in rendered["read"]
    assert "no such table: posts" in rendered["read"]
    assert "не «неподтверждённых нет»" in rendered["read"]
    assert "не пришёл в ответе" in rendered["absent"]
    for name, html in rendered.items():
        assert "Не подтверждено, ушло ли" not in html, f"{name} invented a worklist"


def test_a_hook_is_data_and_never_markup(tmp_path):
    """Prompt injection: post text arrives from the world, not from this app."""
    hostile = '<img src=x onerror="fetch(/evil)">'
    html = _render_cases(tmp_path, [{
        "name": "hostile",
        "payload": {"unresolved": _block(
            total=1, shown=1, limit=1,
            items=[_item(hook=hostile, channel_title=hostile, error=hostile)],
        )},
    }])["hostile"]

    assert hostile not in html
    assert "&lt;img src=x onerror=&quot;fetch(/evil)&quot;&gt;" in html
    assert html.count("<img") == 0, "no image tag may be built out of a database column"


# ------------------------------------------------------------------ shape lock
def test_the_panel_reads_only_fields_the_endpoint_sends(client, worklist):
    """Every `row.*` and `block.*` the renderer touches must exist in the payload."""
    payload = client.get("/api/backlog").json()
    block = payload["unresolved"]
    assert block is not None, payload["error"]

    read_block = set(re.findall(r"\bblock\.([a-z_][a-z0-9_]*)", worklist))
    read_row = set(re.findall(r"\brow\.([a-z_][a-z0-9_]*)", worklist))
    assert read_block and read_row, "the lock is vacuous: the renderer names no fields"
    for key in read_block:
        assert key in block, f"panel reads unresolved.{key}; the endpoint omits it"
    if block["items"]:
        for key in read_row:
            assert key in block["items"][0], f"panel reads {key}; a named row omits it"
    else:
        # A fresh test database can legitimately hold no unresolved post at all;
        # then the contract is the shape `_backlog` builds, asserted by name.
        assert read_row <= {
            "public_id", "hook", "channel_id", "channel_title",
            "platform", "created_at", "age_hours", "attempts", "error",
        }, "the renderer reached for a field this block never sends"
