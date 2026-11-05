"""Every row the reconciliation board shows must carry a reason a human can act on.

Live measurement behind these seeds (2026-11-05, read-only `mode=ro`, 365
`failed` + 51 `publish_unknown` + 47 `gated`): not one reason was empty, but the
whole `terminal` bucket rendered the *same* 340-character blob that ends in a
Playwright `Call log:` block, and the whole `unknown` bucket glued a raw English
driver sentence onto the Russian one. `publish_error` is untrusted platform text
— it can carry a credential-bearing URL — so the board was pasting it, uncut and
unredacted, into an operator card.

Test hygiene: `tests/conftest.py` builds ONE session database shared by the whole
suite, so bucket sizes are asserted as a delta against a preview taken before
seeding, every row seeded here is deleted by the autouse fixture, and each seeded
row is aged so it sorts to the FRONT of its own bucket (`terminal` is newest
first, `unknown`/`held` are oldest first) — a bucket truncated at the board's
200-row cap must still contain the row under test.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete

from tgfarm.db.models import Channel, Post
from tgfarm.db.session import session_scope
from tgfarm.review import review_queue

#: What this file owns, so the teardown hands the shared database back clean.
_seeded_channels: list[str] = []
_seeded_public_ids: list[str] = []

#: The exact sentence `maintenance.publication_evidence_repair` writes when it
#: moves a false `published` row to `publish_unknown` (and sets `published_at`
#: back to NULL). Kept in sync by hand because maintenance owns the string.
EVIDENCE_REPAIR_NOTE = (
    "history: marked published with no remote evidence of any kind "
    "(no telegram message id / no `posted` journal row); verify the "
    "destination before trusting or retrying it"
)

#: The real 340-character blob every one of the 365 live `failed` rows carries.
LIVE_DZEN_OUTAGE_ERROR = (
    "expired: never submitted and older than 48 h after a transport outage; "
    "content is stale. Requeue manually if it is still relevant. Last error: "
    "редактор Дзена: Page.goto: net::ERR_NAME_NOT_RESOLVED at "
    "https://dzen.ru/profile/editor/create\nCall log:\n"
    '  - navigating to "https://dzen.ru/profile/editor/create", waiting until "domcontentloaded"\n'
)

#: A 500+ character provider dump whose actionable line is the last one. The
#: wrapper line is long on purpose: it is what the old `error[:260]` kept, and
#: what the board must drop in favour of the exception that actually says why.
TRACEBACK_ERROR = (
    "Публикация прервана на стороне площадки: " + "detail " * 60 + "\n"
    "Traceback (most recent call last):\n"
    '  File "/app/tgfarm/s7_publish/__init__.py", line 1005, in publish_one\n'
    "    outcome = driver.submit(page, payload)\n"
    '  File "/app/tgfarm/s7_publish/dzen_web.py", line 220, in submit\n'
    "    return self._press_publish()\n"
    "httpx.ConnectError: [Errno -3] Temporary failure in name resolution\n"
)


@pytest.fixture(autouse=True)
def _drop_own_rows():
    """Leave no residue: the suite shares one session database."""
    yield
    with session_scope() as session:
        if _seeded_public_ids:
            session.execute(delete(Post).where(Post.public_id.in_(_seeded_public_ids)))
        if _seeded_channels:
            session.execute(delete(Channel).where(Channel.id.in_(_seeded_channels)))
    _seeded_channels.clear()
    _seeded_public_ids.clear()


def _seed(
    status: str,
    platform: str,
    *,
    error: str | None = None,
    provider: str = "offline",
    published_at: datetime | None = None,
    age_hours: float | None = None,
) -> str:
    """One post in the state under test, aged to sort first inside its bucket."""
    suffix = uuid4().hex[:8]
    channel_id = f"reason_{platform}_{suffix}"
    public_id = f"reason{suffix}"
    # `terminal` sorts newest-first, `unknown`/`held` oldest-first: 0 h for a
    # failure, 9000 h (≈ 1 year) for anything else, so the row survives the cap.
    if age_hours is None:
        age_hours = 0.0 if status == "failed" else 9000.0
    stamp = datetime.now(UTC) - timedelta(hours=age_hours)
    with session_scope() as session:
        session.add(Channel(id=channel_id, title="Сверка", niche="tech", platform=platform))
        session.add(
            Post(
                public_id=public_id,
                channel_id=channel_id,
                status=status,
                hook="Пост для сверки причин",
                body="Текст",
                attempts=9,
                publish_error=error,
                published_at=published_at,
                generation_provider=provider,
                created_at=stamp,
                updated_at=stamp,
            )
        )
    _seeded_channels.append(channel_id)
    _seeded_public_ids.append(public_id)
    return public_id


def _board(monkeypatch, *, approval_required: bool = False, dzen_requires_llm: bool = False):
    """The board with pinned settings — never the operator's local `.env` values."""
    from tgfarm import review

    monkeypatch.setattr(
        review,
        "get_settings",
        lambda: SimpleNamespace(
            approval_required=approval_required, dzen_requires_llm=dzen_requires_llm
        ),
    )
    return review_queue(limit=200)


