"""The panel must stop calling dead evidence a measurement, and a stop a crash.

Three product commits each produced a number the operator could not see:

* ``1b9309f`` split every format measurement into live and dead snapshots
  (``measurement``, ``fresh_measured_total``, ``stale_after_days``,
  ``evidence_status``) inside ``format_performance``, which
  ``GET /api/metrics/formats`` returns verbatim. ``measured: 9`` and
  ``measured: 9 but every snapshot is six days old`` are different facts, and
  ``planner_history`` hands the planner only the live counts — so the panel was
  advertising a readiness the farm's own planner had already refused.
* ``f235908`` and ``f0c317a`` produced the attribution half of the audit
  (``askable_posts``, ``crosspost_with_remote``, and Dzen's
  ``mapped_publications_with_id`` on ``/api/report``): a publication the journal
  cannot name can never be matched to its metrics even by a working collector.
* ``f038373`` added the journalled ``cancelled`` outcome.

Like the rest of the UI suite these tests read ``/ui/app.js`` the way a browser
does and drive the real endpoints, so a rename on either side of the HTTP
boundary breaks a test instead of rendering ``undefined``:

* the field names are scraped out of the renderer source, not restated here;
* an absent measurement renders as «нет отчёта», never as a zero;
* an unrecognised server code travels verbatim on a colourless chip;
* a state with no live evidence cannot earn the green "enough data" chip;
* every server-supplied string reaches the DOM through ``esc()``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from tgfarm.db.models import Channel, ChannelStat, CrosspostLog, Media, Post, PostStat
from tgfarm.db.session import session_scope
from tgfarm.s8_analytics.formats import (
    EVIDENCE_MIXED,
    EVIDENCE_NEVER,
    EVIDENCE_OK,
    EVIDENCE_STALE,
    EVIDENCE_UNTRUSTED,
)

ROOT = Path(__file__).resolve().parent.parent

#: Children first for the wipe, parents first for the restore: the session schema
#: turns ``PRAGMA foreign_keys=ON``. Same tuple the coverage panel suite uses, so
#: the shared database is left exactly as it was found.
_ANALYTICS_TABLES = (
    PostStat.__table__,
    Media.__table__,
    ChannelStat.__table__,
    CrosspostLog.__table__,
    Post.__table__,
    Channel.__table__,
)


@pytest.fixture(autouse=True)
def _isolated_analytics_tables():
    with session_scope() as session:
        snapshot = {
            table.name: [dict(row) for row in session.execute(select(table)).mappings().all()]
            for table in _ANALYTICS_TABLES
        }
        for table in _ANALYTICS_TABLES:
            session.execute(delete(table))
    yield
    with session_scope() as session:
        for table in _ANALYTICS_TABLES:
            session.execute(delete(table))
        for table in reversed(_ANALYTICS_TABLES):
            rows = snapshot.get(table.name) or []
            if rows:
                session.execute(table.insert(), rows)


@pytest.fixture()
def client():
    from tgfarm.api import create_app
    from tgfarm.core.apiauth import ensure_token

    # /ui/ is a public prefix, /api/ is not: one header covers both reads.
    with TestClient(
        create_app(with_scheduler=False),
        base_url="http://127.0.0.1",
        headers={"X-TGFarm-Token": ensure_token()},
    ) as test_client:
        yield test_client


def _script(client) -> str:
    return client.get("/ui/app.js").text


def _css(client) -> str:
    return client.get("/ui/app.css").text


def _slice(script: str, start_marker: str, end_marker: str) -> str:
    start = script.index(start_marker)
    return script[start : script.index(end_marker, start)]


def _formats_body(script: str) -> str:
    # Ends at the *next* renderer in the file, not two below it: when the queue
    # card was inserted between these two, a slice that ran to
    # `renderDzenAttribution` scraped that card's `data.totals` too and the shape
    # lock accused `/api/metrics/formats` of omitting a field it never claimed.
    return _slice(
        script,
        "function renderMetricsFormats(payload)",
        "function renderBacklog(payload, failure)",
    )


def _evidence_table(script: str) -> str:
    return _slice(script, "const EVIDENCE_STATUS = {", "const EVIDENCE_UNTRUSTED")


def _coverage_body(script: str) -> str:
    return _slice(script, "function renderMetricsCoverage(payload)", "const PUBLICATION_REASON")


def _gap_table(script: str) -> str:
    return _slice(script, "const COVERAGE_GAP = {", "const COVERAGE_STATUS")


def _dzen_body(script: str) -> str:
    return _slice(script, "function renderDzenAttribution(", "function renderReport(")


def _stage_table(script: str) -> str:
    return _slice(script, "const STAGE_STATUS = {", "function renderPipeline(")


def _pipeline_body(script: str) -> str:
    return _slice(script, "function renderPipeline(runs)", "const TREND_PHASE_RU")


def _run_chip(script: str) -> str:
    return _slice(script, "const runOutcomeChip = (stages)", "function renderRuns(runs)")


def _seed_platform(
    platform: str,
    *,
    fmt: str = "news",
    published: int = 0,
    fresh_snapshots: int = 0,
    stale_snapshots: int = 0,
    with_remote_id: int = 0,
    with_tg_ids: int = 0,
) -> None:
    """Publish ``published`` posts, some of them with a live or a dead snapshot.

    The split is the whole subject: a snapshot inside
    ``FORMAT_EVIDENCE_STALE_AFTER_DAYS`` is evidence, one outside it is history.
    Conftest pins that knob to the code default, so "yesterday" is alive and
    "40 days ago" is dead under any operator's ``.env``.
    """
    suffix = uuid4().hex[:8]
    channel_id = f"evidui_{platform}_{suffix}"
    now = datetime.now(UTC)
    with session_scope() as session:
        channel = Channel(id=channel_id, title=f"{platform} evidui", niche="news", platform=platform)
        if with_tg_ids:
            channel.tg_chat_id = 10_000_000_1
        session.add(channel)
        session.flush()
        for index in range(published):
            post = Post(
                public_id=f"evidui{platform}{suffix}{index}",
                channel_id=channel_id,
                status="published",
                published_at=now - timedelta(days=2),
                post_format=fmt,
                hook=f"Заголовок публикации {index} для {platform} достаточной длины",
                body="Текст",
            )
            if index < with_tg_ids:
                post.tg_message_id = 4000 + index
            session.add(post)
            session.flush()
            if index < fresh_snapshots:
                session.add(
                    PostStat(
                        post_id=post.id, reactions=4, comments=1, forwards=1,
                        collected_at=now - timedelta(days=1),
                    )
                )
            elif index < fresh_snapshots + stale_snapshots:
                session.add(
                    PostStat(
                        post_id=post.id, reactions=4, comments=1, forwards=1,
                        collected_at=now - timedelta(days=40),
                    )
                )
            if index < with_remote_id:
                session.add(
                    CrosspostLog(
                        post_public_id=post.public_id,
                        platform=platform,
                        status="posted",
                        remote_id=f"remote-{index}",
                        url=f"https://example.com/remote-{index}",
                        created_at=now,
                    )
                )


# ------------------------------------------------------------------- the premise
def test_the_formats_endpoint_passes_the_measurement_block_through(client):
    """No API change was needed: the route returns ``format_performance`` verbatim.

    Pinned from the off-limits source rather than restated, so a route that ever
    starts reshaping the report fails here instead of blanking the new card.
    """
    source = (ROOT / "tgfarm" / "api.py").read_text(encoding="utf-8")
    assert "return await io(format_performance, days)" in source
    _seed_platform("telegram", fmt="news", published=4, fresh_snapshots=2, stale_snapshots=2)
    payload = client.get("/api/metrics/formats?days=30").json()
    assert {"measurement", "fresh_measured_total", "stale_measured_total",
            "evidence_status", "stale_after_days"} <= set(payload)
    row = payload["measurement"]["news"]
    assert {"format", "measured", "published", "fresh", "stale", "fresh_avg_engagement",
            "fresh_reliable", "evidence_status", "last_measured_at",
            "fresh_last_measured_at"} <= set(row)


def test_the_same_measured_count_is_two_different_facts(client):
    """The product point, proved against the real module rather than the markup.

    Two formats with six measurements each are not the same evidence, and the
    panel is the place where an operator would otherwise never learn it.
    """
    _seed_platform("telegram", fmt="news", published=6, fresh_snapshots=6)
    _seed_platform("dzen", fmt="stat", published=6, stale_snapshots=6)
    payload = client.get("/api/metrics/formats?days=30").json()
    live, dead = payload["measurement"]["news"], payload["measurement"]["stat"]
    assert live["measured"] == dead["measured"] == 6
    assert live["evidence_status"] == EVIDENCE_OK
    assert dead["evidence_status"] == EVIDENCE_STALE
    assert (live["fresh"], live["stale"]) == (6, 0)
    assert (dead["fresh"], dead["stale"]) == (0, 6)
    assert dead["fresh_last_measured_at"] is None
    assert payload["stale_after_days"] == formats_stale_default()
    # The planner refuses the second one, which is why the panel must not claim it.
    assert dead["fresh_reliable"] is False and live["fresh_reliable"] is True


def formats_stale_default() -> int:
    """The threshold the code declares, read from the module, never restated."""
    from tgfarm.s8_analytics.formats import DEFAULT_EVIDENCE_STALE_AFTER_DAYS

    return DEFAULT_EVIDENCE_STALE_AFTER_DAYS


def test_the_panel_reads_exactly_the_fields_the_endpoint_returns(client):
    """Shape lock, scraped from the renderer: a rename on either side breaks here.

    ``error`` is the one field the card guards that the endpoint does not send
    today — ``format_performance`` has no error field and a dead read throws, so
    the branch is the shared card contract, not a live path.
    """
    _seed_platform("telegram", fmt="news", published=6, fresh_snapshots=3, stale_snapshots=3)
    _seed_platform("dzen", fmt="stat", published=4, stale_snapshots=4, with_remote_id=2)
    body = _formats_body(_script(client))
    payload = client.get("/api/metrics/formats?days=30").json()

    top_level = set(re.findall(r"\bdata\.([a-z_][a-z_]*)", body))
    row_keys = set(re.findall(r"\brow\.([a-z_][a-z_]*)", body))
    evidence_keys = set(re.findall(r"\bevidence\.([a-z_][a-z_]*)", body))

    # A rewrite to bracket access would empty every set and make this vacuous.
    assert {"measurement", "stale_after_days", "evidence_status",
            "fresh_measured_total", "stale_measured_total", "min_sample"} <= top_level
    assert {"format", "measured", "published", "avg_engagement", "reliable"} <= row_keys
    assert {"fresh", "stale", "evidence_status", "fresh_reliable", "fresh_avg_engagement",
            "last_measured_at", "fresh_last_measured_at"} <= evidence_keys

    for key in top_level - {"error"}:
        assert key in payload, f"panel reads {key!r} at the top level; endpoint omits it"
    assert payload["formats"], "seeded formats vanished, row keys went untested"
    for name, row in payload["formats"].items():
        for key in row_keys:
            assert key in row, f"panel reads {key!r} per format; {name} omits it"
    for name, row in payload["measurement"].items():
        for key in evidence_keys:
            assert key in row, f"panel reads {key!r} per evidence row; {name} omits it"


def test_the_dzen_attribution_fields_exist_on_the_endpoint_the_panel_uses(client):
    """``mapped_publications_with_id`` is served by ``/api/report``, not coverage."""
    _seed_platform("dzen", fmt="news", published=3, with_remote_id=2)
    body = _dzen_body(_script(client))
    dz = client.get("/api/report?days=14").json()["dzen"]
    read = set(re.findall(r"\bdz\.([a-z_][a-z_]*)", body))
    assert {"mapped_publications", "mapped_publications_with_id",
            "published_posts", "measured_posts"} <= read, read
    for key in read:
        assert key in dz, f"panel reads {key!r} from the report's dzen block; it is not there"
    assert dz["mapped_publications"] == 2
    assert dz["mapped_publications_with_id"] == 2


def test_the_cancelled_status_the_panel_colors_is_a_status_the_pipeline_writes():
    source = (ROOT / "tgfarm" / "pipeline.py").read_text(encoding="utf-8")
    assert '"cancelled",' in source
    assert '"cancelled": True' in source


# ------------------------------------------------------- live versus dead counts
def test_the_card_states_the_threshold_and_the_newest_measurement(client):
    """"Measured" is only weighable next to "how old is the newest number"."""
    assert 'id="metrics-formats-meta"' in client.get("/").text
    body = _formats_body(_script(client))
    for fragment in ("data.stale_after_days", "data.fresh_measured_total",
                     "data.stale_measured_total", "самый новый замер", "порог живости"):
        assert fragment in body, fragment
    # The report carries no top-level timestamp, so the header's stamp is the max
    # over the per-format ones — a derived number, and labelled as one.
    assert "latestMeasuredAt(" in body
    assert "last_measured_at" in body


def test_fresh_and_stale_counts_travel_on_items_never_on_n(client):
    """``n()`` ends in ``|| 0``: a missing counter must not come back as a zero.

    ``items()`` is the only honest reader of a counter that can be absent — here
    because the panel is talking to a backend older than the ``measurement``
    block, which is a real state and not "measured: nothing".
    """
    body = _formats_body(_script(client))
    assert "items(data.fresh_measured_total)" in body
    assert "items(data.stale_measured_total)" in body
    assert "items(fresh)" in body
    assert "items(stale)" in body
    for fabricated in ("n(data.fresh", "n(data.stale", "n(fresh)", "n(stale)",
                       "fresh || 0", "stale || 0", "fresh ?? 0", "stale ?? 0"):
        assert fabricated not in body, f"{fabricated!r} re-invents the zero"


def test_missing_measurement_block_admits_it_does_not_know(client):
    """An older backend must not be rendered as "every snapshot is alive"."""
    body = _formats_body(_script(client))
    assert "data.measurement" in body
    assert "отчёт о свежести не пришёл" in body
    assert "свежесть не измерена" in body
    # A non-object block collapses to null: without it there is no state to name.
    assert "? data.measurement : null" in body


def test_the_evidence_table_covers_the_formats_module_vocabulary(client):
    """Every ``EVIDENCE_*`` constant S8 can emit has an agreed colour and word."""
    script = _script(client)
    table = _evidence_table(script)
    css = _css(client)
    for status in (EVIDENCE_OK, EVIDENCE_MIXED, EVIDENCE_STALE, EVIDENCE_NEVER, "blocked"):
        assert re.search(rf"{status}:\s*\{{", table), f"{status} has no entry in the panel table"
    for cls in set(re.findall(r'cls:\s*"([^"]+)"', table)):
        assert f".{cls}" in css, f"{cls} is not defined in app.css"

    # The four states the report actually produces are distinguishable by word.
    labels = {}
    for status in (EVIDENCE_OK, EVIDENCE_MIXED, EVIDENCE_STALE, EVIDENCE_NEVER):
        match = re.search(rf'{status}:\s*\{{\s*cls:\s*"[^"]+",\s*label:\s*"([^"]+)"', table)
        assert match, status
        labels[status] = match.group(1)
    assert len(set(labels.values())) == 4, labels
    # `ok` alone is green: nothing that is not fully live evidence may wear it.
    green = re.findall(r'cls:\s*"tag-ok"[^}]*label:\s*"([^"]+)"', table)
    assert green == [labels[EVIDENCE_OK]], green


def test_the_untrusted_list_is_the_servers(client):
    """The panel's "no live evidence" set is ``formats.EVIDENCE_UNTRUSTED``."""
    assert EVIDENCE_UNTRUSTED == frozenset({"stale", "never", "blocked"})
    listing = _script(client)
    declared = re.search(r"EVIDENCE_UNTRUSTED = \[(.*?)\]", listing, re.S).group(1)
    assert set(re.findall(r'"([a-z_]+)"', declared)) == set(EVIDENCE_UNTRUSTED)


