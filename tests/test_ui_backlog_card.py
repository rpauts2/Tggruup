"""UI contract regressions for the publication-queue card.

`/api/backlog` answers the question the operator asks in words — «что происходит
с очередью» — and the answer it ships is deliberately partial: an age, a lead or
a deadline that could not be computed arrives as `null`, not as `0`. That only
helps if the panel keeps the difference visible. These tests read `tgfarm/ui/app.js`
the way a browser does and pin the four rules the card exists to hold:

* a `null` ratio, age or lead prints «нет отчёта», never `0,0%` / `0 ч` / `0×`;
* the chip list for waiting states is the server's `BACKLOG_WAITING` list, in
  step, so a new state cannot be counted and left unlabelled;
* "the network died", "the server answered with an error" and "the read came
  back empty" are three wordings, and none of them reuses last pass's numbers;
* every server-supplied string reaches the DOM through `esc()`.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

APP_CSS = Path(__file__).resolve().parent.parent / "tgfarm" / "ui" / "app.css"

STATE_START = "  const BACKLOG_STATE = {"
CARD_START = "  function renderBacklog(payload, failure) {"
CARD_END = "  // The Dzen attribution split"


@pytest.fixture()
def client():
    from tgfarm.api import create_app

    with TestClient(create_app(with_scheduler=False), base_url="http://127.0.0.1") as test_client:
        yield test_client


@pytest.fixture()
def script(client) -> str:
    """Fetched through the app, the way the browser gets it, so a path or media
    type that drifted cannot hide behind a direct file read."""
    return client.get("/ui/app.js").text


def _chunk(text: str, start_marker: str, end_marker: str) -> str:
    start = text.index(start_marker)
    return text[start:text.index(end_marker, start)]


@pytest.fixture()
def card(script: str) -> str:
    return _chunk(script, CARD_START, CARD_END)


# --------------------------------------------------------------------- wiring
def test_the_card_lives_on_the_overview_with_the_other_honesty_cards(client):
    html = client.get("/").text
    assert 'id="backlog-list"' in html
    assert 'id="backlog-meta"' in html
    # Next to the publication-decision card it borrows the blocking reason from,
    # and before the freshness/coverage cards it deliberately does not restate.
    assert html.index("publication-decision") < html.index("backlog-list")
    assert html.index("backlog-list") < html.index("metrics-freshness")


def test_the_panel_fetches_the_endpoint_the_api_serves(script):
    """The fetch table and the route table must name the same path."""
    assert 'backlog: "/api/backlog"' in script

    from tgfarm.api import create_app

    routes = {getattr(route, "path", "") for route in create_app(with_scheduler=False).routes}
    assert "/api/backlog" in routes, "the fetch must not point at a route that drifted"


def test_a_dead_read_leaves_no_previous_snapshot_on_screen(script):
    """Every card in this view re-renders on every pass, including the pass where
    it died; leaving last run's queue on screen reports a queue that has moved."""
    start = script.index("renderBacklog(", script.index(CARD_END) + 1)
    call = script[start:start + 260]
    assert 'renderBacklog("backlog" in data ? data.backlog : null,' in call
    assert "reasons.backlog" in call, "the failure is handed to the renderer"
    assert 'new Error("очередь не получена")' in call, "and worded as this card's own silence"


# ------------------------------------------------------- no invented numbers
def test_a_ratio_the_server_could_not_compute_is_not_zero(script):
    """`pct()` ends in `|| 0`, so it cannot be handed a `null`.

    `reject_rate` and `avg_er` are exactly that shape: a denominator of no rows,
    and `AVG(err)` over no measurements. Printing `0,0%` would report a measured
    quiet where the honest answer is «нет отчёта».
    """
    helpers = _chunk(script, "  const rate = (v) =>", "  const esc = (s) =>")
    assert 'typeof v === "number"' in helpers
    assert "нет отчёта" in helpers

    kpis = _chunk(script, '{ label: "Отклонено"', '{ label: "Обложек"')
    assert "rate(s.reject_rate)" in kpis
    assert "rate(s.avg_er)" in kpis
    assert "pct(s.reject_rate)" not in kpis
    assert "pct(s.avg_er)" not in kpis
    assert "замеров не было" in kpis, "an unmeasured average says so on its own hint line"


def test_an_age_that_was_never_taken_prints_no_report(script):
    helpers = _chunk(script, "  const hrs = (v) =>", "  const leadVs")
    assert 'typeof v === "number"' in helpers
    assert "нет отчёта" in helpers
    card = _chunk(script, CARD_START, CARD_END)
    assert "hrs(row.oldest_age_hours)" in card
    assert "hrs(item.oldest_age_hours)" in card


