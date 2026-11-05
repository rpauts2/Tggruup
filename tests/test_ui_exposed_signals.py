"""UI contract regressions for the signals the pipeline already measures.

Four facts existed behind the API before this block was wired: the per-cluster
`trend_phase` / `acceleration` of S2, the append-only rules history of
CommunityPolicy, the richer summary of `/api/sources/health` and the per-format
report of `/api/metrics/formats`. The panel ignored all of them.

These tests read /ui/app.js the way a browser does and pin the two properties
that keep breaking in this product:

* an absent measurement must not be rendered as a zero (no fictitious readiness);
* every server- or page-sourced string must reach the DOM through `esc()`.
"""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    from tgfarm.api import create_app

    with TestClient(create_app(with_scheduler=False), base_url="http://127.0.0.1") as test_client:
        yield test_client


def _script(client) -> str:
    return client.get("/ui/app.js").text


def _chunk(script: str, start_marker: str, end_marker: str) -> str:
    start = script.index(start_marker)
    end = script.index(end_marker, start)
    return script[start:end]


# ----------------------------------------------------------------- trend shape
def test_trends_list_renders_phase_and_acceleration(client):
    script = _script(client)
    body = _chunk(script, "function renderTrends", "const STATUS_TAG")
    assert "trendShapeRow(t)" in body
    # The five codes S2 can store, each with a human label.
    phase_block = _chunk(script, "const TREND_PHASE_RU", "const trendPhaseChip")
    for phase in ("rising", "steady", "spike", "decaying", "unknown"):
        assert f"{phase}:" in phase_block


def test_missing_acceleration_is_never_a_zero(client):
    """`acceleration === null` means "no previous window", not "flat".

    S2 returns None for a missing *and* for an empty baseline; a `|| 0` here
    would turn both into "0 %", i.e. a measurement that was never taken.
    """
    script = _script(client)
    body = _chunk(script, "const trendAccelerationChip", "const trendShapeRow")
    assert "ускорение: нет данных" in body
    assert "typeof value === \"number\"" in body
    assert "Number.isFinite(value)" in body
    assert "|| 0" not in body
    assert "?? 0" not in body
    # A measured 0 still has to print as a number, not as "нет данных".
    assert "percent !== 0" in body


def test_unknown_phase_is_marked_unknown_not_renamed(client):
    script = _script(client)
    body = _chunk(script, "const trendPhaseChip", "const trendAccelerationChip")
    assert "фаза не измерена" in body
    # An unrecognised code travels verbatim instead of being folded into a
    # known phase the server never sent.
    assert "esc(label)" in body
    assert "TREND_PHASE_RU[key]" in body


def test_trends_legend_explains_absent_measurement(client):
    html = client.get("/").text
    assert "это не ноль" in html


# ----------------------------------------------------------- rules history
def test_policy_history_is_wired_to_the_real_endpoint(client):
    script = _script(client)
    assert '"/api/community-policy/history?"' in script
    body = _chunk(script, "async function loadPolicyHistory", "function renderSources")
    assert 'params.set("platform", platform)' in body
    assert 'params.set("target_id", targetId)' in body


def test_policy_history_marks_the_rules_change_from_the_stored_hash(client):
    """The endpoint has no `changed` flag; `rules_hash` per snapshot is the mark.

    History is newest-first, so a row changed the rules when its hash differs
    from the *older* neighbour of the same destination. The oldest row in the
    slice must not claim either verdict — nothing precedes it in the answer.
    """
    script = _script(client)
    body = _chunk(script, "function renderPolicyHistory", "async function loadPolicyHistory")
    assert "rules_hash" in body
    assert "правила изменились" in body
    assert "older.target_id" in body
    assert "раньше этой записи история не спрашивалась" in body


def test_policy_history_degrades_to_one_clear_line(client):
    script = _script(client)
    body = _chunk(script, "function renderPolicyHistory", "async function loadPolicyHistory")
    # [] is as unavailable as a 500: the operator gets no empty box either way.
    assert "История недоступна: по этому месту" in body
    load = _chunk(script, "async function loadPolicyHistory", "function renderSources")
    assert "История недоступна: " in load
    # An unavailable answer must not be cached as loaded, so the next click
    # retries the request instead of hiding the message.
    assert 'if (items.length) root.dataset.loaded = "1";' in load
    assert load.count('delete root.dataset.loaded') >= 2