def test_dead_evidence_cannot_borrow_the_enough_data_chip(client):
    """The one rule this card exists for: no green for a format nothing live backs.

    ``planner_history`` gates its sample on the LIVE count, so a format measured
    only by dead snapshots reaches the planner as ``measured: 0``. A panel calling
    that "данных достаточно" would claim a readiness the farm refused.
    """
    body = _formats_body(_script(client))
    assert "noLiveEvidence" in body
    # The verdict keys on the counts AND on a declared untrusted state, never on
    # the known-code list alone: a code invented tomorrow must fail safe.
    assert "fresh === 0 || untrusted" in body
    assert "evidence.fresh_reliable === true" in body
    assert "живых данных достаточно" in body
    assert "замеров довольно, но они мертвы: план их отвергает" in body
    assert "мало живых данных" in body
    # Readiness is decided after the no-live test, so it cannot be reached by a
    # format with zero live snapshots.
    assert body.index("const noLiveEvidence") < body.index("const readiness")
    assert body.index("noLiveEvidence") < body.index("evidence.fresh_reliable === true")
    # A dead-evidence row is marked outside the chips too, so the warning does not
    # rest on a colour alone.
    assert "is-dead-evidence" in body
    assert ".item.is-dead-evidence" in _css(client)


def test_the_sample_threshold_is_never_assumed(client):
    """``min_sample`` decides a claim of "enough", so an absent one decides nothing."""
    body = _formats_body(_script(client))
    assert "Number.isInteger(data.min_sample) && data.min_sample > 0" in body
    assert "нет отчёта" in body
    assert "measured >= data.min_sample" in body
    # The blended figure stays readable only while the report says nothing about
    # freshness; once it does, the live-only average is the one on the chip.
    assert "evidence.fresh_avg_engagement" in body
    assert "внимание по живым" in body
    assert "внимание: только мёртвые замеры" in body