def _item(board, bucket: str, public_id: str) -> dict:
    items = board["buckets"][bucket]
    for item in items:
        if item["public_id"] == public_id:
            return item
    pytest.fail(
        f"{public_id} not in the shown {bucket} bucket ({len(items)} rows, "
        f"total in that bucket {board['counts'][bucket]})"
    )


def test_board_never_renders_an_empty_reason(monkeypatch):
    """A row with nothing stored still has to tell the operator what to do."""
    before = _board(monkeypatch)["counts"]
    blank = _seed("failed", "vk")
    whitespace = _seed("failed", "vk", error="   \n  ")
    traceback_row = _seed("failed", "dzen", error=TRACEBACK_ERROR)
    outage = _seed("failed", "dzen", error=LIVE_DZEN_OUTAGE_ERROR)

    board = _board(monkeypatch)
    assert board["counts"]["terminal"] == before["terminal"] + 4
    for public_id in (blank, whitespace, traceback_row, outage):
        reason = _item(board, "terminal", public_id)["reason"]
        assert reason.strip(), f"{public_id} renders an empty reason"
        assert "\n" not in reason, f"{public_id} renders a multi-line log blob"


def test_terminal_collapse_keeps_the_cause_and_drops_the_log(monkeypatch):
    """Raw traceback text must become the one line that says why."""
    before = _board(monkeypatch)["counts"]
    row = _seed("failed", "dzen", error=TRACEBACK_ERROR)
    outage = _seed("failed", "dzen", error=LIVE_DZEN_OUTAGE_ERROR)

    board = _board(monkeypatch)
    assert board["counts"]["terminal"] == before["terminal"] + 2

    reason = _item(board, "terminal", row)["reason"]
    assert "httpx.ConnectError" in reason
    assert "Traceback (most recent call last)" not in reason
    assert 'File "' not in reason
    assert "submit(page, payload)" not in reason
    assert len(reason) <= 260 + len("Публикация не состоялась (финальный отказ): ")

    live = _item(board, "terminal", outage)["reason"]
    assert live.startswith("expired:")
    assert "net::ERR_NAME_NOT_RESOLVED" in live
    assert "Call log" not in live and "domcontentloaded" not in live
    assert len(live) < len(LIVE_DZEN_OUTAGE_ERROR)


