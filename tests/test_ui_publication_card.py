"""UI contract regressions for the publication-decision card.

`cadb5a4` made the publish tick journalling: `/api/status` now carries
`publication` — the ranked reason (`published / publish_failed / gated /
attempted_not_sent / nothing_due`, plus `stage_error` with `measured: false`)
and the named gate ledger from `tgfarm/s7_publish`. Nothing rendered it, so
«почему ферма ничего не публикует?» again had a machine answer and no panel
answer.

Like the rest of the UI suite these tests read /ui/app.js the way a browser
does and pin the honesty rules this repo enforces by test:

* an absent or unmeasured decision never renders as a zero;
* unknown reason and gate codes travel verbatim on the colourless chip;
* "no data yet", "HTTP error" and "network does not answer" are three wordings;
* every server-supplied string reaches the DOM through `esc()`.
"""

from pathlib import Path

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


REASONS = ("published", "publish_failed", "gated", "attempted_not_sent", "nothing_due", "stage_error")


# ------------------------------------------------------------------ the premise
def test_status_endpoint_actually_serves_publication():
    """The card renders a field that exists: pipeline.status() ships `publication`."""
    source = (Path(__file__).resolve().parent.parent / "tgfarm" / "pipeline.py").read_text(encoding="utf-8")
    assert '"publication": last_publish_decision()' in source
    assert '"measured": False' in source


def test_publication_card_lives_on_the_overview_with_the_other_honesty_cards(client):
    html = client.get("/").text
    assert 'id="publication-decision"' in html
    assert 'id="publication-decision-meta"' in html
    # The same view as «Свежесть метрик» and «Покрытие измерений», and the
    # sub-line states where the answer comes from.
    assert html.index("publication-decision") < html.index("metrics-freshness")
    assert "Решение последнего publish-тика из журнала прогонов" in html


def test_absent_publication_says_no_data_not_a_zero(client):
    """`publication: null` is "no tick has journalled yet", not "the farm sent 0"."""
    script = _script(client)
    body = _chunk(script, "function renderPublicationDecision", "function renderMetricsFormats")
    assert "Нет данных" in body
    assert "ни один publish-тик" in body
    # The fabricated zero is the exact bug: `n()` ends in `|| 0`, so the
    # counters must ride on the type-testing `items()` helper, never on n().
    assert "items(decision.due)" in body
    assert "items(decision.published)" in body
    assert "n(decision." not in body


def test_unmeasured_stage_error_is_rendered_as_no_data(client):
    """`measured: false` may borrow a plausible chip but never a plausible number."""
    script = _script(client)
    body = _chunk(script, "function renderPublicationDecision", "function renderMetricsFormats")
    assert "decision.measured === false" in body
    assert "нет данных" in body
    assert "а не нулями" in body
    # The unmeasured branch returns before the counters line is ever built.
    assert body.index('decision.measured === false') < body.index("из очереди")


# ------------------------------------------------------------------- the codes
def test_reason_table_pins_the_pipeline_vocabulary(client):
    script = _script(client)
    block = _chunk(script, "const PUBLICATION_REASON", "const PUBLICATION_GATE")
    for reason in REASONS:
        assert f"{reason}:" in block


def test_gate_table_covers_every_named_gate_the_publisher_counts(client):
    """The chip table is derived from the server's own vocabulary.

    It used to be a hand-copied list of seven codes, which is exactly why a new
    reason could ship without a label; `empty_gate_report()` is the single place
    the publisher names them, so the panel is now checked against all of it.
    """
    from tgfarm import s7_publish

    script = _script(client)
    block = _chunk(script, "const PUBLICATION_GATE", "function renderPublicationDecision")
    for code in s7_publish.empty_gate_report():
        assert f"{code}:" in block, f"{code} is counted by the server and unlabeled by the panel"