def test_unrecognised_evidence_status_is_printed_verbatim_and_stays_uncoloured(client):
    body = _formats_body(_script(client))
    assert "EVIDENCE_STATUS[statusKey]" in body
    assert 'known ? known.cls : "tag-none"' in body
    assert 'esc(known ? known.label : (statusKey || "статус не записан"))' in body
    # And the same rule for the report-level verdict in the header.
    assert "EVIDENCE_STATUS[overallKey]" in body


def test_formats_card_checks_the_error_field_before_any_totals(client):
    body = _formats_body(_script(client))
    assert "esc(data.error)" in body
    assert body.index("data.error") < body.index("data.published_total"), (
        "totals are rendered before the error branch returns"
    )
    assert body.index("data.error") < body.index("const rows = Object.values")
    assert "return;" in body[: body.index("const measurement")]


def test_formats_text_reaches_the_dom_escaped(client):
    script = _script(client)
    body = _formats_body(script)
    for field in (
        "esc(data.error)",
        "esc(evidenceMeta)",
        "esc(FORMAT_RU[row.format]",
        "esc(known ? known.label",
        "esc(typeof evidence.fresh_avg_engagement",
    ):
        assert field in body, field
    dzen = _dzen_body(script)
    assert "esc(String(dz.last_reason))" in dzen
    # No server-supplied value is interpolated raw into the markup.
    for raw in ("${row.format}", "${evidence.", "${statusKey}", "${known.label}",
                "${evidenceMeta}", "${fresh}", "${stale}"):
        assert raw not in body, f"raw interpolation of {raw!r}"


