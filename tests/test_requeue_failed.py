"""The bulk requeue of terminal publish failures: bounded, consented, journalled —
and it never touches an unknown publication outcome.

Live measurement (2026-11-05, read-only over `tgfarm.db`) that this path exists for:
365 posts sit in `failed` carrying 1890 attempts between them, every one of them a
Дзен row, every one of them closed out by `requeue_unsubmitted` with the sentence
"Requeue manually if it is still relevant." All 365 had `tg_message_id IS NULL`,
`published_at IS NULL` and no `posted` journal row, so none of them is provably on
the platform — but that is a fact about this outage, not a property of the state, and
the guard cannot rely on it. Meanwhile `/api/review/queue` renders 25 cards per
bucket and the only manual path was one click per row, so "act on it" meant 365
clicks or SQL.

`publish_unknown` (51 rows live) is not in scope and never will be: §80. The test
below gives the sweep full consent, a fresh backup and a reason, and still expects
those rows to stay exactly where they are.

Two things make the numbers exact inside the one session database this suite shares:
every row is seeded on a platform name nobody else uses and every call filters by it,
and the autouse teardown deletes this file's posts, channels, journal rows and
`operator_logs` entries. `now` is injected, so `scheduled_at` and the ages are
asserted, not approximated.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, or_, select

from tgfarm.db.models import Channel, CrosspostLog, OperatorLog, Post
from tgfarm.db.session import session_scope
from tgfarm.maintenance import (
    PUBLISH_FAILURE_REQUEUE_APPLY_ENV,
    REQUEUE_BUDGET_MAX,
    requeue_eligibility,
    requeue_failed_publishes,
)

#: A platform name no other test file invents, so every count in this file is a
#: count of this file's own rows rather than of the suite's residue.
PLATFORM = "rqplat"

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)

_seeded_channels: list[str] = []
_seeded_public_ids: list[str] = []


@pytest.fixture(autouse=True)
def _drop_own_rows():
    """Leave no residue: the suite shares one session database."""
    yield
    with session_scope() as session:
        # The batch route journals under the *platform*, not the post, so a delete
        # keyed on public ids alone left `requeue-failed-batch` rows behind and the
        # next route test in this file counted two of them. Both predicates below
        # name this file's own vocabulary — an invented platform and the one action
        # only this path writes — so they cannot eat another file's audit rows.
        session.execute(
            delete(OperatorLog).where(
                or_(OperatorLog.target == PLATFORM, OperatorLog.action == "requeue-failed-batch")
            )
        )
        if _seeded_public_ids:
            session.execute(
                delete(CrosspostLog).where(CrosspostLog.post_public_id.in_(_seeded_public_ids))
            )
            session.execute(
                delete(OperatorLog).where(OperatorLog.target.in_(_seeded_public_ids))
            )
            post_ids = list(
                session.scalars(select(Post.id).where(Post.public_id.in_(_seeded_public_ids))).all()
            )
            if post_ids:
                session.execute(delete(Post).where(Post.id.in_(post_ids)))
        if _seeded_channels:
            session.execute(delete(Channel).where(Channel.id.in_(_seeded_channels)))
    _seeded_channels.clear()
    _seeded_public_ids.clear()


@pytest.fixture()
def enabled(monkeypatch):
    """Pretend every destination is switched on, as the other retry tests do.

    `is_platform_enabled` fails closed for an unknown platform id, and `rqplat` is
    deliberately unknown so that no other test's channel state can leak in here; the
    disabled-platform test overrides this fixture's replacement per platform.
    """
    from tgfarm import platforms

    monkeypatch.setattr(platforms, "is_platform_enabled", lambda _platform: True)
    return monkeypatch


@pytest.fixture()
def consented(enabled, tmp_path, monkeypatch):
    """Consent flag on, plus a `backup/` copy fresh enough to satisfy §80."""
    from tgfarm.core import settings as settings_mod

    monkeypatch.setenv(PUBLISH_FAILURE_REQUEUE_APPLY_ENV, "1")
    settings = settings_mod.get_settings()
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path), raising=False)
    (tmp_path / f"tgfarm-{uuid4().hex[:8]}.db").write_bytes(b"copy")
    return monkeypatch


def _slug() -> str:
    return uuid4().hex[:8]


def _seed_failed(
    *,
    error: str | None = "net::ERR_NAME_NOT_RESOLVED at https://dzen.ru/profile/editor/create",
    attempts: int = 3,
    age_hours: float = 6.0,
    status: str = "failed",
    platform: str = PLATFORM,
    tg_message_id: int | None = None,
    published: bool = False,
) -> str:
    suffix = _slug()
    channel_id = f"rq_{platform}_{suffix}"
    public_id = f"rq{suffix}"
    _seeded_channels.append(channel_id)
    _seeded_public_ids.append(public_id)
    created = NOW - timedelta(hours=age_hours)
    with session_scope() as session:
        session.add(
            Channel(id=channel_id, title="Возврат в очередь", niche="tech", platform=platform)
        )
        session.add(
            Post(
                public_id=public_id,
                channel_id=channel_id,
                status=status,
                hook="Заголовок",
                body="Текст поста.",
                attempts=attempts,
                publish_error=error,
                tg_message_id=tg_message_id,
                published_at=(created + timedelta(hours=1)) if published else None,
                created_at=created,
                updated_at=created,
            )
        )
    return public_id


def _journal(public_id: str, *, status: str = "posted", platform: str = PLATFORM) -> None:
    with session_scope() as session:
        session.add(
            CrosspostLog(
                post_public_id=public_id,
                platform=platform,
                status=status,
                remote_id="remote-1",
            )
        )


def _row(public_id: str) -> Post:
    with session_scope() as session:
        return session.execute(select(Post).where(Post.public_id == public_id)).scalar_one()


def _stamp(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes for `DateTime(timezone=True)` columns."""
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _journal_rows(action: str, target: str) -> list[OperatorLog]:
    with session_scope() as session:
        return list(
            session.scalars(
                select(OperatorLog).where(
                    OperatorLog.action == action,
                    OperatorLog.target == target,
                )
            ).all()
        )