def test_a_dry_run_is_labelled_as_nothing_sent(client):
    """PART 2: the panel must be able to say "these posts were not delivered".

    A post held back because the farm does not send is not an empty queue and not
    a publication, and the wording has to carry that: "не отправлено" for both
    dry-run codes, and "ждём sync_channels" for the chat we cannot name.
    """
    from tgfarm import s7_publish

    script = _script(client)
    block = _chunk(script, "const PUBLICATION_GATE", "function renderPublicationDecision")
    for code in (
        s7_publish.GATE_FARM_NOT_LIVE,
        s7_publish.GATE_BOT_TOKEN_MISSING,
    ):
        assert f"{code}:" in block
        label = block.split(f"{code}:")[1].splitlines()[0]
        assert "\u043e\u0442\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u043e" in label, label
    chat_label = block.split(f"{s7_publish.GATE_UNKNOWN_TG_CHAT}:")[1].splitlines()[0]
    assert "sync_channels" in chat_label, chat_label


def test_unknown_reason_is_printed_verbatim_and_stays_uncoloured(client):
    """A reason the server invented must not be dyed into a known diagnosis."""
    script = _script(client)
    body = _chunk(script, "function renderPublicationDecision", "function renderMetricsFormats")
    assert "PUBLICATION_REASON[reasonKey]" in body
    assert 'class="tag tag-none">${esc(reasonKey' in body
    assert "причина не записана" in body


def test_unknown_gate_code_is_printed_verbatim_and_stays_uncoloured(client):
    script = _script(client)
    body = _chunk(script, "function renderPublicationDecision", "function renderMetricsFormats")
    assert "PUBLICATION_GATE[code]" in body
    assert "esc(label || code)" in body
    # The warm chip belongs to the known gate alone; the stranger gets tag-none.
    assert 'label ? "tag-warn" : "tag-none"' in body
    # A gate count is a number or nothing: no `|| 0` behind it.
    assert 'typeof count === "number"' in body
    assert "нет данных" in body


# ------------------------------------------------------------------ the failures
def test_http_error_network_silence_and_absence_are_three_different_wordings(client):
    script = _script(client)
    body = _chunk(script, "function renderPublicationDecision", "function renderMetricsFormats")
    assert "Сеть не отвечает" in body
    assert "Решение о публикации не прочитано" in body
    assert "Нет данных: ни один publish-тик" in body
    # The discrimination is the `api()` rejection signature, not a guess.
    assert r"/\s->\s\d/" in body


def test_error_field_is_checked_before_any_totals(client):
    script = _script(client)
    body = _chunk(script, "function renderPublicationDecision", "function renderMetricsFormats")
    assert "payload.error" in body
    assert body.index("payload.error") < body.index("из очереди")


def test_refresh_renders_the_card_before_the_all_failed_throw(client):
    """A fully dead backend must leave «сеть не отвечает», not a frozen old verdict."""
    script = _script(client)
    body = _chunk(script, "async function refresh()", "async function runCycle()")
    assert 'renderPublicationDecision("status" in data' in body
    assert body.index("renderPublicationDecision") < body.index('throw new Error("API недоступен")')
    # The rejection reason travels, so HTTP and network wordings can differ.
    assert "reasons[key] = outcome.reason" in body
    assert "Promise.allSettled(" in body


# ------------------------------------------------------------------- the hygiene
def test_publication_rows_reuse_the_existing_item_layout(client):
    """No bespoke block: the card is the same .item/.item-main/.item-side family."""
    css = client.get("/ui/app.css").text
    for selector in (".item ", ".item-main", ".item-side", ".item-title", ".item-meta",
                     ".tag-none", ".tag-warn", ".alert-err", ".list"):
        assert selector in css
    script = _script(client)
    body = _chunk(script, "function renderPublicationDecision", "function renderMetricsFormats")
    assert '<div class="item"><div class="item-main"><div class="item-title">' in body
    assert '<div class="item-side">' in body


def test_publication_text_reaches_the_dom_escaped(client):
    script = _script(client)
    body = _chunk(script, "function renderPublicationDecision", "function renderMetricsFormats")
    for field in ("esc(known.label)", "esc(reasonKey", "esc(label || code)", "esc(code)",
                  "esc(message)", "esc(payload.error)", "esc(counters)"):
        assert field in body
    for raw in ("${decision.", "${code}", "+ code", "${label}", "${counters"):
        assert raw not in body, f"raw interpolation of {raw!r}"
