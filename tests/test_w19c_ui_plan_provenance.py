"""W19-C UI contract: the provenance of a post, in the queue area that shows it.

The operator's question is one sentence — «этот пост из какого решения плана и на
какой слот он был забронирован?» — and the two columns that answer it
(`posts.plan_entry_id`, `posts.plan_slot_at`, W19-B) plus the resolved
`plan_public_id` reach the panel through `GET /api/posts`, which `renderPosts`
already feeds both the post cards and the «Ближайшие публикации» queue.

This file pins the three properties this repo keeps having to relearn:

* a value that was never written is not a value that was measured — a `null` slot
  may not become `01.01.1970`, and a `0` reference may not become a plan entry;
* a booked slot is a moment on the clock, usually in the future, and `dt()` answers
  "только что" for any future instant (its elapsed time is negative, and every
  negative number is under a minute). The slot may only be rendered by the helper
  that already separates "a run that just happened" from "a run that is booked";
* every string in the payload is stored text the API echoes raw, so it reaches the
  DOM through `esc()`.

Rendering is run for real under node, on the file the browser fetches: only the
produced string can tell «неизвестно» from a fabricated slot. The last test in the
file is the seam: it takes the keys the block dereferences and requires each of
them in the row the server actually sends, because each half passing its own test
is exactly how a rename survives review.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from tgfarm.core.apiauth import ensure_token
from tgfarm.db.models import Channel, Post
from tgfarm.db.session import session_scope

ROOT = Path(__file__).resolve().parent.parent
UI_DIR = ROOT / "tgfarm" / "ui"
APP_CSS = UI_DIR / "app.css"

# Anchors in full: a short reused anchor silently slices the wrong body.
HELPERS_START = "  const $ = (sel, root = document) => root.querySelector(sel);"
HELPERS_END = "  // The admin API can force-publish"
EXEC_START = "  // ── Исполнитель плана: работает ли потребитель и что он уже сделал ───────"
EXEC_RENDER_END = "  async function loadPlanExecution() {"
PROV_START = "  // ── Провенанс поста: из какого решения плана этот пост"
PROV_END = "  function postCard(p) {"
POSTCARD_END = "  function renderPosts(posts) {"
QUEUE_START = "  function renderPosts(posts) {"
QUEUE_END = "  function renderUnresolvedWorklist(payload, failure) {"
DIST_START = "  function renderDistributionPlan(payload) {"
DIST_END = "  window.tgfarmSetRuntimeMode = async (mode) => {"

#: The three fields the read is allowed to offer, and the block is allowed to read.
PROVENANCE_KEYS = {"plan_entry_id", "plan_slot_at", "plan_public_id"}


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
def provenance(script) -> str:
    return _slice(script, PROV_START, PROV_END)


@pytest.fixture(scope="module")
def postcard(script) -> str:
    return _slice(script, "  function postCard(p) {", POSTCARD_END)


@pytest.fixture(scope="module")
def queue(script) -> str:
    return _slice(script, QUEUE_START, QUEUE_END)


@pytest.fixture(scope="module")
def distribution(script) -> str:
    return _slice(script, DIST_START, DIST_END)


# -------------------------------------------------------------------- wordings
def test_the_three_states_have_three_different_words(provenance):
    """One sentence per state the columns can be in, and no state shares a word."""
    for phrase in ("по плану", "без плана", "не читается"):
        assert phrase in provenance, f"состояние потеряло свою формулировку: {phrase}"


def test_a_post_the_plan_never_ordered_is_stated_and_not_blanked(provenance):
    guard = provenance[provenance.index("function postProvenance") :]
    assert "p.plan_entry_id === null" in guard or "plan_entry_id === null" in guard
    assert "без плана" in guard


def test_an_unreadable_entry_keeps_its_reference_in_the_sentence(provenance):
    guard = provenance[provenance.index("function postProvenance") :]
    assert "p.plan_entry_id" in guard, (
        "the lost entry is not named, so the operator cannot tell a pruned plan "
        "from no plan"
    )
    assert "не читается" in guard


def test_the_block_admits_an_unread_payload_instead_of_answering_it(provenance):
    """A server that does not send the keys says nothing; `null` says "no plan"."""
    assert "in p" in provenance, "an absent field was merged into a null one"
    assert "неизвестно" in provenance


def test_the_slot_is_rendered_by_the_helper_that_knows_future_from_past(provenance):
    """`dt()` on a booked slot is the defect at app.js:2173, not a formatting choice.

    Every future instant has a negative elapsed time, so `dt()` answers "только
    что" for it; `nextRunCell()` is this file's existing answer to that, and the
    provenance may not invent a second one that gets it wrong.
    """
    assert "nextRunCell(" in provenance, "the slot was rendered without the "
    "future-aware clock the plan block already uses"
    assert re.search(r"\bdt\(\s*p\.plan_slot_at", provenance) is None, (
        "dt() on a future booking reports it as a moment that already passed"
    )


def test_no_number_or_date_is_fabricated_from_a_null(provenance):
    code = "\n".join(line.split("//", 1)[0] for line in provenance.splitlines())
    for coerced in ("|| 0", "?? 0", "Number(p.plan", "new Date(p.plan_slot_at)"):
        assert coerced not in code, f"{coerced} invents the answer the API refused"
    assert "1970" not in code


def test_every_server_string_reaches_the_dom_through_esc(provenance, postcard, queue):
    for needle in ("esc(p.plan_public_id)", "esc(String(p.plan_entry_id))"):
        assert needle in provenance, f"{needle} is stored text interpolated raw"
    chip = provenance[provenance.index("const postProvenanceChip") :]
    assert '<span class="plan-chip">${postProvenance(p)}</span>' in chip, (
        "the chip is where the escaping already happened; escaping it twice would "
        "show the operator &amp;lt; instead of the stored text"
    )
    interpolations = re.findall(
        r"\$\{([^}]*)\}", "\n".join((provenance, postcard, queue))
    )
    unsafe = [
        expr for expr in interpolations
        if re.search(r"\bp\.plan_[a-z_]+\b", expr) and "esc(" not in expr and "String(" not in expr
    ]
    assert not unsafe, f"unescaped interpolation of provenance fields: {unsafe}"


def test_the_chip_class_the_block_emits_is_already_styled(provenance):
    """No new stylesheet: an unstyled class name is an invisible change."""
    sheet = APP_CSS.read_text(encoding="utf-8")
    used: set[str] = set()
    for match in re.finditer(r'class="([a-z0-9 -]+)"', provenance):
        used.update(match.group(1).split())
    assert used, "the block emits no markup the test can check"
    for token in used:
        assert f".{token}" in sheet, f"{token} is rendered but never styled"


# ------------------------------------------------------------------- call sites
def test_the_provenance_is_drawn_where_the_operator_reads_the_post(postcard, queue):
    assert "postProvenanceChip(p)" in postcard, (
        "the cards still show a post without saying what ordered it"
    )
    assert queue.count("postProvenanceChip(p)") == 2, (
        "«Ближайшие публикации» has two branches — the queue and, when nothing is "
        "waiting, the most recent posts — and provenance drawn in only one of them "
        "disappears exactly when the operator has no queue to read"
    )


def test_the_queue_area_no_longer_reports_a_future_slot_as_a_past_one(distribution):
    """The defect class the brief names, pinned where it lives.

    `dt(post.scheduled_at)` answered "только что" for a booking made for tonight;
    the same helper that fixed the plan block's `next_run_at` is the fix here.
    """
    assert "dt(post.scheduled_at)" not in distribution, (
        "a queue preview booking in the future is rendered as an elapsed time"
    )
    assert "nextRunCell(post.scheduled_at)" in distribution


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
const build = new Function("document", "window", body + "\n; return postProvenanceChip;");
const chip = build(document, {});
const out = [];
for (const item of spec.cases) {
  out.push({ name: item.name, html: chip(item.post) });
}
process.stdout.write(JSON.stringify(out));
"""

