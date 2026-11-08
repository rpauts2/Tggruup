"""UI contract for the attribution card — «сколько вообще можно было измерить».

The coverage card (`tests/test_ui_coverage_panel.py`) answers *how many
publications are measured*. Nothing in the panel answered how many could ever
have been measured, so a 0 % coverage caused by a missing `tg_message_id` looked
identical to a 0 % coverage caused by a collector that never ran.
`GET /api/metrics/attribution` (`tgfarm/s8_analytics/attribution.py::
attribution_report`) now ships that split, and on the live base it says: 528
published Telegram-side posts, **0** of them carrying `tg_message_id`, 22
`post_stats` rows in the whole database.

These tests read `tgfarm/ui/app.js` the way a browser does — served through the
app, sliced by full literal anchors (the technique of
`tests/test_ui_coverage_panel.py`, `tests/test_ui_backlog_card.py` and
`tests/test_ui_unresolved_worklist.py`) — and then run the renderer for real
under node, because only the rendered string can tell «нет отчёта» from a
printed zero. Pinned rules:

* the fetch path is `/api/metrics/attribution`, and that path is a route the API
  registers;
* a `null` coverage prints «нет отчёта», never `0.0 %` — and the null branch is
  driven with a hand-built payload, since the endpoint as wired today
  (`api.py::metrics_attribution_api` passes no `platforms=`) cannot emit one;
* `unknowable` rides on the same line as the coverage number, so the two failure
  modes stay distinguishable on screen;
* every server-supplied string reaches the DOM through `esc()`;
* the whole script still passes `node --check`.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from tgfarm.db.models import Channel, CrosspostLog, Post, PostStat
from tgfarm.db.session import session_scope

UI_DIR = Path(__file__).resolve().parent.parent / "tgfarm" / "ui"

# Anchors in full, never as short substrings: slicing a function out of app.js
# with a reused two-word anchor silently returns the wrong body.
HELPERS_START = "  const $ = (sel, root = document) => root.querySelector(sel);"
HELPERS_END = "  // The admin API can force-publish"
GAP_TABLE_START = "  const ATTRIBUTION_GAP = {"
CARD_START = "  function renderMetricsAttribution(payload, failure) {"
CARD_END = "  async function refresh() {"
FETCH_ANCHOR = '        metricsCoverage: "/api/metrics/coverage",'
DISPATCH_ANCHOR = '      if ("metricsCoverage" in data) renderMetricsCoverage(data.metricsCoverage);'

ROOT_ID = "#metrics-attribution"


@pytest.fixture(scope="module")
def client():
    from tgfarm.api import create_app
    from tgfarm.core.apiauth import ensure_token

    # /ui/ is a public prefix, /api/ is not: the one header covers both reads.
    with TestClient(
        create_app(with_scheduler=False),
        base_url="http://127.0.0.1",
        headers={"X-TGFarm-Token": ensure_token()},
    ) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def script(client) -> str:
    """Fetched through the app, the way the browser gets it."""
    return client.get("/ui/app.js").text


@pytest.fixture(scope="module")
def card(script: str) -> str:
    return _card_body(script)


def _slice(text: str, start_marker: str, end_marker: str) -> str:
    """Same slicing as `test_ui_evidence_and_cancellation.py::_slice`: a missing
    anchor raises ValueError, which is a louder failure than a silent empty body."""
    start = text.index(start_marker)
    return text[start : text.index(end_marker, start)]


def _card_body(script: str) -> str:
    """The renderer alone, sliced on `\\n  }` — the first dedent to the top
    function level after its start. Written without the trailing newline because
    `core.autocrlf` makes the file CRLF on some checkouts and LF in the blob."""
    return _slice(script, CARD_START, "\n  }")


def _attribution_block(script: str) -> str:
    return _slice(script, GAP_TABLE_START, CARD_END)


# --------------------------------------------------------------------- anchors
def test_the_anchors_the_file_slices_on_are_all_present(script):
    """A moved anchor must fail here, loudly, not silently hand a wrong body to
    every assertion below."""
    for anchor in (
        HELPERS_START,
        HELPERS_END,
        GAP_TABLE_START,
        CARD_START,
        CARD_END,
        FETCH_ANCHOR,
        DISPATCH_ANCHOR,
        '        metricsAttribution: "/api/metrics/attribution?days=30",',
        'const existing = $("#metrics-attribution");',
        'renderMetricsAttribution("metricsAttribution" in data ? data.metricsAttribution : null,',
    ):
        assert anchor in script, f"anchor gone from app.js: {anchor!r}"


# ---------------------------------------------------------------------- wiring
def test_the_card_is_fetched_and_dispatched_in_the_shared_refresh(script):
    body = _slice(script, "async function refresh()", "async function runCycle()")
    assert 'metricsAttribution: "/api/metrics/attribution?days=30"' in body
    # Dispatched the way the neighbouring analytics cards are, and re-rendered on
    # the pass where the read died — an old attribution would report a split that
    # no longer holds.
    assert 'renderMetricsAttribution("metricsAttribution" in data ? data.metricsAttribution : null,' in body
    assert "reasons.metricsAttribution" in body
    assert 'new Error("атрибуция не получена")' in body
    assert "Promise.allSettled(" in body


def test_the_endpoint_the_panel_fetches_is_a_route_the_api_registers(client, script):
    """A route rename must fail here, not show up as an empty card."""
    path = re.search(r'metricsAttribution:\s*"([^"?]+)', script).group(1)
    assert path == "/api/metrics/attribution", "the card must call this path, spelled exactly"
    get_routes = {
        route.path for route in client.app.routes if "GET" in getattr(route, "methods", set())
    }
    assert path in get_routes
    assert client.get(path, params={"days": 30}).status_code == 200


def test_the_card_is_mounted_beside_the_coverage_cards(script):
    """This wave owns the script only, so the node is created the way
    `renderAlerts` creates `#alert-banner`: looked up, inserted when absent."""
    mount = _slice(script, "  function attributionRoot() {", CARD_START)
    assert 'const existing = $("#metrics-attribution");' in mount
    assert 'const anchor = $("#metrics-coverage-dzen") || $("#metrics-coverage");' in mount
    assert "document.createElement" in mount
    assert 'anchor.insertAdjacentElement("afterend", node);' in mount
    # No bespoke block: the card is the same .item/.item-side family the panel
    # already styles, so this wave needed no CSS rule at all.
    sheet = (UI_DIR / "app.css").read_text(encoding="utf-8")
    for selector in (".item ", ".item-main", ".item-side", ".item-meta", ".coverage-attribution"):
        assert selector in sheet, f"{selector} is rendered but never styled"


# ------------------------------------------------------------------ shape lock
def _seed(platform: str, *, telegram: bool = False, with_id: bool = False, stats: int = 0):
    suffix = uuid4().hex[:8]
    channel_id = f"attrui_{platform}_{suffix}"
    with session_scope() as session:
        session.add(Channel(id=channel_id, title=platform, niche="news", platform=platform))
        for index in range(3):
            post = Post(
                public_id=f"attrui{suffix}{index}",
                channel_id=channel_id,
                status="published",
                published_at=datetime.now(UTC),
                hook=f"Заголовок публикации {index} достаточной длины",
                body="Текст",
                tg_message_id=(index * 900 + 1) if (with_id and index == 0) else None,
            )
            session.add(post)
            session.flush()
            if stats and index == 0:
                session.add(PostStat(post_id=post.id, views=40, reactions=3))
            if telegram:
                continue
            session.add(
                CrosspostLog(
                    post_public_id=post.public_id,
                    platform=platform,
                    status="posted",
                    # Only the first publication of the bucket is nameable: the
                    # rest is exactly the `unknowable` share the card reports.
                    remote_id=f"remote-{index}" if (with_id and index == 0) else None,
                    url=f"https://example.com/{index}",
                    created_at=datetime.now(UTC),
                )
            )
        if not stats:
            # A stat row attached to a *draft*: the collector wrote it, and
            # nothing published owns it.
            draft = Post(
                public_id=f"attrui{suffix}orphan",
                channel_id=channel_id,
                status="draft",
                hook="Заголовок сиротского замера достаточной длины",
                body="Текст",
            )
            session.add(draft)
            session.flush()
            session.add(PostStat(post_id=draft.id, views=7))


@pytest.fixture()
def _clean_analytics():
    """The session database is shared, so this file cleans before and after: a
    leftover published post from an earlier file widens every bucket below."""
    def _wipe() -> None:
        with session_scope() as session:
            ids = session.execute(select(Post.id).where(Post.public_id.like("attrui%"))).scalars().all()
            if ids:
                session.execute(delete(PostStat).where(PostStat.post_id.in_(list(ids))))
            session.execute(delete(Post).where(Post.public_id.like("attrui%")))
            session.execute(
                delete(CrosspostLog).where(CrosspostLog.post_public_id.like("attrui%"))
            )
            session.execute(delete(Channel).where(Channel.id.like("attrui_%")))

    _wipe()
    yield
    _wipe()


def test_every_field_the_panel_reads_exists_in_the_endpoint_response(client, card, _clean_analytics):
    """The shape the UI reads is the shape the API returns, scraped from the
    renderer rather than restated here."""
    _seed("telegram", telegram=True, with_id=False)
    _seed("dzen", telegram=False, with_id=True, stats=1)
    payload = client.get("/api/metrics/attribution", params={"days": 30}).json()

    top_level = set(re.findall(r"\bdata\.([a-z_][a-z_]*)", card))
    row_keys = set(re.findall(r"\brow\.([a-z_][a-z_]*)", card))
    # The scrape itself is pinned: a rewrite to bracket access would make these
    # sets empty and every assertion below vacuous.
    assert {"platforms", "orphan_measurements", "window_days"} <= top_level, sorted(top_level)
    assert {
        "publications", "with_external_id", "with_stats", "unknowable",
        "coverage", "last_measurement_at",
    } <= row_keys, sorted(row_keys)

    telegram = payload["platforms"]["telegram"]
    dzen = payload["platforms"]["dzen"]
    for key in top_level:
        assert key in payload, f"panel reads {key!r} at the top level; endpoint has no such key"
    for name, row in payload["platforms"].items():
        for key in row_keys:
            assert key in row, f"panel reads {key!r} per platform; {name} row omits it"

    # The live fact the module exists to surface, in miniature. Counts are
    # floors, not equalities: this database is session-scoped and other UI files
    # seed published posts too, and the Telegram bucket is a whole-platform
    # aggregate. What must hold regardless is the *split*.
    assert telegram["publications"] >= 3
    assert telegram["with_external_id"] < telegram["publications"]
    assert telegram["unknowable"] == telegram["publications"] - telegram["with_external_id"]
    assert telegram["unknowable"] >= 3
    assert telegram["coverage"] is not None, "0 of n is a computed ratio, not an absence"
    assert 0.0 <= telegram["coverage"] < 1.0
    assert dzen["unknowable"] >= 1
    assert dzen["with_external_id"] >= 1
    assert dzen["with_stats"] <= dzen["with_external_id"], "a number needs an id to attach to"
    assert payload["orphan_measurements"] >= 1, "a stat row on a draft must be counted as an orphan"
    assert "error" not in payload, "the endpoint has no error key; the card must not invent one"


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
const render = new Function("document", "window", body + "\n; return renderMetricsAttribution;")(
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
  out.push({ name: item.name, html: nodes[item.root]["innerHTML"] });
}
process.stdout.write(JSON.stringify(out));
"""

