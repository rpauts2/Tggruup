"""The freshness card must show the collector half of the report it is given.

``tgfarm/s8_analytics/freshness.py`` answers three separate questions per
collector and keeps them in separate fields: the schedule claim (``job_id``,
``interval_hours``, ``scheduled``), the data claim (``rows``, ``age_hours``,
``data_status``, ``reason``) and the attempt claim (``runs_recorded``,
``last_run_at``, ``last_run_status``). On the live database the sentence that
matters most was computed, returned by ``/api/metrics/freshness`` and never
rendered: ``renderMetricsFreshness`` in ``tgfarm/ui/app.js`` read only
``payload.platforms`` and ``summary.ok/stale/never/blocked``, so «задача стоит в
расписании, но нового снимка не оставляла» stayed invisible while the extra keys
were ignored harmlessly — which is precisely why nobody noticed.

Three layers are pinned here.

* The endpoint builds the report once per request instead of twice, with the
  session count measured through a patched ``session_scope``, not assumed.
* The summary the panel reads is derived from that same detail and is *equal* to
  ``freshness.freshness_summary`` on identical facts: the guard that makes the
  derivation honest instead of a second, silently diverging implementation. Both
  counters are fed one report object — once the report read from the database, once
  a synthetic payload carrying every status a live database may never show. This is
  how the leaked loop variable that stamped the freshest snapshot with the *last*
  platform's status was caught: over two separate database reads it hid as a race.
* The card renders the block, and P34 holds inside it — a value the payload never
  carried prints «нет отчёта», ``rows: 0`` prints differently from ``rows: null``,
  nothing in the new path goes through ``Number(x || 0)``, and every string that
  came from outside the file arrives in the DOM escaped.

The escaping and the no-report branches are also run for real: the collector
block is a pure string function, so it is sliced out of ``app.js`` and evaluated
under node against a stub DOM. A text assertion alone cannot tell «нет отчёта»
from a fabricated zero that was printed, and that difference is the defect.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

UI_DIR = Path(__file__).resolve().parent.parent / "tgfarm" / "ui"

# Anchors are spelled out in full, not as short substrings: slicing a function out
# of app.js has bitten people before, and a reused two-word anchor silently returns
# the wrong body — or the right one, truncated.
HELPERS_START = "const $ = (sel, root = document) => root.querySelector(sel);"
# `esc`, `n`, `dt` and `hrs` are the file's own formatters, and the collector block
# calls all four: the slice has to run to just before the next helper it does NOT
# need, or the harness fails on a ReferenceError instead of on a broken claim.
HELPERS_END = "const clock = (v) => {"
HOURS_START = 'const hrs = (v) => (typeof v === "number"'
HOURS_END = "const leadVs = (v) =>"
BLOCK_START = 'const NO_REPORT = "нет отчёта";'
BLOCK_END = "function renderMetricsFreshness(payload) {"
CARD_START = "function renderMetricsFreshness(payload) {"
CARD_END = "// `blocked` and `unmeasured` each cover"

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)

COLLECTOR_KEYS = (
    "job_id",
    "snapshot_table",
    "scheduled",
    "interval_hours",
    "rows",
    "age_hours",
    "data_status",
    "reason",
    "runs_recorded",
    "last_run_at",
    "last_run_status",
    "scheduled_but_silent",
)


def _token() -> str:
    from tgfarm.core.apiauth import ensure_token

    return ensure_token()


@pytest.fixture(scope="module")
def client():
    from tgfarm.api import create_app

    with TestClient(
        create_app(with_scheduler=False),
        base_url="http://127.0.0.1",
        headers={"X-TGFarm-Token": _token()},
    ) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def script(client) -> str:
    return client.get("/ui/app.js").text


def _slice(text: str, start: str, end: str, *, unique: bool = True) -> str:
    if unique:
        assert text.count(start) == 1, f"якорь встречается не один раз: {start!r}"
    head = text.index(start)
    assert end in text[head:], f"конечный якорь не найден после start: {end!r}"
    return text[head : text.index(end, head)]


def _code_only(text: str) -> str:
    """Comments out: «no fabricated zero» is a rule about code, not about prose.

    The card comments quote the very idiom they forbid, so a naive substring check
    would fail on the explanation of the defect rather than on the defect.
    """
    without_blocks = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(line.split("//", 1)[0] for line in without_blocks.splitlines())


@pytest.fixture(scope="module")
def collector_block(script) -> str:
    return _slice(script, BLOCK_START, BLOCK_END)


@pytest.fixture(scope="module")
def freshness_card(script) -> str:
    return _slice(script, CARD_START, CARD_END)


# ------------------------------------------------------------------ api: one read


def test_freshness_endpoint_reads_the_database_once_not_twice(monkeypatch):
    """Four sessions per refresh became two: the summary is counted, not re-collected.

    ``metrics_freshness()`` opens one session for ``post_stats`` and one for the
    collector tables, and ``freshness_summary()`` builds that whole report again
    inside itself — so the old endpoint paid four sessions to answer one question.
    The count is measured through the module's own ``session_scope`` rather than
    read off the source.
    """
    import tgfarm.s8_analytics.freshness as freshness
    from tgfarm.api import create_app

    opened: list[str] = []
    real = freshness.session_scope

    def counting_scope():
        opened.append("session")
        return real()

    monkeypatch.setattr(freshness, "session_scope", counting_scope)

    freshness.metrics_freshness()
    assert len(opened) == 2, "одна сборка отчёта = ровно две сессии"

    opened.clear()
    freshness.metrics_freshness()
    freshness.freshness_summary()
    assert len(opened) == 4, "прежняя пара вызовов = четыре сессии на один запрос"

    opened.clear()
    with TestClient(
        create_app(with_scheduler=False),
        base_url="http://127.0.0.1",
        headers={"X-TGFarm-Token": _token()},
    ) as test_client:
        assert test_client.get("/api/metrics/freshness").status_code == 200
    assert len(opened) == 2, (
        "/api/metrics/freshness должен строить отчёт один раз, "
        f"открыто сессий: {len(opened)}"
    )


def test_freshness_summary_of_equals_the_module_that_owns_the_semantics(monkeypatch):
    """The api-side counting must be `freshness.freshness_summary` on the same facts.

    The comparison is made race-free by construction rather than by hoping no
    collector writes in between: the report is read from the database *once*, the
    module's own `metrics_freshness` is then pinned to hand back that very object,
    and `freshness_summary` counts it — so the module and `_freshness_summary_of`
    are given the identical in-memory facts, not two snapshots of a moving table.
    A previous version of this test read the database on both sides and lost to a
    vk row whose status flipped under it mid-suite.

    If either side starts to mean something else by ``stale``, ``no_report`` or
    ``scheduled_but_silent``, this fails by name instead of the panel quietly
    showing a number the CLI does not produce.
    """
    from tgfarm import api
    from tgfarm.s8_analytics import freshness

    detail = freshness.metrics_freshness(now=NOW)
    monkeypatch.setattr(freshness, "metrics_freshness", lambda now=None: detail)
    reference = freshness.freshness_summary(now=NOW)
    derived = api._freshness_summary_of(detail)

    differing = {
        key: (derived[key], reference[key])
        for key in set(derived) | set(reference)
        if derived.get(key) != reference.get(key)
    }
    assert differing == {}, f"расчёт api разошёлся с freshness_summary: {differing}"
    assert set(derived) == set(reference), "ни один ключ summary не потерян"
    assert derived["platforms_total"] == len(detail["platforms"])
    assert {"ok", "stale", "never", "blocked", "collectors", "silent_collectors"} <= set(derived)


def _synthetic_report() -> dict:
    """A report carrying every status the module can emit, plus unmeasured fields.

    The live database only ever shows the statuses it happens to have, so a
    comparison against it cannot tell that `unreadable` and `no_report` mean the
    same thing to both counters. Here each collector status appears exactly once,
    one silent collector has `rows: None` and `runs_recorded: None` — absent, not
    zero — and no platform is missing from the vocabulary.
    """
    from tgfarm.s8_analytics.freshness import (
        COLLECTOR_MISSING_TABLE,
        COLLECTOR_NO_REPORT,
        COLLECTOR_OK,
        COLLECTOR_STALE,
        COLLECTOR_UNREADABLE,
        STATUS_BLOCKED,
        STATUS_NEVER,
        STATUS_OK,
        STATUS_STALE,
    )

    platforms = {
        "fresh": {"id": "fresh", "name": "Свежая", "status": STATUS_OK,
                  "last_snapshot_at": "2026-11-05T11:00:00+00:00", "age_hours": 1.0},
        "old": {"id": "old", "name": "Запоздалая", "status": STATUS_STALE,
                "last_snapshot_at": "2026-11-03T12:00:00+00:00", "age_hours": 48.0},
        "empty": {"id": "empty", "name": "Пустая", "status": STATUS_NEVER,
                  "last_snapshot_at": None, "age_hours": None},
        "shut": {"id": "shut", "name": "Заблокированная", "status": STATUS_BLOCKED,
                 "last_snapshot_at": None, "age_hours": None},
    }
    collector_by_status = {
        COLLECTOR_OK: ("job-ok", "snap_ok", 12, 0.5, 3),
        COLLECTOR_STALE: ("job-stale", "snap_stale", 4, 90.0, 2),
        COLLECTOR_NO_REPORT: ("job-silent", "snap_silent", None, None, None),
        COLLECTOR_MISSING_TABLE: ("job-table-gone", "snap_gone", None, None, 0),
        COLLECTOR_UNREADABLE: ("job-unreadable", "snap_locked", None, None, None),
    }
    collectors = []
    for index, (data_status, (job_id, table, rows, age, runs)) in enumerate(
        collector_by_status.items()
    ):
        collectors.append(
            {
                "id": f"collector-{index}",
                "platform": f"platform-{index}",
                "job_id": job_id,
                "snapshot_table": table,
                "scheduled": index != 1,
                "interval_hours": 6.0 if index != 1 else None,
                "data_status": data_status,
                "rows": rows,
                "last_collected_at": None if rows is None else "2026-11-05T10:00:00+00:00",
                "age_hours": age,
                "runs_recorded": runs,
                "last_run_at": None if not runs else "2026-11-05T10:00:00+00:00",
                "last_run_status": "ok" if runs else None,
                "scheduled_but_silent": index in (2, 3, 4),
                "reason": f"причина {index}",
            }
        )
    return {
        "generated_at": NOW.isoformat(),
        "stale_after_hours": 24.0,
        "snapshots_total": 20,
        "platforms": platforms,
        "collectors": collectors,
        "tables_without_collector": [{"table": "stray_snapshots", "rows": 7}],
        "collectors_error": None,
        "error": None,
    }


def test_freshness_summary_of_agrees_on_statuses_the_database_may_never_show(monkeypatch):
    """Whole-vocabulary parity: one synthetic report, both counters, no database.

    The live database only shows the statuses it happens to have; a silent collector
    whose `rows` and `runs_recorded` are absent must still count identically, because
    turning «нет отчёта» into `0` is exactly the defect the card exists to expose.
    """
    from tgfarm import api
    from tgfarm.s8_analytics import freshness

    report = _synthetic_report()
    monkeypatch.setattr(freshness, "metrics_freshness", lambda now=None: report)
    reference = freshness.freshness_summary(now=NOW)
    derived = api._freshness_summary_of(report)

    assert derived == reference, "на всём словаре статусы считаются одинаково"
    assert derived["collectors_no_report"] == 1
    assert derived["collectors_missing_table"] == 1
    assert derived["collectors_unreadable"] == 1
    assert len(derived["silent_collectors"]) == 3, (
        "три «по расписанию, данных нет» — и ни одной единицей больше"
    )
    silent = {row["id"]: row for row in derived["silent_collectors"]}
    assert silent["collector-2"]["rows"] is None, "отсутствие снимка не должно стать нулём"
    assert silent["collector-2"]["runs_recorded"] is None
    assert silent["collector-3"]["runs_recorded"] == 0, "пустой журнал — это ноль, а не отсутствие"


def test_endpoint_exposes_collectors_and_orphan_tables_at_the_top_level(client):
    """The panel reads ``payload.collectors``; a summary-only shape left it unreachable."""
    body = client.get("/api/metrics/freshness").json()
    assert isinstance(body["collectors"], list) and body["collectors"]
    for row in body["collectors"]:
        for key in COLLECTOR_KEYS:
            assert key in row, f"панели нужен ключ {key}"
    assert isinstance(body["tables_without_collector"], list)
    assert body["summary"]["collectors_total"] == len(body["collectors"])
    assert body["summary"]["collectors"] == body["collectors"], "collectors один раз, не дважды"
    # The platform half is untouched: the shape every older consumer already reads.
    assert body["summary"]["platforms_total"] == len(body["platforms"])
    assert body["stale_after_hours"] == body["summary"]["stale_after_hours"]
    assert body["snapshots_total"] == body["summary"]["snapshots_total"]


def test_silent_collector_is_named_as_scheduled_but_without_data(client):
    """«расписание есть — данных нет» has to survive the trip through the API.

    ``scheduled_but_silent`` is the only field that can say it and the one the old
    card ignored, so the endpoint keeping it is the contract the card depends on.
    """
    body = client.get("/api/metrics/freshness").json()
    silent = body["summary"]["silent_collectors"]
    assert silent, "на голой базе хоть один зарегистрированный сборщик немой"
    for row in silent:
        assert row["claim"], "немота обязана быть объяснена словами сервера"
        assert row["id"] in {item["id"] for item in body["collectors"]}
        assert row["rows"] is None or isinstance(row["rows"], int)


# ------------------------------------------------------------------ ui: text pins


def test_collector_block_prints_the_three_clauses_apart(collector_block, freshness_card):
    """Job id, the table it reads, rows, age against its threshold, the journal, the claim."""
    for needle in (
        "data.job_id",
        "data.snapshot_table",
        "rowsCell(data.rows)",
        "hrs(data.age_hours)",
        "thresholdCell(threshold)",
        "runsCell(data.runs_recorded)",
        "stampCell(data.last_run_at)",
        "esc(data.reason)",
        "everyCell(data.interval_hours)",
    ):
        assert needle in collector_block, f"сборочный блок обязан печатать {needle}"
    assert "payload.collectors" in freshness_card
    assert '#metrics-freshness-collectors' in freshness_card
    # The claim line is a separate row: schedule, data and attempt never merge.
    assert "freshness-claim" in collector_block


def test_tables_without_collector_is_printed(freshness_card):
    """A dated table no registered job fills is a finding, not an invisible row."""
    assert "payload.tables_without_collector" in freshness_card
    assert "#metrics-freshness-tables" in freshness_card
    html = (UI_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="metrics-freshness-collectors"' in html
    assert 'id="metrics-freshness-tables"' in html
    assert html.index('id="metrics-freshness"') < html.index('id="metrics-freshness-collectors"')
    assert html.index('id="metrics-freshness-collectors"') < html.index('id="metrics-freshness-tables"')


def test_scheduled_but_silent_state_is_neither_ok_nor_error(collector_block):
    """The colour carries the claim: a green or a red chip sends the operator out for
    the wrong thing, so the state needs a chip class of its own in the CSS too."""
    assert "COLLECTOR_SILENT_CHIP" in collector_block
    assert 'cls: "tag-silent"' in collector_block
    assert "по расписанию, данных нет" in collector_block
    assert "scheduled_but_silent" in collector_block
    css = (UI_DIR / "app.css").read_text(encoding="utf-8")
    assert ".tag-silent" in css
    rule = css[css.index(".tag-silent") : css.index(".item.freshness-collector.is-silent")]
    assert ".tag-ok" not in rule and ".tag-err" not in rule
    assert "is-silent" in collector_block


def test_collector_statuses_use_the_servers_vocabulary(collector_block):
    """`COLLECTOR_*` in freshness.py, by name, and no invented diagnosis for the rest."""
    for status in ("ok", "stale", "no_report", "missing_table", "unreadable"):
        assert f"{status}:" in collector_block, f"статус {status} не отображён"
    assert "String(data.data_status || NO_REPORT)" in collector_block


def test_no_fabricated_zero_in_the_collector_path(collector_block):
    """P34: `Number(x || 0)` prints a measurement that never happened.

    The gate is ``typeof v === "number"``: only a value that came from the payload
    may be printed as a number, everything else prints the file's no-report
    literal.
    """
    code = _code_only(collector_block)
    assert "Number(" not in code
    assert "|| 0" not in code
    assert "?? 0" not in code
    assert 'typeof v === "number"' in code
    assert '"нет отчёта"' in code
    # 0 and null are different facts and get different words.
    assert "v === 0" in code, "0 запусков и «замеров не было» — разные состояния"


def test_the_card_itself_stops_inventing_zeros(freshness_card):
    """`summary.ok || 0` and `Number(row.snapshots || 0)` are the same defect up top."""
    code = _code_only(freshness_card)
    assert "|| 0" not in code
    assert "Number(" not in code
    assert "cell(summary.ok)" in code
    assert "hasRows(row.snapshots)" in code
    assert "hrs(row.age_hours)" in code


# Every field below arrives from the database or from a collector module: rules
# and post text are untrusted data, never markup and never an instruction.
UNTRUSTED_FIELDS = (
    "data.job_id",
    "data.platform",
    "data.snapshot_table",
    "data.reason",
    "data.last_run_status",
    "row.name",
    "row.reason",
)

# Concatenation and interpolation are the two ways a value enters the HTML; a
# wrapper call in between (`+ esc(x.y)`) breaks the pattern, a bare `+ x.y` is it.
RAW_PRINT = re.compile(
    r"(?:\+\s*|\$\{\s*)(?:data|row|collector|table|summary|payload)\.[A-Za-z_]+"
)


def test_external_strings_cannot_reach_the_dom_unescaped(collector_block, freshness_card):
    for name, block in (("collector", collector_block), ("card", freshness_card)):
        offenders = RAW_PRINT.findall(block)
        assert offenders == [], f"{name}: внешнее значение печатается без обёртки: {offenders}"
    haystack = collector_block + freshness_card
    for field in UNTRUSTED_FIELDS:
        assert f"esc({field}" in haystack, f"{field} не проходит через esc"


def test_missing_payload_keys_are_no_report_not_an_empty_success(freshness_card, collector_block):
    """An older server or a dead collector read must not read as "no collectors exist".

    Three states, three sentences: the key is absent, the list is empty, the list
    has rows — and only the middle one is a measured fact about the registry.
    """
    assert "Array.isArray(items)" in collector_block
    assert "Сервер не вернул этот список" in collector_block
    assert "это не пустой список, это отсутствие отчёта" in collector_block
    assert "payload ? payload.collectors : undefined" in freshness_card
    assert "payload.tables_without_collector" in freshness_card


# ----------------------------------------------------------------- ui: real render

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
const body = [spec.anchors.helpers, spec.anchors.hours, spec.anchors.block, spec.anchors.card]
  .map(slice).join("\n");
const document = {
  createElement: () => ({ style: {}, classList: { add() {}, remove() {} } }),
  addEventListener: () => {},
  querySelector: () => null,
};
const render = new Function("document", "window", body + "\n; return renderMetricsFreshness;")(
  document,
  {},
);
const out = [];
for (const item of spec.cases) {
  const nodes = {};
  for (const id of item.ids) {
    nodes[id] = {
      innerHTML: "",
      textContent: "",
      insertAdjacentHTML(_pos, html) { this.innerHTML = html + this.innerHTML; },
    };
  }
  document.querySelector = (sel) => (sel in nodes ? nodes[sel] : null);
  render(item.payload);
  out.push({ name: item.name, nodes });
}
process.stdout.write(JSON.stringify(out));
"""