SLICES = [
    [HELPERS_START, HELPERS_END],
    [EXEC_START, EXEC_RENDER_END],
    [PROV_START, PROV_END],
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
            {
                "slices": SLICES,
                "cases": [{"name": c["name"], "post": c.get("post")} for c in cases],
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


def _post(**overrides) -> dict:
    post = {"plan_entry_id": None, "plan_slot_at": None, "plan_public_id": None}
    post.update(overrides)
    return post


def test_rendered_planned_post_names_the_plan_and_the_booked_slot(tmp_path):
    html = _render_cases(tmp_path, [
        {
            "name": "booked",
            "post": _post(
                plan_entry_id=17,
                plan_public_id="pl0aaaabbbbccccdddd",
                plan_slot_at="2096-11-05T09:00:00+00:00",
            ),
        },
        {
            "name": "noslot",
            "post": _post(plan_entry_id=17, plan_public_id="pl0aaaabbbbccccdddd"),
        },
    ])

    booked = html["booked"]
    assert "по плану" in booked
    assert "pl0aaaabbbbccccdddd" in booked
    assert "17" in booked
    assert "только что" not in booked, (
        "a booking in 2096 was rendered as a moment that already passed"
    )
    assert "не читается" not in booked
    assert "без плана" not in booked
    assert "01.01.1970" not in booked and "1970" not in booked

    noslot = html["noslot"]
    assert "по плану" in noslot
    assert "слот" in noslot and "1970" not in noslot
    assert "неизвестно" not in noslot, "a planned post with no slot is not an unread one"


def test_rendered_slot_in_the_past_keeps_the_elapsed_word(tmp_path):
    html = _render_cases(tmp_path, [
        {
            "name": "past",
            "post": _post(
                plan_entry_id=3,
                plan_public_id="plpast",
                plan_slot_at="2020-01-02T03:04:05+00:00",
            ),
        },
    ])
    assert "назад" in html["past"] or "янв" in html["past"]
    assert "по плану" in html["past"]


def test_rendered_unplanned_post_says_no_plan_and_prints_no_slot(tmp_path):
    html = _render_cases(tmp_path, [{"name": "plain", "post": _post()}])
    assert "без плана" in html["plain"]
    for forbidden in ("по плану", "не читается", "неизвестно", "слот", "0", "—"):
        assert forbidden not in html["plain"], f"{forbidden} invented an answer"


def test_rendered_gone_entry_says_unread_and_keeps_what_it_still_knows(tmp_path):
    html = _render_cases(tmp_path, [
        {
            "name": "gone",
            "post": _post(plan_entry_id=904, plan_slot_at="2096-11-05T09:00:00+00:00"),
        },
        {
            "name": "goneslot",
            "post": _post(plan_entry_id=904),
        },
    ])

    gone = html["gone"]
    assert "не читается" in gone
    assert "904" in gone, "the reference is the last thing the row still knows"
    assert "без плана" not in gone, "a pruned plan is not the absence of one"
    assert "по плану" not in gone, "the snapshot was not read, so it may not be named"
    assert "только что" not in gone
    assert "слот" in gone, "the booked slot is a column on the post and survives the loss"
    assert html["goneslot"] != html["gone"], "the slot was dropped from the unread state"


def test_rendered_missing_fields_admit_they_were_not_sent(tmp_path):
    html = _render_cases(tmp_path, [
        {"name": "empty", "post": {}},
        {"name": "partial", "post": {"plan_entry_id": None}},
        {"name": "none", "post": None},
    ])
    for name, chip in html.items():
        assert "неизвестно" in chip, f"{name} answered a question it was not asked"
        for claimed in ("по плану", "без плана", "не читается"):
            assert claimed not in chip, f"{name} claimed {claimed} out of silence"


def test_rendered_unparseable_slot_stays_unreadable(tmp_path):
    html = _render_cases(tmp_path, [
        {
            "name": "junk",
            "post": _post(plan_entry_id=5, plan_public_id="pl5", plan_slot_at="не дата"),
        },
        {
            "name": "zero",
            "post": _post(plan_slot_at=""),
        },
    ])
    for chip in html.values():
        assert "1970" not in chip
        assert "NaN" not in chip
        assert "Invalid Date" not in chip


def test_rendered_hostile_plan_id_stays_text(tmp_path):
    hostile = '<img src=x onerror="fetch(/evil)">'
    html = _render_cases(tmp_path, [
        {"name": "hostile", "post": _post(plan_entry_id=9, plan_public_id=hostile)},
    ])
    chip = html["hostile"]
    assert hostile not in chip
    assert "&lt;img" in chip, "the stored text is shown, as data"
    assert chip.count("<img") == 0, "no image tag may be built out of a plan id"


# ---------------------------------------------------------------- the seam test
def _row_with_its_own_provenance(client) -> dict:
    """A row this file wrote, served through the endpoint the panel calls."""
    status = "w19cseam"
    channel_id = "w19cseamchan"
    post_id = "w19cseampost"
    try:
        with session_scope() as session:
            session.add(Channel(id=channel_id, title="W19-C seam", platform="telegram"))
            session.add(
                Post(
                    public_id=post_id,
                    channel_id=channel_id,
                    hook="seam",
                    body="seam",
                    status=status,
                )
            )
        response = client.get(f"/api/posts?status={status}&limit=5")
        assert response.status_code == 200, response.text
        rows = response.json()["posts"]
        assert len(rows) == 1, rows
        return rows[0]
    finally:
        with session_scope() as session:
            session.execute(delete(Post).where(Post.public_id == post_id))
            session.execute(delete(Channel).where(Channel.id == channel_id))


def test_the_panel_only_dereferences_keys_the_server_sends(script, client, provenance):
    """One contract, checked against the live payload rather than a fixture.

    `tgfarm/api.py` and `tgfarm/ui/app.js` are edited in different worktrees, so
    the rename that survives review is the one each half's own test cannot see.

    W21-C amendment, and it is a real change of assertion rather than a rewording:
    the key literals are no longer written in `api.py` at all. `_post_view` forwards
    the dict of `tgfarm/editorial_plan.py::plan_provenance`, because the distribution
    queue preview answers the same question about the same row and a second copy of
    the triple is what this wave was ordered to remove. The check therefore splits
    in two halves the seam can actually fail — the owner really names each key, and
    the card really goes through the owner — while the payload assertion above,
    which is the half that catches a rename, is untouched.
    """
    reads = set(re.findall(r"(?<![A-Za-z0-9_$])p\.([a-z][a-z0-9_]*)", provenance))
    provenance_reads = {key for key in reads if key.startswith("plan_")}
    assert provenance_reads == PROVENANCE_KEYS, (
        f"the block reads {sorted(provenance_reads)}; the read offers "
        f"{sorted(PROVENANCE_KEYS)}"
    )
    row = _row_with_its_own_provenance(client)
    for key in provenance_reads:
        assert key in row, f"the panel dereferences {key}, the payload never sends it"
    owner = (ROOT / "tgfarm" / "editorial_plan.py").read_text(encoding="utf-8")
    card = (ROOT / "tgfarm" / "api.py").read_text(encoding="utf-8")
    for key in PROVENANCE_KEYS:
        assert f'"{key}"' in owner, f"the owner no longer names {key}"
        assert "plan_provenance(" in card, f"{key} is served by a card that left the owner"


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