SLICES = [[HELPERS_START, HELPERS_END], [GAP_TABLE_START, CARD_END]]


def _render_cases(tmp_path: Path, cases: list[dict]) -> dict[str, str]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node недоступен — поведение панели не проверить")
    harness = tmp_path / "harness.cjs"
    harness.write_text(HARNESS, encoding="utf-8")
    spec_path = tmp_path / "cases.json"
    spec_path.write_text(
        json.dumps(
            {
                "slices": SLICES,
                "cases": [
                    {
                        "name": case["name"],
                        "ids": [ROOT_ID],
                        "root": ROOT_ID,
                        "payload": case.get("payload"),
                        "failure": case.get("failure"),
                    }
                    for case in cases
                ],
            },
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
    return {item["name"]: item["html"] for item in json.loads(done.stdout)}


def _row(**overrides) -> dict:
    row = {
        "publications": 528,
        "with_external_id": 0,
        "with_stats": 0,
        "unknowable": 528,
        "coverage": 0.0,
        "share_unknowable": 1.0,
        "last_measurement_at": None,
    }
    row.update(overrides)
    return row


def _payload(**overrides) -> dict:
    payload = {
        "platforms": {"telegram": _row()},
        "orphan_measurements": 22,
        "window_days": 30,
        "generated_at": "2026-11-06T10:00:00+00:00",
    }
    payload.update(overrides)
    return payload


def test_the_whole_script_still_parses(client, tmp_path):
    """No build step, so `node --check` is the only syntax gate the panel has."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node недоступен — синтаксис не проверить")
    done = subprocess.run(  # noqa: S603 - fixed executable, one file
        [node, "--check", str(UI_DIR / "app.js")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert "function renderMetricsAttribution(payload, failure) {" in client.get(
        "/ui/app.js"
    ).text


def test_a_coverage_the_server_could_not_compute_is_not_a_zero(tmp_path):
    """`coverage = None` means «нечего делить», and the card must say so.

    `pct()` ends in `|| 0`, so handing it the null would print `0.0%` — exactly
    the zero `summarize_attribution` refused to send. The endpoint as wired today
    never forces an empty bucket, so this payload is built by hand: the card's
    contract is the shape the module documents, not one server day's output.
    """
    rendered = _render_cases(
        tmp_path,
        [{"name": "empty_bucket", "payload": _payload(platforms={
            "vk": _row(publications=0, with_external_id=0, with_stats=0,
                       unknowable=0, coverage=None, share_unknowable=None),
        })}],
    )
    html = rendered["empty_bucket"]
    assert "нет отчёта" in html, "a null coverage must print the honest marker"
    for zero in ("0.0%", "0,0%", "измеримо 0"):
        assert zero not in html, f"a null denominator was rendered as the number {zero!r}"
    # And the absence of the thing must not be silently dropped either.
    assert "0 % без id и 0 % с id" in html, "the card must say what it means on one line"


def test_a_measured_zero_and_a_missing_id_are_not_the_same_screen(tmp_path):
    """The whole point of the card: two 0 % rows, two different sentences.

    `unknowable` must sit on the same line as the coverage number — split across
    two lines an operator compares nothing.
    """
    rendered = _render_cases(
        tmp_path,
        [
            # Live Telegram case: every publication lacks `tg_message_id`.
            {"name": "no_id", "payload": _payload()},
            # The ids exist, the collector stayed home.
            {"name": "idle_collector", "payload": _payload(platforms={"vk": _row(
                publications=528, with_external_id=528, with_stats=0, unknowable=0,
                coverage=0.0)})},
        ],
    )
    no_id = rendered["no_id"]
    idle = rendered["idle_collector"]
    assert "измеримо 0.0%" in no_id and "измеримо 0.0%" in idle
    assert "нет внешнего id — измерить нельзя" in no_id
    assert "id есть, снимков нет" in idle
    assert "нет внешнего id" not in idle and "id есть, снимков нет" not in no_id
    # Same line, both numbers, for each row.
    for html in (no_id, idle):
        line = next(chunk for chunk in html.split('<div class="item-meta">')
                    if "измеримо" in chunk)
        assert "не привязываемо" in line, f"coverage and unknowable printed apart: {line}"
    assert "528" in no_id


def test_the_failure_paths_print_no_numbers(tmp_path):
    """A dead read is not a quiet platform: no digit may reach the screen."""
    rendered = _render_cases(
        tmp_path,
        [
            {"name": "network", "failure": "атрибуция не получена"},
            {"name": "empty_report", "payload": _payload(platforms={})},
        ],
    )
    network = rendered["network"]
    assert "Атрибуция метрик не прочитана" in network
    assert "атрибуция не получена" in network
    assert "Ни одной цифры не показано" in network
    assert "0" not in re.sub(r"class=\"[^\"]*\"|<[^>]+>", "", network), "a digit leaked"
    # An empty report is its own sentence, not a zero coverage either.
    assert "0 % измерено" in rendered["empty_report"]
    assert "измеримо" not in rendered["empty_report"]


def test_every_server_string_reaches_the_dom_through_esc(card, script):
    """The platform key is `crosspost_logs.platform` straight from the database,
    and the panel renders foreign titles — nothing raw may be interpolated."""
    for needle in ("esc(platform)", "esc(dt(row.last_measurement_at))",
                   "esc(gap.label)", "esc(items(data.orphan_measurements, \"\"))",
                   "esc(message.slice(0, 160))"):
        assert needle in card, f"{needle} missing: remote text must stay inert"
    assert "innerHTML" in card
    assert "innerHTML = payload" not in card
    # The escape helper is the panel's own, not a new one.
    assert "  const esc = (s) =>" in script
