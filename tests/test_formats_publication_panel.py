"""The Formats card must show the publication half of the report it is given.

``GET /api/metrics/formats`` (``tgfarm/api.py:5284``) answers ``format_performance``
verbatim, and that report describes the same rows three times over:
``formats[fmt]`` is the legacy count of everything published in the window,
``measurement[fmt]`` is the freshness dimension (how many snapshots are still
alive), and ``measurement[fmt].publication`` plus the report-level ``publication``
block are the evidence dimension — how many of those publications the platform can
actually prove, per ``EVIDENCE_RULES`` in ``tgfarm/s8_analytics/formats.py``.

The card is not legacy-only: ``renderMetricsFormats`` already rendered the first
two. What it read nowhere was ``publication`` — in ``app.js`` that key reached only
the publish-tick card and the backlog card — so the third question never appeared
on the panel that answers it. On the live database that is exactly the sentence an
operator is missing: format ``news`` carries legacy ``avg_engagement 67.8571,
reliable true, n 7`` while its publication block says ``sample_size 0, reliable
false``, and the scale is vk 403 published / 403 evidenced, dzen 57 / 35, telegram
15 / 0.

The trap this file pins is the naming collision. ``publication`` has its own
``measured``, ``avg_engagement``, ``reliable``, ``evidence_status`` and
``fresh_sample_size``, and every one of them divides a different set than the
identically-named freshness value beside it. So the tests below demand that both
denominators are printed, that they are never merged, that a disagreement is shown
rather than resolved, that an absent block reads «не вернул» instead of the zeros
``Number(x || 0)`` would fabricate, and that the report-level ``publish_unknown``
COUNT keeps its own words — it must not borrow the «неизвестно, ушло ли — ждёт
оператора» label of the ``publish_unknown`` STATUS at ``app.js:534``.

Rendering is run for real under node: the block is sliced out of ``app.js`` between
literal code anchors and evaluated against a stub DOM, because a text assertion
cannot tell «нет отчёта» from a printed zero, and that difference is the defect.
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

# Anchors in full, never as short substrings: slicing a function out of app.js with
# a reused two-word anchor silently returns the wrong body.
HELPERS_START = "const $ = (sel, root = document) => root.querySelector(sel);"
HELPERS_END = "const clock = (v) => {"
NO_REPORT_START = 'const NO_REPORT = "нет отчёта";'
NO_REPORT_END = "const rowsCell = (v) =>"
EVIDENCE_START = "const EVIDENCE_STATUS = {"
CARD_START = "function renderMetricsFormats(payload) {"
PUBLICATION_START = "const PUBLICATION_EVIDENCE_CHIP = {"
PUBLICATION_END = (
    "// «Что происходит с очередью» used to be answerable only by hand-written SQL,"
)
FORMATS_RU_START = "const FORMAT_RU = {"
FORMATS_RU_END = "const PLATFORM_RU = {"

# The three sets the block may never fuse, in the file's own words.
PUBLICATION_KEYS = (
    "published",
    "published_evidenced",
    "published_unevidenced",
    "published_unrulled",
    "measured",
    "measured_evidenced",
    "measured_unrulled",
    "measured_unevidenced",
    "sample_size",
    "avg_engagement",
    "reliable",
    "fresh_sample_size",
    "fresh_unevidenced",
    "fresh_avg_engagement",
    "evidence_status",
)

# Concatenation and interpolation are the two ways a value enters the HTML; a
# wrapper call in between breaks the pattern, a bare `+ pub.x` is it.
RAW_PRINT = re.compile(
    r"(?:\+\s*|\$\{\s*)(?:pub|data|payload|legacy|row|totals|rules)\.[A-Za-z_]+"
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

    The block quotes the idiom it forbids while explaining why it is forbidden, so
    a naive substring check would fail on the explanation instead of on the defect.
    """
    without_blocks = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(line.split("//", 1)[0] for line in without_blocks.splitlines())


@pytest.fixture(scope="module")
def publication_block(script) -> str:
    return _slice(script, PUBLICATION_START, PUBLICATION_END)