def test_policy_history_button_passes_no_untrusted_text(client):
    script = _script(client)
    # The handler is built inside a template literal: only the validated post id
    # and platform id may be interpolated, the destination id comes back from
    # state and never enters an inline JS literal.
    assert "tgfarmPolicyHistory('${esc(id)}','${esc(platform)}')" in script
    assert 'id="policy-history-${esc(id)}-${esc(platform)}"' in script
    body = _chunk(script, "window.tgfarmPolicyHistory", "window.tgfarmBuildEditorialPreview")
    assert "state.communityTargetPlans" in body
    assert "selected.slug || selected.path || selected.name" in body


def test_server_text_reaches_the_dom_escaped(client):
    """Rules excerpts and requirement lines are page content, not markup."""
    script = _script(client)
    for marker in (
        "function renderPolicyHistory",
        "const trendPhaseChip",
        "function renderMetricsFormats",
        "async function loadSourcesHealth",
        "const routerSkipRow",
        "function renderRouterSkips",
        "function renderReprintBackfill",
    ):
        body = script[script.index(marker):script.index(marker) + 3000]
        for leak in ('">${row.', '">${item.', "+ row.", "+ item."):
            assert leak not in body, f"{marker} interpolates raw {leak!r}"
    history = _chunk(script, "function renderPolicyHistory", "async function loadPolicyHistory")
    for field in ("esc(row.target_name", "esc(row.platform", "esc(row.target_type", "esc(hash"):
        assert field in history
    assert "<li>${esc(line)}</li>" in history


# ------------------------------------------------------------- sources health
def test_sources_health_block_uses_the_richer_summary(client):
    script = _script(client)
    body = _chunk(script, "async function loadSourcesHealth", "async function explainTrend")
    for field in (
        "summary.total",
        "counts.disabled",
        "summary.attention",
        "summary.stale",
        "summary.stale_hours",
        "summary.never_checked",
        "summary.generated_at",
        "row.cooldown_until",
        "row.last_attempt_at",
    ):
        assert field in body


def test_sources_health_admits_a_truncated_problem_list(client):
    """`top_problems` is capped by the server; saying so is not optional."""
    script = _script(client)
    body = _chunk(script, "async function loadSourcesHealth", "async function explainTrend")
    assert "summary.top_problems || []" in body
    assert "Показаны не все" in body
    assert "attention - problems.length" in body
    # A calm fleet is reported as calm, not as an empty card.
    assert "ни один источник не в карантине" in body


def test_sources_health_still_reuses_the_existing_button_contract(client):
    script = _script(client)
    body = _chunk(script, "async function loadSourcesHealth", "async function explainTrend")
    assert 'data-source-reset="' in body
    assert 'id="sources-health"' in client.get("/").text


# ------------------------------------------------------------------- formats
def test_formats_report_is_wired_into_the_dashboard_refresh(client):
    script = _script(client)
    body = _chunk(script, "async function refresh()", "async function runCycle()")
    assert 'metricsFormats: "/api/metrics/formats?days=30"' in body
    assert 'if ("metricsFormats" in data) renderMetricsFormats(data.metricsFormats);' in body
    # Still one settled pass, so a dead endpoint cannot kill the dashboard.
    assert "Promise.allSettled(" in body
    assert 'id="metrics-formats"' in client.get("/").text


def test_unreliable_format_row_is_not_a_score(client):
    """`avg_engagement` is `null` when nothing was measured: an absence, not a zero.

    S8 used to send `0.0` for an unmeasured format and the panel printed it as a
    score; P81 made the absence cross the wire, so the panel must read it as an
    absence too. `Number(null || 0).toFixed(1)` prints exactly the `0.0` the report
    stopped lying about, so the line that coerced it is pinned out of the file.
    """
    script = _script(client)
    body = _chunk(script, "function renderMetricsFormats", "function renderReport")
    assert "row.reliable === true" in body
    assert "внимание: нет данных" in body
    assert "мало данных" in body
    assert "row.measured || 0" in body
    assert 'class="tag tag-acc">внимание' in body
    assert 'typeof row.avg_engagement === "number"' in body
    for coerced in ("row.avg_engagement || 0", "Number(row.avg_engagement"):
        assert coerced not in body, f"{coerced} re-fabricates the zero S8 refuses to send"