def test_the_new_cards_are_dispatched_through_the_shared_refresh(client):
    body = _slice(_script(client), "async function refresh()", "async function runCycle()")
    assert 'metricsFormats: "/api/metrics/formats?days=30"' in body
    assert 'if ("metricsFormats" in data) renderMetricsFormats(data.metricsFormats);' in body
    assert 'if ("metricsCoverage" in data) renderMetricsCoverage(data.metricsCoverage);' in body
    assert "Promise.allSettled(" in body
    # The Dzen attribution is re-rendered on every pass, before the all-failed
    # throw, so a fully dead backend says so instead of freezing on last pass's
    # attribution — the rule the publication card already follows.
    assert "renderDzenAttribution(" in body
    assert body.index("renderDzenAttribution(") < body.index('throw new Error("API недоступен")')


# ------------------------------------------------------------------ attribution
def test_coverage_row_separates_named_publications_from_measured_ones(client):
    """"Has an id at all" and "has a snapshot" are two different gaps.

    A publication the journal cannot name can never be matched to its metrics
    even by a collector that works, so ``measured: 0`` there is our bookkeeping —
    a different sentence from a platform nobody reads.
    """
    _seed_platform("dzen", fmt="news", published=4, fresh_snapshots=1, with_remote_id=1)
    _seed_platform("telegram", fmt="stat", published=5, fresh_snapshots=3, with_tg_ids=2)
    body = _coverage_body(_script(client))
    for fragment in ("row.crosspost_with_remote", "row.crosspost_posted", "row.askable_posts"):
        assert fragment in body, fragment
    payload = client.get("/api/metrics/coverage").json()
    for name, row in payload["platforms"].items():
        for key in ("crosspost_with_remote", "crosspost_posted", "askable_posts"):
            assert key in row, f"{name} row omits {key!r} that the panel reads"
    # `askable_posts` is `int | None` by design — no column describes it on the
    # other platforms — so it must ride on the type-testing printer.
    assert "items(row.askable_posts)" in body
    assert "n(row.askable_posts)" not in body
    telegram = payload["platforms"]["telegram"]
    assert telegram["askable_posts"] == 2
    assert telegram["measured_posts"] == 3
    assert payload["platforms"]["dzen"]["askable_posts"] is None
    assert "публ" in body or "снимок имеют" in body