def test_a_lead_without_output_explains_why_it_is_absent(script, card):
    """Neither infinity nor 0×: the relation between intake and output simply has
    no second term."""
    helpers = _chunk(script, "  const leadVs = (v) =>", CARD_START)
    assert "не вычисляется" in helpers
    assert "Infinity" not in helpers
    assert "leadVs(totals.lead)" in card
    assert "leadVs(rates.lead)" in card



def test_an_unknown_number_never_reaches_the_summary_line(card):
    assert "hrs(" in card
    assert "leadVs(" in card
    assert "typeof totals.overdue === \"number\"" in card


# ------------------------------------------------------------------- honesty
def test_the_waiting_chips_cover_exactly_the_states_the_server_counts(script):
    """A state the card counts in `ждут` and has no chip for travels as
    «состояние не распознано» — better than a wrong label, but a drift worth
    catching the day it happens."""
    from tgfarm.api import BACKLOG_WAITING

    block = _chunk(script, STATE_START, "  // Age is stated in hours")
    keys = set(re.findall(r"^\s{4}(\w+): \{ cls:", block, re.MULTILINE))

    assert keys == set(BACKLOG_WAITING), (
        f"panel chips {sorted(keys)} vs server states {sorted(BACKLOG_WAITING)}"
    )
    assert "publish_unknown" in keys, (
        "51 live rows sit in this state; the queue card may not be where the "
        "operator loses them"
    )


def test_a_draft_is_never_shown_as_late(card):
    """`overdue` is a number only for a bucket of bookings that all carry a time.
    A draft has no deadline, so it gets no chip; an unreadable booking bucket
    says how many rows lack a time instead of publishing a too-small count."""
    assert 'const late = typeof row.overdue === "number"' in card
    assert 'row.status === "scheduled"' in card
    assert "без времени брони" in card
    assert "просрочено ${n(row.overdue)}" in card


def test_the_blocking_reason_is_the_ledgers_and_is_read_as_such(card):
    assert "const decision = data.publication;" in card
    assert "PUBLICATION_REASON[decision.reason]" in card
    assert "гейтов с удержанными" in card
    # No publish tick has journalled a decision yet: that is the absence of an
    # answer, not an answer of «публикует ноль».
    assert "«публикует ноль», это отсутствие ответа" in card
    assert "причина не записана" in card


def test_a_state_the_panel_does_not_know_travels_verbatim(script, card):
    assert "const BACKLOG_STATE = {" in script
    assert "const state = BACKLOG_STATE[row.status]" in card
    assert "состояние не распознано" in card
    assert 'esc(row.status || "unknown")' in card


def test_the_disabled_tap_is_visible_on_the_platform_line(card):
    """Channels switched off produce zeros that mean «оператор закрыл кран», not
    «ферма сломалась» — so the enabled count rides on the line."""
    assert "item.channels_enabled" in card
    assert "каналов " in card


def test_an_empty_queue_is_not_an_error(card):
    assert "if (!platforms.length)" in card
    assert "очередь пуста не потому" in card


def test_every_server_string_reaches_the_dom_through_esc(card):
    for needle in (
        "esc(item.platform",
        "esc(row.status",
        "esc(data.error)",
        "esc(decision.reason",
        "esc(message)",
        "esc(state.label)",
    ):
        assert needle in card, f"{needle} must be escaped: the strings come from the database"


# ------------------------------------------------------------------ failures
def test_the_three_silences_keep_three_wordings(card):
    assert "очередь не прочитана" in card, "the server answered with an error"
    assert "сервер не ответил" in card, "the network did not answer"
    assert "Состояние очереди не прочитано:" in card
    assert "Сеть не отвечает:" in card
    assert "Чтение очереди не удалось:" in card, "the read came back with `error` set"
    assert card.count("Ни одной цифры не показано") >= 2, (
        "both failure paths must refuse to render numbers, not just one"
    )


def test_a_server_side_read_error_replaces_the_numbers_with_itself(card):
    assert "if (data.error)" in card
    assert "esc(data.error)" in card