def _sweep(monkeypatch=None, **kwargs):
    """One call, always scoped to this file's platform."""
    kwargs.setdefault("platform", PLATFORM)
    kwargs.setdefault("now", NOW)
    return requeue_failed_publishes(**kwargs)


# --------------------------------------------------------------------- the guard
def test_eligibility_matrix_names_every_refusal():
    """`requeue_eligibility` is the single answer both operator paths ask.

    Asserted as a table because the codes are the contract: the panel toast, the
    journal and the report all print them, and §80 needs `publish_unknown` to be
    distinguishable from "not a failure" rather than folded into it.
    """
    assert requeue_eligibility(status="failed", publish_error="net::ERR_NAME_NOT_RESOLVED") == (
        True,
        "",
    )
    assert requeue_eligibility(status="publish_unknown")[1] == "publish_outcome_unknown"
    assert requeue_eligibility(status="scheduled")[1] == "not_failed"
    assert requeue_eligibility(status="published")[1] == "not_failed"
    assert requeue_eligibility(status="failed", tg_message_id=17)[1] == "already_published"
    assert (
        requeue_eligibility(status="failed", published_at=NOW)[1] == "already_published"
    )
    assert (
        requeue_eligibility(status="failed", journaled_posted=True)[1] == "journaled_posted"
    )
    assert (
        requeue_eligibility(
            status="failed",
            publish_error="редактор Дзена: Connection closed while reading from the driver",
        )[1]
        == "outcome_uncertain"
    )
    assert requeue_eligibility(status="failed", enabled=False)[1] == "platform_disabled"


def test_every_refusal_carries_an_operator_readable_reason():
    """A refusal the operator cannot read is a refusal they will route around."""
    from tgfarm.maintenance import REQUEUE_REFUSALS

    probes = (
        {"status": "publish_unknown"},
        {"status": "gated"},
        {"status": "failed", "tg_message_id": 1},
        {"status": "failed", "journaled_posted": True},
        {"status": "failed", "publish_error": "target page, context or browser has been closed"},
        {"status": "failed", "enabled": False},
    )
    for probe in probes:
        ok, refusal = requeue_eligibility(**probe)
        assert ok is False and refusal
        assert len(REQUEUE_REFUSALS[refusal]) > 30, f"{refusal} has no readable reason"