@pytest.fixture(scope="module")
def formats_card(script) -> str:
    return _slice(script, CARD_START, PUBLICATION_START)


# ------------------------------------------------------------------- api: payload


def test_endpoint_carries_the_publication_block_at_report_level(client):
    """The panel may not invent this dimension: the answer has to contain it.

    A fresh throwaway database publishes nothing, so the counts are zeros — and
    zeros counted here are still the contract: the keys, the ``publish_unknown``
    count and the platform rules have to arrive, because the card reads them by
    name and prints «нет отчёта» when a name is missing.
    """
    body = client.get("/api/metrics/formats").json()
    publication = body["publication"]
    for key in PUBLICATION_KEYS:
        assert key in publication, f"панели нужен ключ publication.{key}"
    assert isinstance(publication["publish_unknown"], int)
    assert publication["rules"], "правила доказательства обязаны приходить с отчётом"
    assert "posts.tg_message_id" in publication["rules"].values()
    assert "crosspost_logs.status = 'posted'" in publication["rules"].values()
    # Report-wide numbers stay out of the per-format blocks: a per-format copy of a
    # report property would read as a count belonging to that format.
    assert "rules" not in str(body["measurement"]), "правила — только на уровне отчёта"
    assert "publish_unknown" not in str(body["measurement"])
    # The legacy keys keep their meaning: nothing was subtracted from them.
    assert body["published_total"] == publication["published"]
    assert body["measured_total"] == publication["measured"]


# --------------------------------------------------------------- ui: source pins


def test_publication_block_prints_its_own_counters_beside_the_legacy_ones(
    publication_block,
):
    """Every subset gets a word of its own; none of them is borrowed from freshness."""
    for needle in (
        "items(pub.published)",
        "items(pub.published_evidenced)",
        "items(pub.published_unevidenced)",
        "items(pub.published_unrulled)",
        "items(pub.measured)",
        "items(pub.measured_evidenced)",
        "items(pub.sample_size)",
        "items(pub.fresh_sample_size)",
        "pubAvg(pub.avg_engagement)",
        "pubAvg(pub.fresh_avg_engagement)",
        "pubWord(pub.reliable",
        "publicationUnknownCount",
        "publicationRules",
        "publicationConflict",
    ):
        assert needle in publication_block, f"блок обязан печатать {needle}"
    # The denominators are named in words, not implied by position.
    for phrase in (
        "подтверждено правилом",
        "знаменатель подтверждённой средней",
        "живой знаменатель",
        "по отчёту целиком",
    ):
        assert phrase in publication_block, f"знаменатель не назван словами: {phrase}"


def test_publish_unknown_count_does_not_borrow_the_status_label(publication_block):
    """«неизвестно, ушло ли — ждёт оператора» describes one post in that state.

    The report-level ``publish_unknown`` is a COUNT of such posts that sits in no
    denominator, and a reader who met the status wording first would take the count
    for a verdict about a single publication.
    """
    code = _code_only(publication_block)
    assert "неизвестно, ушло ли" not in code
    assert "ждёт оператора" not in code
    assert "в статусе publish_unknown: " in code
    assert "ни в один знаменатель выше" in code, "счёт обязан сказать, где он не считается"


def test_publication_statuses_have_their_own_words(publication_block):
    """`publication.evidence_status` reuses ok/mixed/never for another question.

    The freshness chip at `EVIDENCE_STATUS` says «замеры»; the publication chip must
    say «публикации», otherwise the same word on the same row means two things.
    """
    for label in (
        "публикации подтверждены",
        "подтверждённые и без подтверждения вперемешку",
        "ни одной подтверждённой публикации",
        "для этих площадок нет правила",
    ):
        assert label in publication_block, f"нет своего слова для статуса: {label}"
    assert 'typeof pub.evidence_status === "string"' in publication_block
    assert "статус не записан" in publication_block, "незнакомый код печатается как пришёл"