def _collector(**overrides):
    row = {
        "id": "telegram-post-metrics:telegram",
        "platform": "telegram",
        "module": "tg_sync",
        "entry": "tg_sync.collect_post_stats",
        "job_id": "telegram-post-metrics",
        "startup_job_id": "telegram-post-metrics-startup",
        "interval_hours": 6,
        "scheduled": True,
        "snapshot_table": "post_stats",
        "rows": None,
        "last_collected_at": None,
        "age_hours": None,
        "stale": False,
        "fresh": False,
        "data_status": "no_report",
        "reason": "нет отчёта: в таблице `post_stats` ни одного снимка",
        "runs_recorded": None,
        "last_run_at": None,
        "last_run_status": None,
        "scheduled_but_silent": True,
    }
    row.update(overrides)
    return row


def _render_cases(tmp_path: Path, cases: list[dict]) -> dict[str, dict[str, str]]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node недоступен — поведение панели не проверить")
    harness = tmp_path / "harness.cjs"
    harness.write_text(HARNESS, encoding="utf-8")
    spec = {
        "anchors": {
            "helpers": [HELPERS_START, HELPERS_END],
            "hours": [HOURS_START, HOURS_END],
            "block": [BLOCK_START, BLOCK_END],
            "card": [CARD_START, CARD_END],
        },
        "cases": [
            {
                "name": case["name"],
                "ids": [
                    "#metrics-freshness",
                    "#metrics-freshness-meta",
                    "#metrics-freshness-collectors",
                    "#metrics-freshness-tables",
                ],
                "payload": case["payload"],
            }
            for case in cases
        ],
    }
    spec_path = tmp_path / "cases.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
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
    return {item["name"]: item["nodes"] for item in json.loads(done.stdout)}