# ------------------------------------------------------------------- the preview
def test_preview_counts_and_writes_nothing(enabled):
    moved = _seed_failed(attempts=13)
    unknown = _seed_failed(status="publish_unknown", error="request timeout")

    report = _sweep()

    assert report["apply"] is False
    assert report["scanned"] == 1, "only `failed` rows are even candidates for a scan"
    assert report["eligible"] == 1
    assert report["by_platform"] == {PLATFORM: 1}
    assert report["refusals"] == {}, "the unknown row was never a candidate, so never a refusal"
    assert report["changed"] == 0
    assert report["remaining"] == 0
    assert len(report["sample"]) == 1
    assert report["sample"][0]["public_id"] == moved
    assert report["sample"][0]["attempts"] == 13
    assert report["sample"][0]["age_hours"] == 6.0
    assert _row(moved).status == "failed"
    assert _row(unknown).status == "publish_unknown"
    assert _journal_rows("requeue-failed", moved) == []


def test_preview_redacts_the_stored_error_before_showing_it(enabled):
    """`publish_error` is untrusted platform text and may carry a token."""
    public_id = _seed_failed(
        error="https://oauth.dzen.ru/cb?code=ABC123&access_token=SECRETVALUE123456 net::ERR"
    )

    report = _sweep()

    assert "SECRETVALUE123456" not in report["sample"][0]["error"]
    assert _row(public_id).status == "failed"


def test_platform_filter_keeps_another_platform_out_of_the_pass(enabled):
    mine = _seed_failed()
    theirs = _seed_failed(platform="dzen")

    report = _sweep()

    assert report["scanned"] == 1
    assert [row["public_id"] for row in report["sample"]] == [mine]
    assert _row(theirs).status == "failed"


# ----------------------------------------------------------------- the §80 refusal
def test_full_consent_still_never_requeues_an_unknown_outcome(consented):
    """The one test that must not be negotiable.

    Consent flag set, fresh backup present, reason given, `apply=True` — and the
    `publish_unknown` row is not moved, not rewritten, not journalled. A requeue of
    that state is a blind duplicate publication, so it is refused by the same guard
    the single-post click uses, and the refusal is counted and named.
    """
    unknown = _seed_failed(status="publish_unknown", error="timed out", attempts=4)
    failed = _seed_failed()

    report = _sweep(apply=True, reason="выход задержки закончился, материал ещё свежий")

    assert report["changed"] == 1, "the sweep does run — it just does not run on that row"
    assert report["scanned"] == 1, "only `failed` rows are candidates at all"
    # It is not merely skipped: the report counts the unknown-outcome rows in scope
    # and names the rule that keeps them there, so the operator who sees "51" on the
    # queue card learns that this button is not the way to act on them.
    assert report["parked_unknown"] == 1
    assert "§80" in report["parked_unknown_reason"]
    assert report["refusals"] == {}
    assert report["refused_rows"] == []
    assert _row(failed).status == "scheduled"
    row = _row(unknown)
    assert row.status == "publish_unknown"
    assert row.attempts == 4, "a refusal must not even reset the attempt budget"
    assert row.publish_error == "timed out"
    assert row.scheduled_at is None
    assert _journal_rows("requeue-failed", unknown) == []


def test_unknown_outcome_rows_are_never_even_scanned(consented):
    """Filtering on `status == "failed"` in SQL is the first line of defence."""
    _seed_failed(status="publish_unknown")
    _seed_failed(status="scheduled", error=None)
    _seed_failed(status="gated", error=None)

    report = _sweep(apply=True, reason="проверка границы сканирования")

    assert report["scanned"] == 0
    assert report["changed"] == 0
    assert report["parked_unknown"] == 1, "counted, and still not a candidate"


