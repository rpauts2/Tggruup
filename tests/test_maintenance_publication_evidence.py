"""Publications that carry no evidence are reported, and only history rewrites
with the operator's consent.

Live measurement (2026-11-05, read-only): every one of the 15 telegram rows with
`status = 'published'` had `tg_chat_id = -1` and `tg_message_id IS NULL` — the
dry-run fall-through in `_publish_one`. `posts.status` is the denominator of
`format_performance`, `measurement_coverage`, the S12 lessons and the panel cards,
so those rows made a farm that sent nothing look like a farm that has evidence.

The repair follows the `duplicate_of` precedent: preview by default, `--apply`
plus a consent env flag, and a refusal to write at all without a fresh copy in
`backup/`. Nothing here opens the live database: every row under test is seeded
in the throwaway session database the conftest builds.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from tgfarm.db.models import Channel, CrosspostLog, Post
from tgfarm.db.session import session_scope
from tgfarm.maintenance import (
    PUBLICATION_EVIDENCE_APPLY_ENV,
    publication_evidence_repair,
    run_maintenance,
)

APPLY_ENV_VALUES = ("1", "")


def _slug() -> str:
    return uuid4().hex[:8]


def _seed_post(
    *,
    platform: str,
    status: str = "published",
    tg_message_id: int | None = None,
    tg_chat_id: int | None = None,
    generation_provider: str | None = "offline",
    published_minutes_ago: int = 60,
) -> str:
    """One post, on its own channel, with the evidence shape the test asks for."""
    suffix = _slug()
    channel_id = f"pe_{platform}_{suffix}"
    public_id = f"pe{suffix}"
    with session_scope() as session:
        session.add(
            Channel(
                id=channel_id,
                title="Evidence test",
                niche="test",
                platform=platform,
                tg_chat_id=-1001234567890 if platform == "telegram" else None,
            )
        )
        session.add(
            Post(
                public_id=public_id,
                channel_id=channel_id,
                hook="Заголовок",
                body="Тело поста.",
                status=status,
                published_at=(
                    datetime.now(UTC) - timedelta(minutes=published_minutes_ago)
                    if status == "published"
                    else None
                ),
                tg_message_id=tg_message_id,
                tg_chat_id=tg_chat_id,
                generation_provider=generation_provider,
            )
        )
    return public_id


def _journal(public_id: str, platform: str, *, status: str, remote_id: str | None, url: str | None = None) -> None:
    with session_scope() as session:
        session.add(
            CrosspostLog(
                post_public_id=public_id,
                platform=platform,
                status=status,
                remote_id=remote_id,
                url=url,
            )
        )


def _row(public_id: str) -> Post:
    with session_scope() as session:
        return session.execute(
            select(Post).where(Post.public_id == public_id)
        ).scalar_one()


def _consented(tmp_path, monkeypatch, *, backup: bool = True) -> None:
    """Consent flag on, plus a `backup/` copy that is fresh enough to trust."""
    from tgfarm.core import settings as settings_mod

    monkeypatch.setenv(PUBLICATION_EVIDENCE_APPLY_ENV, "1")
    settings = settings_mod.get_settings()
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path), raising=False)
    if backup:
        (tmp_path / f"tgfarm-{_slug()}.db").write_bytes(b"copy")


def test_preview_reports_the_evidence_free_rows_and_writes_nothing(monkeypatch):
    monkeypatch.delenv(PUBLICATION_EVIDENCE_APPLY_ENV, raising=False)
    ghost = _seed_post(platform="telegram", tg_chat_id=-1)
    real = _seed_post(platform="telegram", tg_message_id=4242)

    report = publication_evidence_repair()

    assert report["apply"] is False
    assert report["moved"] == 0
    assert ghost in report["sample"] or report["by_platform"]["telegram"] >= 1
    assert report["by_platform"]["telegram"] == 1, "only the row without a message id"
    assert _row(ghost).status == "published", "предпросмотр не трогает историю"
    assert _row(real).status == "published"
    assert report["placeholder_chat_ids_all"] >= 1


def test_a_real_publication_is_not_a_candidate():
    with_evidence = _seed_post(platform="telegram", tg_message_id=999, tg_chat_id=-100123)

    report = publication_evidence_repair()

    assert with_evidence not in report["sample"]
    assert report["scanned"] == 0 or report["by_platform"]["telegram"] == 0


def test_imported_history_is_never_touched():
    """Archive rows are *told* to be published and own no farm-side id by design."""
    imported = _seed_post(platform="telegram", generation_provider="history_import")

    report = publication_evidence_repair()

    assert report["by_platform"]["telegram"] == 0
    assert imported not in report["sample"]
    assert report["imported_history_excluded"] >= 1


def test_dzen_publication_without_an_id_keeps_its_status():
    """The half of the decision that says "do not destroy genuine publications".

    Live database: 37 dzen `posted` rows, 8 with an id, 29 without. The journal
    row is the evidence — the browser publisher only writes `posted` once Dzen
    accepted the article — so a missing *identifier* is a lost attribution key,
    not an un-published post.
    """
    posted_no_id = _seed_post(platform="dzen")
    _journal(posted_no_id, "dzen", status="posted", remote_id=None, url=None)

    report = publication_evidence_repair()

    assert report["by_platform"]["dzen"] == 0
    assert report["posted_without_id"] >= 1
    assert _row(posted_no_id).status == "published"


def test_dry_run_journal_is_not_evidence():
    """A `dry_run` row says the opposite of `posted`: the post is a candidate."""
    dry = _seed_post(platform="dzen")
    _journal(dry, "dzen", status="dry_run", remote_id=f"dry-{_slug()}")

    report = publication_evidence_repair()

    assert report["by_platform"]["dzen"] == 1
    assert dry in report["sample"]


def test_apply_refuses_without_the_consent_flag(tmp_path, monkeypatch):
    monkeypatch.delenv(PUBLICATION_EVIDENCE_APPLY_ENV, raising=False)
    ghost = _seed_post(platform="telegram", tg_chat_id=-1)

    report = publication_evidence_repair(apply=True)

    assert report["consent"] is False
    assert report["moved"] == 0
    assert "refused" in report
    assert _row(ghost).status == "published", "без согласия история не переписывается (§80)"


def test_apply_refuses_without_a_fresh_backup_copy(tmp_path, monkeypatch):
    _consented(tmp_path, monkeypatch, backup=False)
    ghost = _seed_post(platform="telegram", tg_chat_id=-1)

    report = publication_evidence_repair(apply=True)

    assert report["moved"] == 0
    assert "backup" in str(report.get("refused", "")).lower()
    assert _row(ghost).status == "published"


def test_apply_moves_the_row_to_the_state_that_admits_the_uncertainty(tmp_path, monkeypatch):
    """`publish_unknown`, and the invented values go with the invented status."""
    _consented(tmp_path, monkeypatch)
    ghost = _seed_post(platform="telegram", tg_chat_id=-1)

    report = publication_evidence_repair(apply=True)

    assert report["moved"] == 1
    assert report["cleared_chat_ids"] == 1
    row = _row(ghost)
    assert row.status == "publish_unknown"
    assert row.published_at is None
    assert row.tg_chat_id is None, "`-1` was never an observed chat"
    assert "no remote evidence" in (row.publish_error or "")


def test_apply_is_bounded_and_reports_the_remainder(tmp_path, monkeypatch):
    _consented(tmp_path, monkeypatch)
    ghosts = [_seed_post(platform="telegram", tg_chat_id=-1) for _ in range(3)]

    report = publication_evidence_repair(apply=True, limit=2)

    assert report["moved"] <= 2
    assert report["remaining"] == len(ghosts) - report["moved"]
    still_published = sum(1 for public_id in ghosts if _row(public_id).status == "published")
    assert still_published == report["remaining"]


def test_the_step_is_registered_and_reports_zero_without_consent(monkeypatch):
    monkeypatch.delenv(PUBLICATION_EVIDENCE_APPLY_ENV, raising=False)
    assert os.environ.get(PUBLICATION_EVIDENCE_APPLY_ENV) is None

    report = run_maintenance()

    assert "publication_evidence_repair" in report
    assert report["publication_evidence_repair"] == 0


def test_the_maintenance_hook_writes_only_under_consent(tmp_path, monkeypatch):
    _consented(tmp_path, monkeypatch)
    ghost = _seed_post(platform="telegram", tg_chat_id=-1)

    report = run_maintenance()

    assert report["publication_evidence_repair"] >= 1
    assert _row(ghost).status == "publish_unknown"


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "apply"])
def test_consent_vocabulary_is_explicit(monkeypatch, value):
    monkeypatch.setenv(PUBLICATION_EVIDENCE_APPLY_ENV, value)
    from tgfarm.maintenance import _truthy_env

    assert _truthy_env(PUBLICATION_EVIDENCE_APPLY_ENV) is True


@pytest.mark.parametrize("value", ["", "0", "no", "false", "off"])
def test_anything_else_is_not_consent(monkeypatch, value):
    monkeypatch.setenv(PUBLICATION_EVIDENCE_APPLY_ENV, value)
    from tgfarm.maintenance import _truthy_env

    assert _truthy_env(PUBLICATION_EVIDENCE_APPLY_ENV) is False


def test_report_counts_are_exact_across_platforms():
    _seed_post(platform="telegram", tg_chat_id=-1)
    _seed_post(platform="vk")
    _seed_post(platform="dzen")
    _journal(_seed_post(platform="vk"), "vk", status="posted", remote_id="123_456", url="https://vk.com/wall-123_456")

    report = publication_evidence_repair()

    assert report["by_platform"] == {"telegram": 1, "dzen": 1, "vk": 1}
    assert report["scanned"] == 3