# ---------------------------------------------------------------- router skips
def test_skip_report_is_wired_to_the_real_endpoint(client):
    script = _script(client)
    assert 'api("/api/router/skips/" + encodeURIComponent(clusterId))' in script
    body = _chunk(script, "async function explainSkips", "async function resetSourceCooldown")
    assert 'data-trend-skips-body="' in body
    assert "CSS.escape(clusterId)" in body
    # A failed answer must not be cached as loaded, so the next click retries.
    assert 'target.dataset.loaded = "1";' in body
    assert body.index('target.dataset.loaded = "1";') < body.index("catch (error)")


def test_trend_rows_carry_the_skips_button_and_body(client):
    script = _script(client)
    body = _chunk(script, "function renderTrends", "const STATUS_TAG")
    assert 'data-trend-skips="' in body
    assert 'data-trend-skips-body="' in body
    assert "Пропуски?" in body
    assert "trend-skips" in body


def test_absent_skip_record_is_not_rendered_as_a_clean_plan(client):
    """`remembered: false` means "this process has no record", not "nothing dropped".

    S3's registry is process memory: a restart or an LRU eviction leaves no row
    at all. Folding that into "all destinations planned" would tell the operator
    the plan was complete when it was simply never measured here.
    """
    script = _script(client)
    body = _chunk(script, "function renderRouterSkips", "async function explainSkips")
    assert "data.remembered !== true" in body
    assert "нет данных" in body
    # Only the remembered branch may claim a clean plan.
    assert "Ни одна площадка не пропущена" in body
    assert "state_note" in body


def test_skip_report_admits_its_memory_is_bounded(client):
    script = _script(client)
    body = _chunk(script, "function routerSkipsMemoryNote", "function renderRouterSkips")
    assert "registry_limit" in body
    assert "Number.isFinite(limit)" in body
    assert "после перезапуска список пуст" in body
    # A missing limit is not shown as "лимит undefined".
    assert 'const cap = Number.isFinite(limit)' in body


def test_unknown_skip_capability_is_printed_verbatim(client):
    """An unrecognised reason code keeps its own colourless chip, never a guess."""
    script = _script(client)
    body = _chunk(script, "const routerSkipRow", "function routerSkipsMemoryNote")
    for field in ("esc(item.channel_id", "esc(item.platform", "esc(item.reason"):
        assert field in body
    assert "capability-${esc(item.capability)}" in body
    assert "esc(item.capability_label || item.capability" in body
    assert "причина не записана" in body
    assert "нет данных" in body


def test_skipped_reason_never_reaches_the_dom_as_markup(client):
    script = _script(client)
    body = _chunk(script, "const routerSkipRow", "async function explainSkips")
    for raw in ("${item.", "${row.", "${data.", "+ item.", "+ data."):
        assert raw not in body, f"raw interpolation of {raw!r}"


def test_skips_block_reuses_the_existing_explanation_control(client):
    """No new widget: the report is the «Почему?» chip family with its own layout."""
    script = _script(client)
    css = client.get("/ui/app.css").text
    body = _chunk(script, "const routerSkipRow", "function routerSkipsMemoryNote")
    assert "trend-reason" in body
    assert "platform-capability" in body
    assert ".trend-explain" in css
    assert ".trend-skips" in css
    assert ".trend-skip-row" in css


def test_trends_legend_explains_the_absent_skip_record(client):
    html = client.get("/").text
    assert "Пропуски?" in html
    assert "не означает «ничего не пропущено»" in html


# ------------------------------------------------------------- reprint backfill
def test_backfill_report_is_wired_to_the_real_endpoint(client):
    script = _script(client)
    loader = _chunk(script, "async function loadReprintBackfill", "async function explainTrend")
    assert 'api("/api/sources/reprint-backfill")' in loader
    assert '$("#reprint-backfill")' in loader
    # A failed answer must not silently keep a stale report on screen.
    assert "Статус бэкфилла репринтов недоступен: " in loader
    assert 'id="reprint-backfill"' in client.get("/").text