def test_evidence_of_publication_refuses_the_requeue(consented):
    """`failed` is what our classifier decided, not what the platform proved."""
    with_message_id = _seed_failed(tg_message_id=9001)
    with_published_at = _seed_failed(published=True)
    journaled = _seed_failed()
    _journal(journaled, status="posted")
    clean = _seed_failed()
    # A `failed` row whose journal says `skipped`, not `posted`, is still requeueable.
    not_posted = _seed_failed()
    _journal(not_posted, status="skipped")

    report = _sweep(apply=True, reason="причина_operator")

    assert report["eligible"] == 2
    assert report["refusals"] == {
        "already_published": 2,
        "journaled_posted": 1,
    }
    assert _row(with_message_id).status == "failed"
    assert _row(with_published_at).status == "failed"
    assert _row(journaled).status == "failed"
    assert _row(clean).status == "scheduled"
    assert _row(not_posted).status == "scheduled"


def test_driver_death_error_text_is_refused_not_resent(consented):
    """A row marked `failed` whose stored error is a dead driver is still uncertain."""
    uncertain = _seed_failed(error="редактор Дзена: Target page, context or browser has been closed")

    report = _sweep(apply=True, reason="причина")

    assert report["refusals"] == {"outcome_uncertain": 1}
    assert report["changed"] == 0
    assert _row(uncertain).status == "failed"


def test_disabled_platform_is_refused_rather_than_queued(tmp_path, monkeypatch):
    """A requeue onto a paused destination leaves the row in a queue that never drains.

    Worse, it leaves `failed`, the one state the operator can still see on the
    reconciliation board: `scheduled` on a paused platform is invisible there.
    """
    from tgfarm import platforms
    from tgfarm.core import settings as settings_mod

    monkeypatch.setattr(platforms, "is_platform_enabled", lambda _platform: False)
    monkeypatch.setenv(PUBLISH_FAILURE_REQUEUE_APPLY_ENV, "1")
    settings = settings_mod.get_settings()
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path), raising=False)
    (tmp_path / f"tgfarm-{_slug()}.db").write_bytes(b"copy")
    public_id = _seed_failed(attempts=14)

    report = _sweep(apply=True, reason="причина")

    assert report["refusals"] == {"platform_disabled": 1}
    assert report["changed"] == 0
    assert _row(public_id).status == "failed"
    assert _row(public_id).attempts == 14


# ---------------------------------------------------------------- the §80 gates
def test_apply_without_consent_changes_nothing(enabled, tmp_path, monkeypatch):
    monkeypatch.delenv(PUBLISH_FAILURE_REQUEUE_APPLY_ENV, raising=False)
    from tgfarm.core import settings as settings_mod

    settings = settings_mod.get_settings()
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path), raising=False)
    (tmp_path / f"tgfarm-{_slug()}.db").write_bytes(b"copy")
    public_id = _seed_failed()

    report = _sweep(apply=True, reason="причина")

    assert report["consent"] is False
    assert PUBLISH_FAILURE_REQUEUE_APPLY_ENV in report["refused"]
    assert report["changed"] == 0
    assert _row(public_id).status == "failed"


def test_apply_without_a_fresh_backup_copy_changes_nothing(consented, tmp_path):
    public_id = _seed_failed()
    for path in tmp_path.glob("*.db"):
        path.unlink()

    report = _sweep(apply=True, reason="причина")

    assert "backup" in str(report.get("refused", "")).lower()
    assert report["changed"] == 0
    assert _row(public_id).status == "failed"


def test_apply_refuses_to_requeue_stale_material_without_a_reason(consented):
    """Every row this sweep can move was closed out with "content is stale".

    So pressing the button is a claim that the material is news again, and the claim
    has to be attributable. No reason, no write — even with consent and a backup.
    """
    public_id = _seed_failed()

    report = _sweep(apply=True, reason="   ")

    assert report["consent"] is True
    assert "reason" in str(report.get("refused", "")).lower() or "relevant" in report["refused"]
    assert report["changed"] == 0
    assert _row(public_id).status == "failed"
    assert _journal_rows("requeue-failed", public_id) == []