def test_a_missing_collector_and_a_gated_one_are_not_painted_the_same(client):
    """Two ``blocked`` rows, two owners: our absent code, or an absent credential."""
    _seed_platform("max", fmt="news", published=3)
    _seed_platform("telegram", fmt="news", published=4)
    payload = client.get("/api/metrics/coverage").json()
    by_platform = payload["platforms"]
    assert by_platform["max"]["collector"] is None
    assert by_platform["telegram"]["collector"] is not None
    assert by_platform["max"]["status"] == by_platform["telegram"]["status"] == "blocked", (
        "both are `blocked`, which is exactly why the chip alone is not enough"
    )

    script = _script(client)
    table = _gap_table(script)
    css = _css(client)
    ours_cls = re.search(r'ours:\s*\{\s*cls:\s*"([^"]+)"', table).group(1)
    operator_cls = re.search(r'operator:\s*\{\s*cls:\s*"([^"]+)"', table).group(1)
    assert ours_cls != operator_cls, "the two silences share one colour"
    ours_label = re.search(r'ours:\s*\{[^}]*label:\s*"([^"]+)"', table).group(1)
    operator_label = re.search(r'operator:\s*\{[^}]*label:\s*"([^"]+)"', table).group(1)
    assert ours_label != operator_label
    for cls in (ours_cls, operator_cls):
        assert f".{cls}" in css, f"{cls} is not defined in app.css"
    # The kind is derived from the structured field, never by matching the
    # server's prose: the reason string stays the server's own wording.
    body = _coverage_body(script)
    assert "row.collector" in body
    assert "COVERAGE_GAP.ours" in body
    assert "COVERAGE_GAP.operator" in body
    assert "esc(row.reason)" in body
    assert "статус не распознан" in body