def test_rendered_card_reports_absent_values_and_a_silent_job(tmp_path):
    """The whole point of the rewrite, measured on rendered HTML rather than on source."""
    results = _render_cases(
        tmp_path,
        [
            {
                "name": "no_report",
                "payload": {"collectors": [_collector()], "stale_after_hours": 24.0},
            },
            {
                "name": "measured_zero",
                "payload": {
                    "collectors": [
                        _collector(rows=0, runs_recorded=0, data_status="no_report"),
                    ],
                    "stale_after_hours": 24.0,
                },
            },
            {"name": "older_server", "payload": {"platforms": {}, "summary": {}}},
        ],
    )

    silent = results["no_report"]["#metrics-freshness-collectors"]["innerHTML"]
    assert "telegram-post-metrics" in silent
    assert "post_stats" in silent
    assert "нет отчёта" in silent
    assert "0 снимков" not in silent, "null rows не имеет печататься нулём"
    assert "tag-silent" in silent and "по расписанию, данных нет" in silent
    assert "журнал запусков: нет отчёта" in silent, "неизвестное число запусков — не ноль"

    zeroed = results["measured_zero"]["#metrics-freshness-collectors"]["innerHTML"]
    assert "0 снимков" in zeroed, "измеренный ноль обязан остаться нулём"
    assert "ни одной записи о запуске" in zeroed
    assert zeroed != silent, "rows 0 и rows null — разные строки"

    older = results["older_server"]["#metrics-freshness-collectors"]["innerHTML"]
    assert "Сервер не вернул этот список" in older
    assert "нет отчёта" in results["older_server"]["#metrics-freshness-meta"]["textContent"]


