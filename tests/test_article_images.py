"""Lead-photo selection and Dzen body splitting."""

from __future__ import annotations

import io
from types import SimpleNamespace
from uuid import uuid4
from urllib.parse import urlparse

import pytest
from PIL import Image

from tgfarm.dzen_web import DzenWebPublisher, clipboard_png, split_body_for_image
from tgfarm.s6_visual.article_images import (
    MIN_WIDTH,
    image_candidates,
    normalise_image,
    save_image,
)


def jpeg_bytes(width: int, height: int, colour: tuple[int, int, int] = (20, 80, 120)) -> bytes:
    image = Image.new("RGB", (width, height), colour)
    buffer = io.BytesIO()
    image.save(buffer, "JPEG")
    return buffer.getvalue()


def test_image_candidates_keep_only_feed_images():
    item = SimpleNamespace(
        media_links=[
            "https://a.invalid/lead.jpg",
            "https://a.invalid/clip.mp4",
            "https://a.invalid/lead.jpg",
        ]
    )
    assert [candidate.url for candidate in image_candidates(item)] == [
        "https://a.invalid/lead.jpg"
    ]


def test_extensionless_cdn_image_is_trusted_when_collection_says_image():
    item = SimpleNamespace(
        media_kind="image",
        media_links=[
            "https://assets.invalid/img/abc=/1200x630/smart/lead",
            "https://assets.invalid/clip.mp4",
        ],
    )
    assert [candidate.url for candidate in image_candidates(item)] == [
        "https://assets.invalid/img/abc=/1200x630/smart/lead"
    ]


def test_html_escaped_query_string_is_decoded_for_the_cdn():
    item = SimpleNamespace(
        media_kind="image",
        media_links=[
            "https://cdn.invalid/lead.jpg?quality=90&#038;strip=all&#038;crop=0,0"
        ],
    )
    assert [candidate.url for candidate in image_candidates(item)] == [
        "https://cdn.invalid/lead.jpg?quality=90&strip=all&crop=0,0"
    ]


def test_phys_thumbnail_gets_large_siblings_first():
    thumb = "https://scx1.invalid/csz/news/tmb/2025/lead.jpg"
    item = SimpleNamespace(media_kind="image", media_links=[thumb])
    assert [candidate.url for candidate in image_candidates(item)] == [
        thumb.replace("/tmb/", "/800/"),
        thumb.replace("/tmb/", "/800a/"),
        thumb,
    ]


def test_tiny_or_extreme_image_is_rejected():
    assert normalise_image(jpeg_bytes(MIN_WIDTH - 1, 400)) is None
    assert normalise_image(jpeg_bytes(1000, 100)) is None


def test_lead_photo_is_normalised_and_saved_under_media_root():
    asset = save_image(jpeg_bytes(1200, 700), "https://publisher.invalid/story")
    assert asset is not None
    assert asset.kind == "article_image"
    assert asset.width <= 1600 and asset.height <= 1600
    assert asset.path is not None
    assert asset.path.endswith(".jpg")
    assert urlparse("https://publisher.invalid/story").hostname == "publisher.invalid"
    assert asset.caption == f"Фото: {urlparse('https://publisher.invalid/story').hostname}"
    with Image.open(asset.path) as saved:
        assert saved.format == "JPEG"


def test_jpeg_is_reencoded_to_png_for_the_clipboard():
    payload = clipboard_png(jpeg_bytes(640, 360))
    assert payload.startswith(b"\x89PNG\r\n\x1a\n")
    with Image.open(io.BytesIO(payload)) as image:
        assert image.format == "PNG"
        assert image.size == (640, 360)


def test_split_body_keeps_every_paragraph_and_puts_photo_after_the_intro():
    body = "Первый абзац.\n\nВторой абзац.\n\nТретий абзац.\n\nИсточник: example"
    first, rest = split_body_for_image(body)
    assert first == "Первый абзац.\n\nВторой абзац."
    assert rest == "Третий абзац.\n\nИсточник: example"
    assert first + "\n\n" + rest == body


def test_split_body_handles_single_paragraph_and_empty_body():
    assert split_body_for_image("Только один абзац.") == ("Только один абзац.", "")
    assert split_body_for_image("") == ("", "")


@pytest.mark.asyncio
async def test_caption_is_filled_after_the_rest_of_the_body(tmp_path):
    publisher = DzenWebPublisher(profile_dir=tmp_path, headless=True)
    events = []

    async def type_intro(field, text, clear=True):
        events.append(("type", text))

    async def type_rest(text):
        events.append(("type_rest", text))

    async def pause(low=0.0, high=0.0):
        return None

    publisher._human = SimpleNamespace(
        type=type_intro,
        type_text=type_rest,
        pause=pause,
    )

    async def find_body(page):
        return "body"

    async def paste(page, image):
        events.append(("paste", image.name))

    async def caption(page, text):
        events.append(("caption", text))

    class Keyboard:
        async def press(self, key):
            events.append(("key", key))

    publisher._find_body = find_body
    publisher._paste_image = paste
    publisher._fill_image_caption = caption
    page = SimpleNamespace(keyboard=Keyboard())

    await publisher._fill_body_with_image(
        page,
        "Первый.\n\nВторой.\n\nТретий.",
        tmp_path / "lead.jpg",
        "Фото: example.invalid",
    )

    assert events == [
        ("type", "Первый.\n\nВторой."),
        ("key", "Enter"),
        ("paste", "lead.jpg"),
        ("key", "Control+End"),
        ("key", "Enter"),
        ("type_rest", "Третий."),
        ("caption", "Фото: example.invalid"),
    ]


def test_loaded_dzen_image_jobs_keep_channel_id_for_generation_fallback(monkeypatch):
    from tgfarm.db.models import Channel, Cluster, Post, RawItem
    from tgfarm.db.session import session_scope
    import tgfarm.s6_visual.article_images as article_images

    suffix = uuid4().hex[:10]
    channel_id = f"dzen_img_{suffix}"
    cluster_public_id = f"cluster_{suffix}"
    post_public_id = f"post_{suffix}"

    with session_scope() as session:
        session.add(Channel(
            id=channel_id,
            platform="dzen",
            title="Dzen image regression",
            niche="general",
            enabled=True,
        ))
        session.flush()
        cluster = Cluster(
            public_id=cluster_public_id,
            label="Dzen image regression",
            niche="general",
            status="ready",
        )
        session.add(cluster)
        session.flush()
        session.add(Post(
            public_id=post_public_id,
            channel_id=channel_id,
            cluster_public_id=cluster_public_id,
            status="scheduled",
            hook="A news item",
            body="An article body",
        ))
        session.add(RawItem(
            source_id=f"source_{suffix}",
            external_id=f"external_{suffix}",
            url="https://example.invalid/story",
            title="A news item",
            text="Article source text",
            cluster_id=cluster.id,
        ))

    class Config:
        def persona(self, _channel_id):
            return SimpleNamespace(platform="dzen", enabled=True)

    monkeypatch.setattr(article_images, "get_config", lambda: Config())
    posts, _items = article_images._load_posts([post_public_id])

    assert len(posts) == 1
    assert posts[0]["public_id"] == post_public_id
    assert posts[0]["channel_id"] == channel_id