def test_the_dzen_line_states_the_split_and_refuses_to_invent_it(client):
    body = _dzen_body(_script(client))
    page = (ROOT / "tgfarm" / "ui" / "index.html").read_text(encoding="utf-8")
    assert 'id="metrics-coverage-dzen"' in page
    # The two `mapped_*` counters are aliased because `posted` is also read below
    # to tell an empty journal from a silent collector; the alias may not change
    # the printer. Every counter taken off the payload must go through `items()`:
    # a payload from an older `metrics_status()` lacks the key, and a missing key
    # is «нет отчёта», never a zero attribution.
    for alias in re.findall(r"const (\w+) = dz\.[a-z_]+;", body):
        assert f"items({alias})" in body, alias
    assert {"withId", "posted"} == set(re.findall(r"const (\w+) = dz\.[a-z_]+;", body))
    assert "items(dz.published_posts)" in body
    assert "items(dz.measured_posts)" in body
    assert "n(dz." not in body, "a report counter must not be coerced through n()"
    # A read that did not happen is not an attribution of zero.
    assert "не прочитана" in body
    assert "esc(why.slice" in body
    assert body.index("if (failed") < body.index("const dz = dzMetrics")
    # The wrapper carries no invented class: the styling lives on the container the
    # page declares, so the markup cannot reference a selector that does not exist.
    assert 'class="coverage-dzen-attribution"' not in body