def test_rendered_card_escapes_external_text(tmp_path):
    """A reason or a job id is data: markup in it must never reach the DOM as markup."""
    hostile = _collector(
        job_id='"><script>alert(1)</script>',
        snapshot_table="<img src=x onerror=alert(1)>",
        reason="снимков <b>7</b>, последний «не записан»",
        platform="'or 1=1--",
    )
    nodes = _render_cases(tmp_path, [{"name": "hostile", "payload": {"collectors": [hostile]}}])
    html = nodes["hostile"]["#metrics-freshness-collectors"]["innerHTML"]
    assert "<script>" not in html
    assert "<img" not in html
    assert "<b>7</b>" not in html
    assert "&lt;script&gt;" in html and "&lt;img" in html
    assert "&#39;or 1=1--" in html


def test_rendered_card_names_a_stale_age_against_its_threshold(tmp_path):
    """158 hours against a 24-hour threshold: the number and its gate on one line."""
    stale = _collector(
        job_id="metrics-and-learning",
        platform="dzen",
        rows=15,
        age_hours=158.01,
        data_status="stale",
        stale=True,
        scheduled_but_silent=True,
        runs_recorded=2413,
        last_run_at="2026-11-04T10:00:00+00:00",
        last_run_status="ok",
        reason="снимков 15, новый снимок не записан",
    )
    nodes = _render_cases(
        tmp_path,
        [
            {
                "name": "stale",
                "payload": {
                    "collectors": [stale],
                    "tables_without_collector": [
                        {
                            "id": "channel_stats",
                            "table": "channel_stats",
                            "snapshot_table": "channel_stats",
                            "scheduled": False,
                            "job_id": None,
                            "rows": 206,
                            "age_hours": 15.55,
                            "data_status": "ok",
                            "reason": "расписания для `channel_stats` нет",
                        },
                    ],
                    "stale_after_hours": 24.0,
                },
            },
        ],
    )
    html = nodes["stale"]["#metrics-freshness-collectors"]["innerHTML"]
    assert "15 снимков" in html
    assert "158 ч" in html, "hrs() обязана остаться в ходу"
    assert "порог 24 ч" in html
    assert "каждые 6 ч" in html
    assert "413 записей о запуске" in html
    assert "нет отчёта" not in html.split("журнал запусков")[1].split("</div>")[0]
    tables = nodes["stale"]["#metrics-freshness-tables"]["innerHTML"]
    assert "channel_stats" in tables and "206 снимков" in tables
