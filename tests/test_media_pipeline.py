"""Media recorded at collection time, and additive schema migrations."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy import inspect

from tgfarm.core.media import discover_from_feed, discover_in_html, dominant_kind, extension_of
from tgfarm.core.schemas import ItemScored, RawItem
from tgfarm.db.bootstrap import apply_additive_migrations, init_db, plan_additive_migrations
from tgfarm.db.models import RawItem as RawItemRow
from tgfarm.db.session import get_engine, session_scope
from tgfarm.s1_sources.base import to_raw


def spec(**kw):
    from tgfarm.config import SourceSpec

    return SourceSpec(id="s1", name="Test", kind="rss", url="https://x.invalid/f.xml", **kw)


# --------------------------------------------------------------------- parsing
def test_extension_of_handles_query_strings():
    assert extension_of("https://a.invalid/c.mp4?token=1") == ".mp4"
    assert extension_of("https://a.invalid/c") == ""
    assert extension_of("https://a.invalid/story.this-is-not-an-extension") == ""
    # a plausible-looking but unsupported extension is still an extension
    assert extension_of("https://a.invalid/clip.exe") == ".exe"


def test_feed_entry_without_media_is_cheap():
    entry = {"title": "plain", "summary": "no media here"}
    assert discover_from_feed(entry) == []
    assert dominant_kind([]) is None


def test_feed_entry_with_video_only():
    entry = {"enclosures": [{"type": "video/mp4", "href": "https://a.invalid/v.mp4"}]}
    assert dominant_kind(discover_from_feed(entry)) == "video"


def test_video_wins_over_image_for_layout():
    entry = {
        "enclosures": [
            {"type": "image/jpeg", "href": "https://a.invalid/p.jpg"},
            {"type": "video/mp4", "href": "https://a.invalid/v.mp4"},
        ]
    }
    assert dominant_kind(discover_from_feed(entry)) == "video"


def test_media_content_namespace_is_read():
    entry = {"media_content": [{"url": "https://a.invalid/v.webm", "type": "video/webm"}]}
    assert dominant_kind(discover_from_feed(entry)) == "video"


def test_media_content_without_mime_is_classified_by_extension():
    entry = {"media_content": [{"url": "https://a.invalid/clip.mov"}]}
    kinds = {c.kind for c in discover_from_feed(entry)}
    assert "video" in kinds


def test_an_img_tag_is_never_a_video():
    """Habr entries carry a cover `<img>`; calling it a video made the farm try
    to download a PNG as footage."""
    html = '<img src="https://a.invalid/cover.png" width="800">'
    assert [c.kind for c in discover_in_html(html)] == ["image"]


def test_video_tag_outranks_a_cover_image():
    html = (
        '<img src="https://a.invalid/cover.png">'
        '<video src="https://a.invalid/clip.mp4" controls></video>'
    )
    assert dominant_kind(discover_in_html(html)) == "video"


def test_og_video_tag_wins():
    html = '<meta property="og:video" content="https://a.invalid/clip.webm">'
    assert [c.kind for c in discover_in_html(html)] == ["video"]


def test_unknown_extension_in_html_is_dropped():
    """A bare URL with no media extension is not guessed at: a false `video`
    costs an article fetch and a download attempt."""
    html = '<source src="https://a.invalid/player/config.json">'
    assert discover_in_html(html) == []


def test_og_image_relative_url_is_resolved_against_page():
    html = '<meta property="og:image" content="/images/lead.jpg">'
    found = discover_in_html(html, "https://news.invalid/story/1")
    assert [c.url for c in found] == ["https://news.invalid/images/lead.jpg"]


def test_string_link_without_type_uses_extension():
    assert dominant_kind(discover_from_feed({"links": ["https://a.invalid/v.mp4"]})) == "video"
    assert dominant_kind(discover_from_feed({"links": ["https://a.invalid/p.jpg"]})) == "image"
    assert discover_from_feed({"links": ["https://a.invalid/page"]}) == []


def test_typed_image_enclosure_is_not_a_video():
    entry = {"enclosures": [{"type": "image/jpeg", "href": "https://a.invalid/p.jpg"}]}
    assert dominant_kind(discover_from_feed(entry)) == "image"


def test_non_http_urls_are_ignored():
    entry = {"enclosures": [{"type": "video/mp4", "href": "data:video/mp4;base64,AAAA"}]}
    assert discover_from_feed(entry) == []


# ------------------------------------------------------------------ collection
def test_to_raw_records_feed_media():
    raw = to_raw(
        spec(),
        title="Заголовок материала",
        url="https://a.invalid/1",
        media_links=["https://a.invalid/v.mp4"],
        media_kind="video",
    )
    assert raw is not None
    assert raw.media_links == ["https://a.invalid/v.mp4"]
    assert raw.media_kind == "video"


def test_to_raw_defaults_to_no_media():
    raw = to_raw(spec(), title="Заголовок материала", url="https://a.invalid/1")
    assert raw is not None
    assert raw.media_links == []
    assert raw.media_kind is None


def test_raw_item_rejects_unknown_fields():
    """`extra="forbid"` is what keeps a typo like `media_link` from silently
    dropping the field the video stage reads."""
    with pytest.raises(ValidationError):
        RawItem(
            source_id="s",
            external_id="e",
            url="https://a.invalid/1",
            title="Заголовок",
            media_link="https://a.invalid/v.mp4",
        )


def test_media_survives_the_round_trip_to_the_database():
    item = ItemScored(
        source_id="s1",
        external_id="ext-1",
        url="https://a.invalid/1",
        title="Материал с видео",
        media_links=["https://a.invalid/v.mp4"],
        media_kind="video",
    )
    with session_scope() as session:
        session.add(
            RawItemRow(
                source_id=item.source_id,
                external_id=item.external_id,
                url=item.url,
                title=item.title,
                text=item.text,
                lang=item.lang,
                niche_hint=item.niche_hint,
                source_weight=item.source_weight,
                media_links=item.media_links,
                media_kind=item.media_kind,
            )
        )
    with session_scope() as session:
        row = session.query(RawItemRow).filter_by(external_id="ext-1").one()
        assert row.media_kind == "video"
        assert row.media_links == ["https://a.invalid/v.mp4"]


def test_s11_accepts_a_bare_orm_row():
    """fetch_video_for is called with an ORM row, not the pydantic model, so it
    must not assume pydantic attribute behaviour."""
    with session_scope() as session:
        row = session.query(RawItemRow).filter_by(external_id="ext-1").one()
        assert getattr(row, "media_links", None) == ["https://a.invalid/v.mp4"]
        assert getattr(row, "text", "") == ""


# ------------------------------------------------------------------ migrations
def test_migration_is_idempotent():
    engine = get_engine()
    assert apply_additive_migrations(engine) == 0
    assert apply_additive_migrations(engine) == 0


def test_media_columns_exist_in_the_live_schema():
    init_db()
    columns = {col["name"] for col in inspect(get_engine()).get_columns("raw_items")}
    assert {"media_links", "media_kind"} <= columns


def test_migration_adds_a_missing_column():
    """Simulates an existing deployment created before the column existed."""
    engine = get_engine()
    with engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE raw_items DROP COLUMN media_kind")
    assert "media_kind" not in {
        col["name"] for col in inspect(engine).get_columns("raw_items")
    }

    assert apply_additive_migrations(engine) == 1
    assert "media_kind" in {col["name"] for col in inspect(engine).get_columns("raw_items")}
    assert apply_additive_migrations(engine) == 0


def test_the_plan_predicts_what_the_migration_will_run():
    """A preview that disagrees with the boot is worse than no preview.

    The restart decision rests on `plan_additive_migrations`, so the drift has to
    show up in the plan and then be repaired by exactly that plan.
    """
    engine = get_engine()
    with engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE raw_items DROP COLUMN media_kind")

    plan = plan_additive_migrations(engine)
    column_statements = [s for s in plan if s.startswith("ALTER TABLE")]
    assert any('ADD COLUMN "media_kind"' in s for s in column_statements)

    assert apply_additive_migrations(engine) == len(column_statements)
    assert plan_additive_migrations(engine) == []


def test_a_missing_index_is_planned_and_applied_without_counting_as_a_column():
    engine = get_engine()
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP INDEX IF EXISTS ix_clusters_trend_phase")

    plan = plan_additive_migrations(engine)
    assert any("ix_clusters_trend_phase" in s for s in plan)

    assert apply_additive_migrations(engine) == 0
    assert "ix_clusters_trend_phase" in {
        ix["name"] for ix in inspect(engine).get_indexes("clusters")
    }


def test_post_format_columns_are_migrated():
    columns = {col["name"] for col in inspect(get_engine()).get_columns("posts")}
    assert {"post_format", "format_reason"} <= columns