def test_consent_reads_the_same_vocabulary_as_the_other_repairs(monkeypatch):
    """`_truthy_env` is the one consent reader in this package; the flag must agree."""
    from tgfarm.core import settings as settings_mod
    from tgfarm.maintenance import _truthy_env

    for value in ("1", "true", "yes", "on", "apply", "TRUE", "  yes  "):
        monkeypatch.setenv(PUBLISH_FAILURE_REQUEUE_APPLY_ENV, value)
        assert _truthy_env(PUBLISH_FAILURE_REQUEUE_APPLY_ENV) is True, value
    for value in ("", "0", "false", "no", "off"):
        monkeypatch.setenv(PUBLISH_FAILURE_REQUEUE_APPLY_ENV, value)
        assert _truthy_env(PUBLISH_FAILURE_REQUEUE_APPLY_ENV) is False, value

    settings = settings_mod.get_settings()
    assert not hasattr(settings, PUBLISH_FAILURE_REQUEUE_APPLY_ENV.lower()), (
        "the flag became a Settings field: it now needs the P40 value check, and "
        "maintenance.py's own docstring says a one-off consent decision is not a knob"
    )


def test_the_flag_is_documented_for_the_operator():
    """The P53 guard owns the general rule; this one names this flag specifically.

    A consent gate the template never mentions is a gate that stays refused forever,
    which is how `PUBLICATION_EVIDENCE_REPAIR_APPLY` shipped.
    """
    from pathlib import Path

    template = Path(__file__).resolve().parents[1] / ".env.example"
    text = template.read_text(encoding="utf-8")
    assert f"\n{PUBLISH_FAILURE_REQUEUE_APPLY_ENV}=false\n" in text, (
        ".env.example must ship this gate off, as an active line"
    )
    import re

    import test_env_template_matches_settings as template_guard

    assert PUBLISH_FAILURE_REQUEUE_APPLY_ENV in template_guard.DIRECT_ENV_KEYS, (
        "the template documents a key that is not a Settings field, so it must be "
        "declared as read straight from the environment"
    )
    assert re.search(PUBLISH_FAILURE_REQUEUE_APPLY_ENV + r"\s*=\s*true", text) is None


# ------------------------------------------------------------------ the bounded pass
def test_one_pass_moves_at_most_its_budget_and_reports_the_rest(consented):
    for index in range(3):
        _seed_failed(age_hours=1.0 + index)

    report = _sweep(apply=True, limit=2, reason="первый проход")

    assert report["eligible"] == 3
    assert report["limit"] == 2
    assert report["changed"] == 2
    assert report["remaining"] == 1
    statuses = [_row(public_id).status for public_id in _seeded_public_ids]
    assert statuses.count("scheduled") == 2
    assert statuses.count("failed") == 1, "the row the budget could not reach stays put"


def test_the_budget_is_clamped_no_matter_what_the_caller_asks_for(consented):
    """`limit=100000` from a curious operator must not become a 100000-row write."""
    report = _sweep(limit=100_000)

    assert report["requested_limit"] == 100_000
    assert report["limit"] == REQUEUE_BUDGET_MAX
    assert REQUEUE_BUDGET_MAX <= 200


def test_a_pass_takes_the_freshest_material_first(consented):
    """The rows are stale by construction, so the budget goes where relevance is likeliest.

    Oldest-first would spend the whole pass on the material least likely to be news
    and leave the operator judging the rest by the board's own ordering.
    """
    newest = _seed_failed(age_hours=2.0)
    oldest = _seed_failed(age_hours=400.0)

    report = _sweep(apply=True, limit=1, reason="только самое свежее")

    assert report["changed"] == 1
    assert _row(newest).status == "scheduled"
    assert _row(oldest).status == "failed"
    assert report["remaining"] == 1


# ---------------------------------------------------------------- journalled writes
def test_a_moved_row_gets_a_fresh_budget_and_a_journal_entry_that_keeps_the_cause(
    consented,
):
    public_id = _seed_failed(attempts=14, error="net::ERR_NAME_NOT_RESOLVED at dzen.ru")

    report = _sweep(apply=True, reason="днс починили, тема ещё не устарела")

    assert report["changed"] == 1
    assert _row(public_id).status == "scheduled"
    row = _row(public_id)
    assert row.attempts == 0
    assert row.publish_error is None, "the row must not keep a stale failure reason"
    assert row.publish_heartbeat_at is None
    assert _stamp(row.scheduled_at) == NOW, "the clock is injected, so the booking is exact"
    entries = _journal_rows("requeue-failed", public_id)
    assert len(entries) == 1
    detail = entries[0].detail or ""
    assert "failed -> scheduled" in detail
    assert "budget reset from 14 attempts" in detail
    assert "днс починили" in detail
    assert "ERR_NAME_NOT_RESOLVED" in detail, (
        "the row loses its reason on purpose; the journal is the only place left "
        "that says what this post failed with"
    )
    assert PLATFORM in detail