def test_terminal_reason_never_leaks_a_credential(monkeypatch):
    """Platform errors carry bearer tokens and signed URLs; the board is a screen."""
    before = _board(monkeypatch)["counts"]
    row = _seed(
        "failed",
        "vk",
        error=(
            "VK API error 5: authorization failed. Request "
            "https://api.vk.com/method/wall.post?access_token=SUPERSECRETVALUE123456 "
            "&v=5.133 headers={'Authorization': 'Bearer AAAABBBBCCCCDDDD'}"
        ),
    )

    board = _board(monkeypatch)
    assert board["counts"]["terminal"] == before["terminal"] + 1
    reason = _item(board, "terminal", row)["reason"]
    assert "SUPERSECRETVALUE123456" not in reason
    assert "AAAABBBBCCCCDDDD" not in reason
    assert "[REDACTED" in reason
    assert "authorization failed" in reason.lower() or "error 5" in reason.lower()


def test_held_reason_per_gate_branch(monkeypatch):
    """Each gate that holds a post must name itself, not fall through to a guess."""
    row = _seed("gated", "vk", provider="compatible")
    board = _board(monkeypatch, approval_required=True)
    assert board["counts"]["held"] >= 1
    assert "TGFARM_APPROVAL_REQUIRED" in _item(board, "held", row)["reason"]

    dzen_row = _seed("gated", "dzen", provider="offline")
    vk_row = _seed("gated", "vk", provider="offline")
    online_dzen = _seed("gated", "dzen", provider="deepseek")
    board = _board(monkeypatch, approval_required=False, dzen_requires_llm=True)
    assert "DZEN_REQUIRES_LLM" in _item(board, "held", dzen_row)["reason"]
    assert "слот ещё не выделен" in _item(board, "held", vk_row)["reason"]
    assert "слот ещё не выделен" in _item(board, "held", online_dzen)["reason"]

    # The offline Dzen draft must not be described as a slot problem when the
    # policy is off, and no branch may render blank.
    board = _board(monkeypatch, approval_required=False, dzen_requires_llm=False)
    for public_id in (row, dzen_row, vk_row, online_dzen):
        reason = _item(board, "held", public_id)["reason"]
        assert reason.strip()
        assert "DZEN_REQUIRES_LLM" not in reason


def test_repaired_publish_unknown_row_is_shown_with_its_note(monkeypatch):
    """`published_at = NULL` is the whole point of the evidence repair; show it."""
    before = _board(monkeypatch)["counts"]
    row = _seed("publish_unknown", "telegram", error=EVIDENCE_REPAIR_NOTE)

    board = _board(monkeypatch)
    assert board["counts"]["unknown"] == before["unknown"] + 1
    item = _item(board, "unknown", row)
    assert item["published_at"] is None
    assert item["status"] == "publish_unknown"
    assert "проверь площадку" in item["reason"].lower()
    assert "verify the destination" in item["reason"]
    assert "\n" not in item["reason"]
    # The Russian instruction plus at most the 200 characters of stored detail.
    assert len(item["reason"]) <= 122 + 1 + 200


def test_unknown_keeps_the_actionable_tail_of_a_long_error(monkeypatch):
    """The cut must not land before the line that says what actually failed."""
    before = _board(monkeypatch)["counts"]
    long_error = (
        "outcome unknown: publisher driver died; verify the platform before retry. "
        + "context " * 40
        + "\nValueError: session expired while uploading the image\nCall log:\n"
        + '  - navigating to "https://dzen.ru/profile/editor/create"\n'
    )
    assert len(long_error) > 200
    row = _seed("publish_unknown", "dzen", error=long_error)

    board = _board(monkeypatch)
    assert board["counts"]["unknown"] == before["unknown"] + 1
    reason = _item(board, "unknown", row)["reason"]
    assert "ValueError: session expired while uploading the image" in reason
    assert "Call log" not in reason
    assert "\n" not in reason
    assert len(reason) <= 200 + 110


def test_unknown_with_a_blank_error_is_not_left_bare(monkeypatch):
    before = _board(monkeypatch)["counts"]
    row = _seed("publish_unknown", "vk", error=" \n ")

    board = _board(monkeypatch)
    assert board["counts"]["unknown"] == before["unknown"] + 1
    reason = _item(board, "unknown", row)["reason"]
    assert reason.strip()
    assert reason == reason.rstrip()
    assert reason.endswith("дубль).")