def test_the_card_reads_publication_at_both_levels(formats_card):
    """Report level and per format, and the legacy keys stay in place."""
    for needle in (
        "data.publication",
        "evidence.publication",
        "formatsPublicationPanel(publication, data)",
        "formatPublicationRow(pub, row)",
        "publicationChip(pub)",
        '#metrics-formats-publication',
    ):
        assert needle in formats_card, f"карточка обязана читать {needle}"
    # The legacy row is untouched: same keys, same meaning, nothing subtracted.
    assert "Number(row.published || 0)" in formats_card
    assert "row.reliable === true" in formats_card
    assert "const publication = (data.publication" in formats_card


def test_no_fabricated_zero_in_the_publication_path(publication_block):
    """P34: `Number(x || 0)` prints a measurement that never happened.

    The gate is `typeof v === "number"` for counts and averages and
    `typeof v === "boolean"` for a verdict, so an absent key cannot become `0` or
    `false`. The pre-existing fabrication vectors of the card itself stay where
    they are — this slice must not add one.
    """
    code = _code_only(publication_block)
    assert "Number(" not in code
    assert "|| 0" not in code
    assert "?? 0" not in code
    assert 'typeof v === "number"' in code
    assert 'typeof v === "boolean"' in code
    assert "NO_REPORT" in code
    assert "items(" in code
    # A default object is the same defect in a cleaner costume: `if (!pub) pub =
    # {published: 0, reliable: true}` fabricates the whole measurement, so no
    # numeric or boolean literal may be assigned to a payload key in this block.
    for fabricated in (": 0", ": 0,", ": true", ": false", "= 0"):
        assert fabricated not in code, f"подставное значение в пути publication: {fabricated}"


def test_external_strings_cannot_reach_the_dom_unescaped(publication_block, formats_card):
    offenders = RAW_PRINT.findall(publication_block)
    assert offenders == [], f"publication: внешнее значение печатается без обёртки: {offenders}"
    for field in ("esc(rule)", "esc(platform)", "esc(publicationCounters(pub))"):
        assert field in publication_block, f"{field} не проходит через esc"
    assert "esc(PUBLICATION_ABSENT)" in publication_block
    assert "esc(conflict)" in publication_block