def test_a_row_that_left_failed_between_the_scan_and_the_write_is_not_moved(consented, monkeypatch):
    """The pass reads the rows, then writes them in a second transaction.

    A publisher tick can claim one in between. Re-applying the guard against the
    row as it is at write time is what stops the sweep from stamping `scheduled`
    over a post another process is sending right now — the same duplicate-send
    race `unpublish` documents for its conditional UPDATE.
    """
    from tgfarm import maintenance

    raced = _seed_failed(attempts=4)
    untouched = _seed_failed()
    real_backup = maintenance._fresh_backup_copy

    def backup_that_races() -> dict:
        """Between the preview and the write, the publisher claims the row."""
        with session_scope() as session:
            row = session.execute(
                select(Post).where(Post.public_id == raced)
            ).scalar_one()
            row.status = "publishing"
        return real_backup()

    monkeypatch.setattr(maintenance, "_fresh_backup_copy", backup_that_races)

    report = _sweep(apply=True, reason="площадка снова доступна")

    assert report["changed"] == 1
    assert report["eligible"] == 2, "the scan saw both rows as requeueable"
    assert report["refusals"] == {"not_failed": 1}
    assert _row(raced).status == "publishing"
    assert _row(raced).attempts == 4, "the sweep must not reset a budget it does not own"
    assert _row(untouched).status == "scheduled"
    assert _journal_rows("requeue-failed", raced) == []


def test_the_journal_never_repeats_a_credential_from_the_stored_error(consented):
    public_id = _seed_failed(error="HTTP 401 with access_token=LEAKEDTOKEN987654")

    _sweep(apply=True, reason="причина")

    detail = (_journal_rows("requeue-failed", public_id)[0].detail or "")
    assert "LEAKEDTOKEN987654" not in detail
    assert "access_token" in detail, "the shape survives redaction, the value does not"


# ------------------------------------------------------------- the single-post path
def test_single_post_retry_keeps_working_and_journalises_the_cause(monkeypatch):
    """The pre-existing path, re-asserted after being routed through the shared guard.

    It must still reset the budget, and it must now say in the journal what it
    erased from the row.
    """
    from tgfarm import platforms
    from tgfarm.api import _retry_failed_publish

    monkeypatch.setattr(platforms, "is_platform_enabled", lambda _platform: True)
    public_id = _seed_failed(attempts=9, error="net::ERR_CONNECTION_REFUSED")

    result = _retry_failed_publish(public_id)

    assert result["ok"] is True
    assert result["platform"] == PLATFORM
    assert result["previous_attempts"] == 9
    assert "ERR_CONNECTION_REFUSED" in result["previous_error"]
    row = _row(public_id)
    assert row.status == "scheduled" and row.attempts == 0 and row.publish_error is None


def test_single_post_retry_and_the_pass_refuse_the_same_rows(consented):
    """The point of one shared guard: no second, looser requeue path."""
    from tgfarm.api import _retry_failed_publish

    journaled = _seed_failed()
    _journal(journaled, status="posted")
    uncertain = _seed_failed(error="browser has been closed")
    _journal(uncertain, status="failed")
    unknown = _seed_failed(status="publish_unknown")

    assert _retry_failed_publish(journaled)["error"] == "journaled_posted"
    assert _retry_failed_publish(uncertain)["error"] == "outcome_uncertain"
    assert _retry_failed_publish(unknown)["error"] == "publish_outcome_unknown"
    for public_id in (journaled, uncertain, unknown):
        assert _row(public_id).status != "scheduled"

    report = _sweep(apply=True, reason="та же граница")
    assert report["scanned"] == 2
    assert report["eligible"] == 0
    assert report["changed"] == 0