def test_backfill_block_reloads_when_the_sources_view_opens(client):
    script = _script(client)
    body = _chunk(script, "function switchView(view)", "window.tgfarmGoto")
    assert 'if (view === "sources") loadReprintBackfill();' in body


def test_never_run_backfill_is_not_rendered_as_finished(client):
    """`state: never_run` means this process has no report, not a clean corpus.

    Only the server's `finished` verdict may show the green chip; every other
    answer — including a missing body — falls to the «нет данных» chip, so an
    operator never reads "nothing remains" out of "nobody looked".
    """
    script = _script(client)
    body = _chunk(script, "function renderReprintBackfill", "async function loadReprintBackfill")
    assert 'd.state === "finished"' in body
    assert 'd.state === "remainder"' in body
    assert 'class="tag tag-none">нет данных' in body
    assert "отчёт не записан в этом процессе" in body
    # The green word belongs to the measured-finished branch alone.
    assert body.index('d.state === "finished"') < body.index("завершён")


def test_absent_backfill_numbers_are_never_rendered_as_zeros(client):
    """rows_scanned null is "no data"; a scanned 0 is a measurement."""
    script = _script(client)
    body = _chunk(script, "const backfillFact", "function renderReprintBackfill")
    assert 'typeof value === "number"' in body
    assert "Number.isFinite(value)" in body
    assert '"нет данных"' in body
    assert "|| 0" not in body
    assert "?? 0" not in body


def test_backfill_counters_stay_four_different_facts(client):
    """Scanned, remaining candidates, unfingerprinted and undated are not merged."""
    script = _script(client)
    body = _chunk(script, "function renderReprintBackfill", "async function loadReprintBackfill")
    for label in (
        "просканировано строк",
        "осталось кандидатов",
        "без отпечатка",
        "без даты",
    ):
        assert label in body
    for field in ("d.rows_scanned", "d.candidates_remaining", "d.unfingerprinted_rows", "d.undated_rows"):
        assert field in body


def test_backfill_block_admits_its_memory(client):
    script = _script(client)
    body = _chunk(script, "function renderReprintBackfill", "async function loadReprintBackfill")
    assert "esc(d.storage" in body
    assert "памяти процесса" in body
    assert "после перезапуска" in body


def test_backfill_state_note_reaches_the_dom_escaped(client):
    script = _script(client)
    body = _chunk(script, "function renderReprintBackfill", "async function loadReprintBackfill")
    assert "esc(d.state_note" in body
    assert "esc(d.stopped_by)" in body
    for raw in ("${d.", "+ d. ", "${data."):
        assert raw not in body, f"raw interpolation of {raw!r}"


# ---------------------------------------------------------------------- style
def test_absent_measurements_share_one_dedicated_style(client):
    """`tag-none` is the visual "not measured"; it must look unlike a verdict."""
    css = client.get("/ui/app.css").text
    script = _script(client)
    assert "tag-none" in script
    assert ".tag-none" in css
    assert "border-style: dashed" in css
    assert ".trend-shape" in css
    assert ".policy-history-row.is-changed" in css


# ----------------------------------------------------------- journal counters
def test_the_counter_helper_distinguishes_unknown_from_measured_nothing(client):
    """`/api/runs` serves `int | null`; the panel must not fold them together.

    `n()` ends in `|| 0`, so handing it a null would print "0 шт" for a stage
    nobody measured — the fabricated zero the writer contract exists to remove,
    re-invented at the render layer. The guard has to be a type test.
    """
    script = _script(client)
    body = _chunk(script, "const items = (v, state)", "const esc =")
    assert 'typeof v === "number"' in body
    assert "нет отчёта" in body
    assert "до контракта" in body
    assert "Number(v) || 0" not in body


def test_no_run_view_prints_a_raw_counter(client):
    """Both readers of `in`/`out` branch first; neither interpolates the value."""
    script = _script(client)
    assert "${s.out}" not in script
    assert "${s.in}" not in script
    detail = _chunk(script, "const detail = s ?", "return `<div class=\"stage")
    assert "items(s.out, s.counters_state)" in detail
    runs_line = _chunk(script, '<div class="item-meta">${stages.map', "</div>")
    assert 'typeof s.out === "number"' in runs_line