# -------------------------------------------------------------- cancelled versus
def test_cancelled_gets_its_own_class_and_word_in_the_pipeline_strip(client):
    script = _script(client)
    table = _stage_table(script)
    css = _css(client)
    assert re.search(r"ok:\s*\{", table) and re.search(r"cancelled:\s*\{", table)
    cancelled_cls = re.search(r'cancelled:\s*\{\s*cls:\s*"([^"]+)"', table).group(1)
    assert cancelled_cls not in ("stage-ok", "stage-err"), cancelled_cls
    assert f".{cancelled_cls}" in css
    assert "прерван" in table


def test_the_pipeline_strip_no_longer_folds_every_non_ok_into_an_error(client):
    body = _pipeline_body(_script(client))
    assert 's.status === "ok" ? "stage-ok" : "stage-err"' not in body
    assert 'stage.status === "cancelled"' in body
    assert 'STAGE_STATUS[stage.status] || { cls: "stage-err"' in body, (
        "an unknown status must keep the red: it may not be promoted to 'nothing happened'"
    )
    # The counters of a cancelled stage still ride on `items()`, never on `n()`,
    # and the server's error string stays escaped.
    assert "items(stage.out, stage.counters_state)" in body
    assert "esc((s.error || \"\").slice(0, 40))" in body
    assert "${s.out}" not in body and "${s.in}" not in body


def test_a_cancelled_run_is_reported_as_cancelled_and_not_as_ok(client):
    """The journal chip used to be two-state, so cancellation read as success."""
    script = _script(client)
    chip = _run_chip(script)
    assert 'stages.some((s) => s.status === "error")' in chip
    assert 'stages.some((s) => stage.status === "cancelled")' in chip
    assert "прерван" in chip
    classes = re.findall(r'class="tag ([a-z-]+)"', chip)
    assert len(classes) == 3 and len(set(classes)) == 3, classes
    assert "tag-ok" in classes and "tag-warn" in classes and "tag-cancelled" in classes
    # Failure is decided first, so a run that both failed and was cut short still
    # reports the thing the operator has to act on.
    assert chip.index('"error"') < chip.index('"cancelled"')
    assert "runOutcomeChip(stages)" in _slice(script, "function renderRuns(runs)", "/* ---------")


def test_the_cancelled_look_is_shared_by_both_readers_of_the_journal(client):
    """``.stage-cancelled`` and ``.tag-cancelled``: one outcome, one colour, two cards."""
    css = _css(client)
    for selector in (".stage-cancelled", ".tag-cancelled", ".stage-cancelled .stage-dot"):
        assert selector in css, selector
    # The dot goes hollow, so the difference survives a screenshot with no colour.
    dot = css[css.index(".stage-cancelled .stage-dot") :]
    assert "transparent" in dot[:200]
    assert ".stage-err" in css and ".stage-ok" in css