# ------------------------------------------------------------------------ the route
@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from tgfarm.api import create_app
    from tgfarm.core.apiauth import ensure_token
    from tgfarm.db.bootstrap import ensure_bootstrapped

    ensure_bootstrapped()
    headers = {"X-TGFarm-Token": ensure_token()}
    with TestClient(
        create_app(with_scheduler=False),
        base_url="http://127.0.0.1",
        headers=headers,
    ) as test_client:
        yield test_client


def test_route_previews_on_apply_false(client, enabled, monkeypatch):
    """The route answers "what would a pass do" and writes nothing on the way.

    Both shapes are checked: the scoped call gives the exact numbers this file can
    own, and the unscoped one — what the panel's button actually sends — must stay
    a read even though it now counts other test files' `failed` residue too. That
    residue is why the unscoped half asserts "wrote nothing", not "found N rows".
    """
    monkeypatch.delenv(PUBLISH_FAILURE_REQUEUE_APPLY_ENV, raising=False)
    public_id = _seed_failed(attempts=5)

    scoped = client.post(
        "/api/posts/retry-failed", json={"limit": 10, "platform": PLATFORM}
    ).json()
    assert scoped["apply"] is False and scoped["changed"] == 0
    assert scoped["eligible"] == 1 and scoped["by_platform"] == {PLATFORM: 1}
    assert scoped["refused"] is None
    assert _row(public_id).status == "failed"

    response = client.post("/api/posts/retry-failed", json={"limit": 10})

    assert response.status_code == 200, response.text
    report = response.json()
    assert report["apply"] is False and report["changed"] == 0
    assert report["scanned"] >= 1
    assert _row(public_id).status == "failed", "a preview must not move the row"


def test_route_reports_a_refused_apply_as_a_report_not_an_http_error(client, monkeypatch):
    """The numbers are the product; 409 would throw them away in the UI's error path."""
    from tgfarm import platforms

    monkeypatch.delenv(PUBLISH_FAILURE_REQUEUE_APPLY_ENV, raising=False)
    monkeypatch.setattr(platforms, "is_platform_enabled", lambda _platform: True)
    public_id = _seed_failed()

    response = client.post(
        "/api/posts/retry-failed",
        json={"apply": True, "limit": 10, "platform": PLATFORM, "reason": "без флага"},
    )

    assert response.status_code == 200, response.text
    report = response.json()
    assert PUBLISH_FAILURE_REQUEUE_APPLY_ENV in report["refused"]
    assert report["changed"] == 0
    assert _row(public_id).status == "failed"
    batch = _journal_rows("requeue-failed-batch", PLATFORM)
    assert len(batch) == 1
    assert "REFUSED" in (batch[0].detail or "")


def test_route_refuses_a_single_post_unknown_row_with_a_named_reason(client, enabled):
    public_id = _seed_failed(status="publish_unknown", error="timed out")

    response = client.post(f"/api/posts/{public_id}/retry")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "§80" in detail
    assert _row(public_id).status == "publish_unknown"


# ------------------------------------------------------------------- not automated
def test_the_cycle_never_requeues_by_itself(consented, monkeypatch):
    """`run_maintenance` deliberately does not call the sweep.

    The two consent-gated repairs in maintenance.py only ever make a row more
    honest; a requeue restarts a publication the farm already gave up on, for
    material it already called stale. A scheduled stage must not be able to do
    that because an environment variable happened to be set.
    """
    from tgfarm.maintenance import run_maintenance

    monkeypatch.setenv(PUBLISH_FAILURE_REQUEUE_APPLY_ENV, "1")
    public_id = _seed_failed()

    run_maintenance()

    assert _row(public_id).status == "failed"
    assert _journal_rows("requeue-failed", public_id) == []


def test_nothing_in_this_path_publishes(consented):
    """The write sets `scheduled` and stops; sending is the publisher's own policy.

    Asserted structurally: the sweep's only UPDATE target columns are the queue
    fields, and no publisher entry point is reachable from it.
    """
    import inspect

    from tgfarm import maintenance

    source = inspect.getsource(maintenance.requeue_failed_publishes)
    for forbidden in ("publish_due", "_publish_one", "PUBLISHER", "requests.", "httpx"):
        assert forbidden not in source, f"{forbidden} appeared in the requeue path"
    assert 'fresh.status = "scheduled"' in source