def test_markup_and_style_own_their_containers():
    """Report level is a separate block, above the rows it qualifies, and it styles."""
    html = (UI_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="metrics-formats-publication"' in html
    assert html.index('id="metrics-formats-meta"') < html.index('id="metrics-formats-publication"')
    assert html.index('id="metrics-formats-publication"') < html.index('id="metrics-formats"')
    css = (UI_DIR / "app.css").read_text(encoding="utf-8")
    assert ".formats-publication" in css
    assert ".formats-pub-conflict" in css
    assert "is-without-publication" in css
    # A row that never received the block is not a dead row: the red bar of
    # `.is-dead-evidence` would call an unread thing a measured one.
    rule = css[css.index(".item.is-without-publication") :]
    assert "248,113,113" not in rule.split("\n\n")[0]


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
const body = spec.slices.map(slice).join("\n");
const document = {
  createElement: () => ({ style: {}, classList: { add() {}, remove() {} } }),
  addEventListener: () => {},
  querySelector: () => null,
};
const render = new Function("document", "window", body + "\n; return renderMetricsFormats;")(
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

IDS = ["#metrics-formats", "#metrics-formats-meta", "#metrics-formats-publication"]


def _publication(**overrides) -> dict:
    """One full publication block: the dzen shape 57 published / 35 evidenced.

    Every key of the server block is present so that a typo in a name shows up as a
    missing string in the rendered row rather than as a silent absence.
    """
    block = {
        "published": 57,
        "published_evidenced": 35,
        "published_unevidenced": 22,
        "published_unrulled": 0,
        "measured": 41,
        "measured_evidenced": 30,
        "measured_unrulled": 1,
        "measured_unevidenced": 10,
        "sample_size": 31,
        "avg_engagement": 52.3416,
        "reliable": True,
        "fresh_sample_size": 9,
        "fresh_unevidenced": 3,
        "fresh_avg_engagement": 61.5,
        "evidence_status": "mixed",
    }
    block.update(overrides)
    return block


def _format(fmt: str, published: int, measured: int, reliable: bool, average: float) -> dict:
    """The legacy row: its keys keep the meaning they always had."""
    return {
        "format": fmt,
        "published": published,
        "measured": measured,
        "avg_engagement": average,
        "reliable": reliable,
        "n": measured,
    }


def _measurement(fmt: str, pub, *, fresh: int, stale: int, status: str = "ok") -> dict:
    entry = {
        "format": fmt,
        "measured": fresh + stale,
        "published": 0,
        "fresh": fresh,
        "stale": stale,
        "avg_engagement": 10.0,
        "fresh_avg_engagement": 12.0,
        "fresh_reliable": fresh >= 3,
        "evidence_status": status,
        "last_measured_at": "2026-11-04T10:00:00+00:00",
        "fresh_last_measured_at": "2026-11-05T08:00:00+00:00" if fresh else None,
    }
    if pub is not None:
        entry["publication"] = pub
    return entry


def _full_payload() -> dict:
    """Four formats, four different evidence shapes, one collision on purpose.

    * `news` is the live collision: legacy `reliable: true` over 7 measurements
      while the publication block proves nothing (`sample_size: 0`, `reliable:
      false`) — both verdicts have to appear.
    * `article` is fully proven (vk shape, 403 / 403).
    * `stat` is the telegram shape, 15 published and 0 evidenced.
    * `flash` has no publication block at all, so one payload carries both the
      measured and the unreported per-format case.
    """
    news_pub = _publication(
        published=7, published_evidenced=0, published_unevidenced=7, published_unrulled=0,
        measured=7, measured_evidenced=0, measured_unrulled=0, measured_unevidenced=7,
        sample_size=0, avg_engagement=None, reliable=False, fresh_sample_size=0,
        fresh_unevidenced=0, fresh_avg_engagement=0.0, evidence_status="never",
    )
    article_pub = _publication(
        published=403, published_evidenced=403, published_unevidenced=0, published_unrulled=0,
        measured=120, measured_evidenced=120, measured_unrulled=0, measured_unevidenced=0,
        sample_size=120, avg_engagement=8.225, reliable=True, fresh_sample_size=40,
        fresh_unevidenced=0, fresh_avg_engagement=9.1, evidence_status="ok",
    )
    stat_pub = _publication(
        published=15, published_evidenced=0, published_unevidenced=0, published_unrulled=15,
        measured=4, measured_evidenced=4, measured_unrulled=4, measured_unevidenced=0,
        sample_size=4, avg_engagement=3.5, reliable=True, fresh_sample_size=2,
        fresh_unevidenced=0, fresh_avg_engagement=4.0, evidence_status="unknown",
    )
    return {
        "window_days": 30,
        "formats": {
            "news": _format("news", 7, 7, True, 67.8571),
            "article": _format("article", 403, 120, True, 8.225),
            "stat": _format("stat", 15, 4, False, 3.5),
            "flash": _format("flash", 2, 1, False, 1.0),
        },
        "measurement": {
            "news": _measurement("news", news_pub, fresh=0, stale=7, status="stale"),
            "article": _measurement("article", article_pub, fresh=40, stale=80),
            "stat": _measurement("stat", stat_pub, fresh=2, stale=2),
            "flash": _measurement("flash", None, fresh=1, stale=0),
        },
        # Report-wide totals that add up out of the four rows above, so a reader
        # (and the assertion below) can check that nothing was subtracted:
        # 405 proven + 7 disproven + 15 with no rule = 427 published.
        "publication": {
            **_publication(
                published=427, published_evidenced=405, published_unevidenced=7,
                published_unrulled=15, measured=132, measured_evidenced=121,
                measured_unrulled=4, measured_unevidenced=7, sample_size=125,
                avg_engagement=9.7712, reliable=True, fresh_sample_size=43,
                fresh_unevidenced=0, fresh_avg_engagement=10.4, evidence_status="mixed",
            ),
            "publish_unknown": 4,
            "rules": {
                "telegram": "posts.tg_message_id",
                "dzen": "crosspost_logs.status = 'posted'",
                "vk": "crosspost_logs.status = 'posted'",
            },
        },
        "measured_total": 132,
        "published_total": 427,
        "fresh_measured_total": 43,
        "stale_measured_total": 89,
        "evidence_status": "mixed",
        "min_sample": 3,
        "stale_after_days": 5,
    }


def _render_cases(tmp_path: Path, cases: list[dict]) -> dict[str, dict[str, str]]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node недоступен — поведение панели не проверить")
    harness = tmp_path / "harness.cjs"
    harness.write_text(HARNESS, encoding="utf-8")
    spec = {
        "slices": [
            [HELPERS_START, HELPERS_END],
            [NO_REPORT_START, NO_REPORT_END],
            [EVIDENCE_START, CARD_START],
            [CARD_START, PUBLICATION_START],
            [PUBLICATION_START, PUBLICATION_END],
            [FORMATS_RU_START, FORMATS_RU_END],
        ],
        "cases": [{"name": case["name"], "ids": IDS, "payload": case["payload"]} for case in cases],
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


def test_rendered_card_shows_publication_beside_the_legacy_numbers(tmp_path):
    """The full block: proven subset, unevidenced, unrulled, both averages and rules."""
    nodes = _render_cases(tmp_path, [{"name": "full", "payload": _full_payload()}])
    panel = nodes["full"]["#metrics-formats-publication"]["innerHTML"]
    rows = nodes["full"]["#metrics-formats"]["innerHTML"]

    # Report level: the legacy totals keep their own words and the block is joined
    # to them, never substituted for them.
    assert "по отчёту целиком: опубликовано 427 шт · с замерами 132 шт" in panel
    assert "публикаций всего 427 шт" in panel
    assert "подтверждено правилом 405 шт" in panel
    assert "правило есть, не подтверждено 7 шт" in panel
    assert "для площадки правила нет 15 шт" in panel
    assert "замеров всего 132 шт · подтверждённых замеров 121 шт" in panel
    assert "знаменатель подтверждённой средней 125 шт" in panel
    assert "средняя по подтверждённым 9.8" in panel
    assert "живой знаменатель 43 шт" in panel
    assert "вывод: подтверждённых публикаций довольно" in panel
    # The count of rows in `publish_unknown`, in its own words.
    assert "в статусе publish_unknown: 4 шт — эти строки не входят ни в один знаменатель выше" in panel
    # The rules arrive verbatim and escaped: they are data, not markup.
    assert "posts.tg_message_id" in panel
    assert "crosspost_logs.status = &#39;posted&#39;" in panel
    assert "telegram —" in panel and "dzen —" in panel
    assert "публикации: подтверждённые и без подтверждения вперемешку" in panel

    # Per format, and the legacy line of the same row is still there.
    assert "опубликовано 403" in rows
    assert "с замерами 120" in rows
    assert "публикаций всего 403 шт · подтверждено правилом 403 шт" in rows
    assert "ни одной подтверждённой публикации" in rows
    assert "правило подтверждения" not in rows, "правила — уровень отчёта, а не формата"
    assert "publish_unknown" not in rows, "счёт publish_unknown — уровень отчёта"


def test_rendered_card_partially_evidenced_and_zero_evidenced(tmp_path):
    """57 / 35 and 15 / 0 are different sentences and must not print alike."""
    payload = _full_payload()
    payload["formats"] = {"stat": _format("stat", 15, 4, False, 3.5)}
    payload["measurement"] = {
        "stat": payload["measurement"]["stat"],
        "news": payload["measurement"]["news"],
    }
    payload["publication"] = {
        **_publication(
            published=57, published_evidenced=35, published_unevidenced=22,
            published_unrulled=0, measured=41, measured_evidenced=30,
            measured_unrulled=1, measured_unevidenced=11, sample_size=30,
            avg_engagement=52.3416, reliable=True, fresh_sample_size=9,
            fresh_unevidenced=3, fresh_avg_engagement=61.5, evidence_status="mixed",
        ),
        "publish_unknown": 0,
        "rules": {"dzen": "crosspost_logs.status = 'posted'"},
    }
    nodes = _render_cases(tmp_path, [{"name": "partial", "payload": payload}])
    panel = nodes["partial"]["#metrics-formats-publication"]["innerHTML"]
    rows = nodes["partial"]["#metrics-formats"]["innerHTML"]

    assert "публикаций всего 57 шт · подтверждено правилом 35 шт" in panel
    assert "published_unevidenced" not in panel, "ключ сервера не печатается вместо слов"
    # A measured zero is a zero, not «нет отчёта» — and prints differently from it.
    assert "в статусе publish_unknown: 0 шт" in panel
    assert "publish_unknown: — (нет отчёта)" not in panel
    assert "публикаций всего 15 шт · подтверждено правилом 0 шт" in rows
    assert "правило есть, не подтверждено 0 шт · для площадки правила нет 15 шт" in rows
    # telegram shape: unrulled rows stay in the sample, so the chip says there is no
    # rule, and that is not the same claim as «nothing is proven».
    assert "для этих площадок нет правила" in rows
    assert "сервер не вернул блок publication" not in rows, "здесь блок пришёл"


def test_rendered_card_never_makes_an_absent_block_look_measured(tmp_path):
    """The one mutation this card must not survive: `pub = {…: 0}` when nothing came.

    A fallback object would print «подтверждено правилом 0 шт» with a green
    `публикации подтверждены` chip and the server's rule strings — a readiness claim
    about evidence the backend never reported, and the exact reading P34 exists to
    prevent. Every assertion here is about the absence, so each of them dies on that
    fallback independently.
    """
    full = _full_payload()
    no_block = {key: value for key, value in full.items() if key != "publication"}
    no_block["measurement"] = {
        fmt: {key: value for key, value in entry.items() if key != "publication"}
        for fmt, entry in full["measurement"].items()
    }
    nodes = _render_cases(tmp_path, [{"name": "absent", "payload": no_block}])
    panel = nodes["absent"]["#metrics-formats-publication"]["innerHTML"]
    rows = nodes["absent"]["#metrics-formats"]["innerHTML"]

    assert "это не «ноль подтверждённых», это нет отчёта" in panel
    assert "подтверждено правилом 0" not in panel
    assert "публикаций всего 0" not in panel
    assert "avg_engagement 0" not in panel
    assert "знаменатель подтверждённой средней 0" not in panel
    assert "tag-ok" not in panel, "зелёный чип там, где ничего не измерялось"
    assert "publish_unknown" not in panel, "счёт не приходит, когда блок не пришёл"
    assert "posts.tg_message_id" not in panel, "правила не печатаются из головы"
    assert "подтверждённость: нет отчёта" in panel
    assert "по отчёту целиком: опубликовано 427 шт" in panel, "легасивные числа на месте"
    assert rows.count("сервер не вернул блок publication") == 4
    assert "подтверждено правилом 0" not in rows


def test_rendered_card_without_the_block_says_it_was_not_returned(tmp_path):
    """An older server: the dimension is absent, and absence may not print as zero.

    One case has no report-level block, one has the block but no `rules` and no
    `publish_unknown`, and one row keeps `measurement` while its per-format
    publication is missing — three silences, all of them named.
    """
    full = _full_payload()
    no_block = {key: value for key, value in full.items() if key != "publication"}
    no_block["measurement"] = {
        fmt: {key: value for key, value in entry.items() if key != "publication"}
        for fmt, entry in full["measurement"].items()
    }
    thin = json.loads(json.dumps(full))
    del thin["publication"]["rules"]
    del thin["publication"]["publish_unknown"]
    nodes = _render_cases(
        tmp_path,
        [
            {"name": "no_block", "payload": no_block},
            {"name": "thin_block", "payload": thin},
            {"name": "error", "payload": {"error": "database is locked"}},
        ],
    )

    empty = nodes["no_block"]["#metrics-formats-publication"]["innerHTML"]
    assert "сервер не вернул блок publication" in empty
    assert "это не «ноль подтверждённых», это нет отчёта" in empty
    assert "подтверждено правилом 0 шт" not in empty, "отсутствие не печатается нулём"
    assert "publish_unknown: — (нет отчёта)" not in empty
    assert "подтверждённость: нет отчёта" in empty
    assert "публикации: подтверждены" not in empty, "нет вердикта там, где нечего вердиктировать"
    # The legacy totals of the same report still print — the block's absence does
    # not silence what the card already showed.
    assert "по отчёту целиком: опубликовано 427 шт" in empty

    rows = nodes["no_block"]["#metrics-formats"]["innerHTML"]
    assert rows.count("сервер не вернул блок publication") == 4, "у каждого формата своя строка"
    assert "is-without-publication" in rows
    assert "подтверждено правилом 0" not in rows

    thin_panel = nodes["thin_block"]["#metrics-formats-publication"]["innerHTML"]
    assert "правила доказательства сервер не вернул" in thin_panel
    assert "posts.tg_message_id" not in thin_panel, "выдуманное правило из воздуха"
    assert "в статусе publish_unknown: — (нет отчёта)" in thin_panel

    failed = nodes["error"]["#metrics-formats-publication"]["innerHTML"]
    assert "Форматы не прочитаны" in failed and "ноль" in failed


def test_rendered_card_prints_the_collision_and_resolves_nothing(tmp_path):
    """`news`: legacy `reliable: true` over 7 measurements, publication `sample_size 0`.

    The panel is not allowed to pick a winner — it has no basis — but it is
    required to show that the two denominators disagree, in both sets of words.
    """
    nodes = _render_cases(tmp_path, [{"name": "collision", "payload": _full_payload()}])
    rows = nodes["collision"]["#metrics-formats"]["innerHTML"]
    # Rows are split on the row element only: `.item-main`/`.item-meta`/`.item-side`
    # are nested and must not be read as a new row.
    news = next(
        chunk
        for chunk in re.split(r'(?=<div class="item[ "])', rows)
        if "новость" in chunk
    )

    assert "опубликовано 7 · с замерами 7" in news, "легасивная строка не потеряна"
    assert "публикаций всего 7 шт · подтверждено правилом 0 шт" in news
    assert "средняя по подтверждённым нет отчёта" in news, "avg None — это не 0.0"
    assert "знаменатель подтверждённой средней 0 шт" in news
    assert "вывод: подтверждённых публикаций мало" in news
    # Both verdicts, on one row, named by their own sets.
    assert "знаменатели разошлись" in news
    assert "по всем замерам — данных достаточно (7 шт)" in news
    assert "по подтверждённым публикациям — мало (0 шт)" in news
    assert "расхождение не разрешено: это два разных вопроса" in news
    assert "formats-pub-conflict" in news
    # The freshness dimension keeps its own chip next to the publication one, in
    # its own words: «все замеры устарели» is about snapshots, not about
    # publications, and the absent block gets a third wording of its own.
    assert "все замеры устарели" in news
    assert "публикации: ни одной подтверждённой публикации" in news


def test_rendered_card_escapes_publication_strings(tmp_path):
    """Rules and statuses are untrusted: markup must not reach the DOM as markup."""
    payload = _full_payload()
    payload["publication"]["rules"] = {"vk": "<img src=x onerror=alert(1)>"}
    payload["publication"]["evidence_status"] = "\"><script>alert(2)</script>"
    payload["measurement"]["article"]["publication"]["evidence_status"] = "<b>х</b>"
    nodes = _render_cases(tmp_path, [{"name": "hostile", "payload": payload}])
    panel = nodes["hostile"]["#metrics-formats-publication"]["innerHTML"]
    rows = nodes["hostile"]["#metrics-formats"]["innerHTML"]

    assert "<img" not in panel and "<script>" not in panel
    assert "&lt;img src=x onerror=alert(1)&gt;" in panel
    assert "&quot;&gt;&lt;script&gt;alert(2)" in panel
    assert "vk — &lt;img" in panel
    assert "&lt;b&gt;х&lt;/b&gt;" in rows