# ------------------------------------------------------------------- shape lock
def test_the_panel_reads_exactly_the_fields_the_endpoint_returns(client, card):
    """Shape lock, scraped from the renderer: a rename on either side breaks here.

    `error` and `publication` are the two the endpoint may answer with `null`, so
    they are in the payload's key set but carry no sub-keys; the guard below still
    requires the keys to exist.
    """
    payload = client.get("/api/backlog", headers=_auth_headers()).json()

    # `[a-z0-9_]` and not `[a-z_]`: the band keys carry digits (`lt_24h`), and a
    # pattern that stops at the first digit turns this lock into a check of `ge_`.
    groups = {
        "data": r"\bdata\.([a-z_][a-z0-9_]*)",
        "totals": r"\btotals\.([a-z_][a-z0-9_]*)",
        "item": r"\bitem\.([a-z_][a-z0-9_]*)",
        "flow": r"\bflow\.([a-z_][a-z0-9_]*)",
        "week": r"\bweek\.([a-z_][a-z0-9_]*)",
        "rates": r"\brates\.([a-z_][a-z0-9_]*)",
        "row": r"\brow\.([a-z_][a-z0-9_]*)",
        "ages": r"\bages\.([a-z_][a-z0-9_]*)",
        "decision": r"\bdecision\.([a-z_][a-z0-9_]*)",
    }
    read = {name: set(re.findall(pattern, card)) for name, pattern in groups.items()}
    for name, keys in read.items():
        assert keys, f"the renderer stopped naming its {name} fields — the lock is vacuous"

    for key in read["data"]:
        assert key in payload, f"panel reads {key!r} at the top level; endpoint omits it"
    for key in read["totals"]:
        assert key in payload["totals"], f"panel reads totals.{key}; the block omits it"
    for key in read["decision"]:
        assert key in {"reason", "gates"}, f"decision.{key} is not part of the journal's shape"

    # One platform entry with every sub-block populated, seeded by this test: a
    # key the renderer only reads from an empty card would otherwise be checked
    # against nothing.
    from uuid import uuid4

    from tgfarm.api import _backlog
    from tgfarm.db.models import Channel, Post
    from tgfarm.db.session import session_scope

    platform = f"blui{uuid4().hex[:6]}"
    channel_id = f"bluic_{uuid4().hex[:6]}"
    now = datetime.now(UTC)
    stamps = {
        "scheduled": (now - timedelta(hours=50), now - timedelta(hours=20)),
        "draft": (now - timedelta(hours=3), None),
        "gated": (now - timedelta(hours=30), None),
        "publish_unknown": (now - timedelta(hours=60), None),
        "failed": (now - timedelta(hours=90), None),
        "publishing": (now - timedelta(hours=1), None),
    }
    with session_scope() as session:
        session.add(
            Channel(id=channel_id, title="Backlog UI", niche="test", platform=platform)
        )
        for index, (status, (created, booked)) in enumerate(stamps.items()):
            session.add(
                Post(
                    public_id=f"blui{index}{uuid4().hex[:6]}",
                    channel_id=channel_id,
                    hook="Заголовок",
                    body="Тело поста.",
                    status=status,
                    created_at=created,
                    scheduled_at=booked,
                    attempts=2 if status == "failed" else 0,
                )
            )

    try:
        entry = next(
            item for item in _backlog()["platforms"] if item["platform"] == platform
        )
        for key in read["item"]:
            assert key in entry, f"panel reads {key!r} per platform; the entry omits it"
        for key in read["flow"]:
            assert key in entry["flow"], f"panel reads flow.{key}; the block omits it"
        for key in read["week"]:
            assert key in entry["week"], f"panel reads week.{key}; the block omits it"
        for key in read["rates"]:
            assert key in entry["rates"], f"panel reads rates.{key}; the block omits it"
        buckets = {row["status"]: row for row in entry["stock"]}
        assert set(buckets) == set(stamps), (
            f"the card counted {sorted(buckets)} of the six seeded waiting states"
        )
        for row in buckets.values():
            for key in read["row"]:
                assert key in row, f"panel reads {key!r} per bucket; it omits it"
            for key in read["ages"]:
                assert key in row["ages"], f"panel reads ages.{key}; the bands omit it"
    finally:
        with session_scope() as session:
            session.execute(delete(Post).where(Post.channel_id == channel_id))
            session.execute(delete(Channel).where(Channel.id == channel_id))


def _auth_headers() -> dict:
    from tgfarm.core.apiauth import HEADER, ensure_token

    return {HEADER: ensure_token()}


# ------------------------------------------------------------------------ css
def test_the_card_uses_a_class_the_stylesheet_defines(script, card):
    """`is-backlog-state` is the only thing that separates the waiting rows
    visually; a class added on the JS side and missing from `app.css` is an
    invisible change."""
    sheet = APP_CSS.read_text(encoding="utf-8")
    used = set(re.findall(r"is-backlog[\w-]*", script))
    assert used, "the card is expected to mark its state rows"
    for token in used:
        assert f".{token}" in sheet, f"{token} is rendered but never styled"
    assert "item.is-backlog-state" in sheet or ".is-backlog-state" in sheet
