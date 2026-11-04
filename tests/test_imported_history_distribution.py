"""Archive imports must never look like fresh material worth cross-posting (P19).

`history_import` stores imported posts with ``status=published`` so analytics can
read them. The automatic distribution selectors filter on that same status, so an
imported archive post was a normal cross-post candidate — old content arriving on
live accounts as if it had just been written.

The other half of the property matters just as much: the schema angle ``history``
is an ordinary editorial angle ("как пришли к текущему состоянию") carried by 992
real posts here, so exclusion keys on the import markers only.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from tgfarm.db.models import Channel, Post
from tgfarm.db.session import session_scope


@pytest.fixture
def own_rows():
    """Channels and posts created by this module, removed again afterwards."""
    created: dict[str, list[str]] = {"channels": [], "public_ids": []}

    yield created

    with session_scope() as session:
        session.execute(
            Post.__table__.delete().where(Post.public_id.in_(created["public_ids"]))
        )
        session.execute(Channel.__table__.delete().where(Channel.id.in_(created["channels"])))


def _seed(own_rows, *, platform="telegram", post_format="news", provider="offline", angle="news",
          status="published", age_minutes=5, enabled=True, null_provider=False):
    suffix = uuid.uuid4().hex[:8]
    channel_id = f"imp_{platform}_{suffix}"
    public_id = f"imp{suffix}"
    with session_scope() as session:
        session.add(Channel(
            id=channel_id, title=f"import test {suffix}", niche="tech",
            platform=platform, enabled=enabled,
        ))
        session.flush()
        session.add(Post(
            public_id=public_id,
            channel_id=channel_id,
            hook="Тестовый заголовок",
            body="Тестовый текст.",
            angle=angle,
            post_format=post_format,
            generation_provider=(None if null_provider else provider),
            status=status,
            published_at=datetime.now(UTC) - timedelta(minutes=age_minutes),
        ))
    own_rows["channels"].append(channel_id)
    own_rows["public_ids"].append(public_id)
    return public_id


def _candidate_ids(cutoff_hours=6) -> set[str]:
    from tgfarm.distribution import published_candidates

    with session_scope() as session:
        rows = published_candidates(
            session, datetime.now(UTC) - timedelta(hours=cutoff_hours)
        )
    return {post.public_id for post, _channel in rows}


def test_the_plan_candidate_read_skips_imported_history(own_rows):
    from tgfarm.distribution import published_candidates  # noqa: F401 - the seam under test

    fresh = _seed(own_rows)
    imported = _seed(own_rows, post_format="history", provider="history_import")

    ids = _candidate_ids()

    assert fresh in ids
    assert imported not in ids


def test_a_post_written_with_the_history_angle_is_still_distributable(own_rows):
    """The exclusion may not touch real content that merely has this editorial angle."""
    public_id = _seed(own_rows, angle="history", post_format="news", provider="offline")

    assert public_id in _candidate_ids()


def test_imported_history_is_excluded_by_either_marker(own_rows):
    """The importer writes both markers; a row carrying only one is still archive."""
    by_format = _seed(own_rows, post_format="history")
    by_provider = _seed(own_rows, provider="history_import")

    ids = _candidate_ids()

    assert by_format not in ids
    assert by_provider not in ids


def test_a_missing_provider_does_not_drop_a_real_candidate(own_rows):
    """`generation_provider` is nullable; SQL would fail this row open on NULL.

    A post whose provider was never recorded must stay distributable: the
    exclusion is about archive imports, not about rows with a sparse field.
    """
    public_id = _seed(own_rows, null_provider=True)

    assert public_id in _candidate_ids()


def test_the_legacy_s9_candidate_reader_applies_the_same_exclusion(own_rows):
    """`_pending_crossposts` reads its own query, so it needs the same guard."""
    from tgfarm.s9_growth import _pending_crossposts

    fresh = _seed(own_rows, platform="telegram", age_minutes=60)
    imported = _seed(own_rows, platform="telegram", age_minutes=60,
                     post_format="history", provider="history_import")

    found = {public_id for _row_id, public_id in _pending_crossposts(1.0, window_minutes=720)}

    assert fresh in found
    assert imported not in found


def test_predicate_keys_on_provenance_and_not_the_angle():
    from tgfarm.distribution import exclude_imported_history

    sql = str(
        select(Post.id)
        .where(*exclude_imported_history())
        .compile(compile_kwargs={"literal_binds": True})
    )

    assert "post_format" in sql
    assert "history_import" in sql
    assert "posts.angle" not in sql
