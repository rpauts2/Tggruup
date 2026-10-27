"""S6 — visual rendering.

Deliberately template-first. Templates give a recognisable, consistent channel
identity that image models do not, cost nothing, and render in milliseconds. The
optional `ai_art` path exists for the ~15% of posts where a template genuinely
cannot carry the message; it is off unless IMAGE_ENABLED=true.

All output is 1280x720 PNG — the aspect Telegram renders best in link previews
and feed cards.
"""

from __future__ import annotations

import hashlib
import textwrap
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from tgfarm.config import Persona
from tgfarm.core.logging import get_logger
from tgfarm.core.schemas import DraftPost, MediaAsset
from tgfarm.core.settings import get_settings
from tgfarm.core.text import first_sentence

log = get_logger(__name__)

WIDTH, HEIGHT = 1280, 720
SAFE_MARGIN = 72

FONT_CANDIDATES_BOLD = [
    "C:/Windows/Fonts/segoeuib.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/calibrib.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]
FONT_CANDIDATES_REGULAR = [
    "C:/Windows/Fonts/segoeui.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/calibri.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
]

DEFAULT_PALETTE = {
    "bg": "#0B1020",
    "fg": "#E8ECF8",
    "accent": "#4C8DFF",
    "muted": "#6B7699",
}

_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


@dataclass
class VisualSpec:
    kind: str = "template"
    kicker: str = ""
    headline: str = ""
    subline: str = ""
    footer: str = ""
    big_number: str | None = None
    palette: dict[str, str] | None = None


def _load_font(bold: bool, size: int) -> ImageFont.FreeTypeFont:
    key = ("bold" if bold else "regular", size)
    if key in _font_cache:
        return _font_cache[key]

    settings = get_settings()
    candidates = [settings.font_path] if settings.font_path else []
    candidates += FONT_CANDIDATES_BOLD if bold else FONT_CANDIDATES_REGULAR

    for path in candidates:
        if path and Path(path).exists():
            try:
                font = ImageFont.truetype(path, size)
                _font_cache[key] = font
                return font
            except OSError:
                continue

    fallback = ImageFont.load_default()
    _font_cache[key] = fallback  # type: ignore[assignment]
    log.warning("no truetype font found for %s — using PIL default", key)
    return fallback  # type: ignore[return-value]


def _hex(value: str, fallback: str) -> tuple[int, int, int]:
    value = (value or "").strip() or fallback
    value = value.lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    try:
        return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return (240, 240, 240)


def _fit_text(draw: ImageDraw.ImageDraw, text: str, bold: bool, max_size: int, min_size: int,
              max_width: int, max_lines: int) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    """Shrink the headline until it fits the box. Binary search keeps this at
    ~5 font loads instead of dozens."""
    size = max_size
    while size >= min_size:
        font = _load_font(bold, size)
        lines: list[str] = []
        for paragraph in text.split("\n"):
            wrapped = textwrap.wrap(paragraph, width=max(18, int(max_width / (size * 0.52)))) or [""]
            lines.extend(wrapped)
        if len(lines) <= max_lines:
            bbox = draw.textbbox((0, 0), " ".join(lines), font=font)
            if bbox[2] - bbox[0] <= max_width:
                return font, lines
        size -= 3
    font = _load_font(bold, min_size)
    wrapped: list[str] = []
    for paragraph in text.split("\n"):
        wrapped.extend(
            textwrap.wrap(paragraph, width=max(14, int(max_width / (min_size * 0.52)))) or [""]
        )
    return font, wrapped[:max_lines]


def _background(size: tuple[int, int], bg: tuple[int, int, int], accent: tuple[int, int, int]) -> Image.Image:
    """Vertical gradient plus two soft accent blobs — enough depth to avoid the
    flat-template look without any image asset."""
    width, height = size
    base = Image.new("RGB", (1, height))
    pixels = base.load()
    assert pixels is not None
    for y in range(height):
        t = y / max(1, height - 1)
        pixels[0, y] = tuple(
            int(bg[i] * (1 - 0.35 * t) + accent[i] * 0.35 * (t**2)) for i in range(3)
        )
    image = base.resize(size, Image.BILINEAR)

    glow = Image.new("RGB", size, (0, 0, 0))
    gdraw = ImageDraw.Draw(glow)
    gdraw.ellipse((size[0] * 0.55, -size[1] * 0.35, size[0] * 1.35, size[1] * 0.55), fill=accent)
    gdraw.ellipse((-size[1] * 0.3, size[1] * 0.6, size[1] * 0.7, size[1] * 1.5), fill=accent)
    glow = glow.filter(ImageFilter.GaussianBlur(radius=140))
    return Image.blend(image, glow, 0.16)


def render(spec: VisualSpec, persona: Persona | None = None) -> MediaAsset:
    palette = {**DEFAULT_PALETTE, **(spec.palette or (persona.palette if persona else {}))}
    bg = _hex(palette.get("bg"), DEFAULT_PALETTE["bg"])
    fg = _hex(palette.get("fg"), DEFAULT_PALETTE["fg"])
    accent = _hex(palette.get("accent"), DEFAULT_PALETTE["accent"])
    muted = _hex(palette.get("muted"), DEFAULT_PALETTE["muted"])

    image = _background((WIDTH, HEIGHT), bg, accent)
    draw = ImageDraw.Draw(image)

    # accent bar
    draw.rounded_rectangle(
        (SAFE_MARGIN, 96, SAFE_MARGIN + 96, 104), radius=4, fill=accent
    )

    cursor_y = 132
    if spec.kicker:
        kicker_font = _load_font(True, 26)
        draw.text((SAFE_MARGIN, cursor_y), spec.kicker.upper(), font=kicker_font, fill=accent)
        cursor_y += 52

    if spec.big_number:
        number_font = _load_font(True, 170)
        number, _, caption = spec.big_number.partition(" ")
        draw.text((SAFE_MARGIN, cursor_y + 8), number.strip(), font=number_font, fill=accent)
        cursor_y += 205

        sub_font = _load_font(False, 40)
        caption_lines = textwrap.wrap(caption, width=26)[:2]
        for i, line in enumerate(caption_lines):
            draw.text((SAFE_MARGIN, cursor_y + i * 52), line, font=sub_font, fill=fg)
        cursor_y += len(caption_lines) * 52 + 20

        # the note is optional dressing — draw it only if it fits above the footer
        note_font = _load_font(False, 28)
        if spec.subline and spec.subline != caption:
            note_lines = textwrap.wrap(spec.subline, width=56)
            room = (HEIGHT - 132) - cursor_y
            max_lines = max(0, room // 38)
            for i, line in enumerate(note_lines[:max_lines]):
                draw.text((SAFE_MARGIN, cursor_y + i * 38), line, font=note_font, fill=muted)
    else:
        headline_font, lines = _fit_text(
            draw, spec.headline, True, 74, 34, WIDTH - SAFE_MARGIN * 2, 4
        )
        line_height = headline_font.size + 14
        for line in lines:
            draw.text((SAFE_MARGIN, cursor_y), line, font=headline_font, fill=fg)
            cursor_y += line_height
        cursor_y += 18

        if spec.subline:
            sub_font = _load_font(False, 34)
            for line in textwrap.wrap(spec.subline, width=44)[:3]:
                draw.text((SAFE_MARGIN, cursor_y), line, font=sub_font, fill=muted)
                cursor_y += 48

    # footer: channel identity
    footer_font = _load_font(True, 24)
    footer = spec.footer or (persona.title if persona else "")
    draw.text((SAFE_MARGIN, HEIGHT - 96), footer, font=footer_font, fill=muted)

    draw.line(
        (SAFE_MARGIN, HEIGHT - 128, WIDTH - SAFE_MARGIN, HEIGHT - 128), fill=muted, width=1
    )

    settings = get_settings()
    root = settings.media_root()
    digest = hashlib.sha1(
        f"{spec.kind}|{spec.headline}|{spec.subline}|{palette}|{footer}".encode()
    ).hexdigest()[:12]
    path = root / f"{digest}.png"
    image.save(path, "PNG", optimize=True)

    return MediaAsset(
        kind=spec.kind,
        path=str(path),
        width=WIDTH,
        height=HEIGHT,
        sha=hashlib.sha1(path.read_bytes()).hexdigest(),
        caption=spec.headline[:200],
    )


# ------------------------------------------------------------------ extraction
def _extract_big_number(text: str) -> str | None:
    """Promote a headline to a number-card only when the headline *is* a figure.

    Naively grabbing the first digit run shreds prose headlines: "$35 million and
    lifetime ban" becomes a giant "35" with the sentence truncated to gibberish.
    The remainder after removing the number has to be short enough to read as a
    caption, otherwise the text layout is the honest one.
    """
    import re

    matches = list(re.finditer(r"\d[\d\s.,]*", text))
    if not matches:
        return None
    if len(matches) > 2:  # a headline full of numbers is prose, not a stat card
        return None

    best = max(matches, key=lambda m: len(m.group().strip().replace(" ", "")))
    number = best.group().strip()
    digits = re.sub(r"[^\d]", "", number)
    if len(digits) < 2 or len(digits) > 8:
        return None

    remainder = (text[: best.start()] + " " + text[best.end() :]).strip()
    remainder = re.sub(r"\s+", " ", remainder).strip(" ,.;:—-")
    if len(remainder) > 28 or len(remainder) < 6:
        return None

    return f"{number} {remainder}"


def spec_for_post(draft: DraftPost, persona: Persona) -> VisualSpec:
    from tgfarm.core.text import first_sentence, truncate

    headline = truncate(first_sentence(draft.hook or draft.body, 140), 150)
    subline = truncate(first_sentence(draft.body, 180), 180)
    kicker = persona.niche.upper()

    big = _extract_big_number(headline)
    kind = "infographic" if big else "template"

    return VisualSpec(
        kind=kind,
        kicker=kicker,
        headline=headline,
        subline=subline or headline,
        big_number=big,
        footer=persona.title,
        palette=persona.palette,
    )


async def ai_art(draft: DraftPost, persona: Persona) -> MediaAsset | None:
    """Optional image-model path. Off unless IMAGE_ENABLED=true and configured."""
    from tgfarm.llm import images

    generated = await images.generate_cover(draft.hook, persona.title, persona.niche, persona.palette)
    if generated is None:
        return None

    digest = generated.sha or hashlib.sha1(generated.path.read_bytes()).hexdigest()[:12]
    return MediaAsset(
        kind="ai_art",
        path=str(generated.path),
        width=generated.width or WIDTH,
        height=generated.height or HEIGHT,
        sha=digest,
        caption=draft.hook[:200],
    )


def render_for_post(draft: DraftPost, persona: Persona) -> MediaAsset:
    return render(spec_for_post(draft, persona), persona)


def persist_media(post_public_id: str, asset: MediaAsset, is_primary: bool = True) -> None:
    from sqlalchemy import select

    from tgfarm.db.models import Media as MediaRow
    from tgfarm.db.models import Post as PostRow
    from tgfarm.db.session import session_scope

    if asset.path is None:
        return
    with session_scope() as session:
        post = session.execute(
            select(PostRow).where(PostRow.public_id == post_public_id)
        ).scalar_one_or_none()
        if post is None:
            return

        # Re-rendering must repair the existing row, not append a second one:
        # `_post_view` reads the first primary row and a stale one pointing at a
        # deleted file would keep shadowing the good cover forever.
        siblings = session.execute(
            select(MediaRow).where(MediaRow.post_id == post.id)
        ).scalars().all()
        for row in siblings:
            if row.path == asset.path:
                row.is_primary = is_primary
                row.sha = asset.sha
                row.kind = asset.kind
                return

        reusable = next((r for r in siblings if not Path(r.path).exists()), None)
        if reusable is not None:
            reusable.kind = asset.kind
            reusable.path = asset.path
            reusable.width = asset.width
            reusable.height = asset.height
            reusable.sha = asset.sha
            reusable.caption = asset.caption
            reusable.is_primary = is_primary
            return

        session.add(
            MediaRow(
                post_id=post.id,
                kind=asset.kind,
                path=asset.path,
                width=asset.width,
                height=asset.height,
                sha=asset.sha,
                caption=asset.caption,
                is_primary=is_primary,
            )
        )


def prune_orphan_media() -> int:
    """Drop media rows whose file is gone. Returns rows removed."""
    from sqlalchemy import select

    from tgfarm.db.models import Media as MediaRow
    from tgfarm.db.session import session_scope

    removed = 0
    with session_scope() as session:
        for row in session.execute(select(MediaRow)).scalars().all():
            if not Path(row.path).exists():
                session.delete(row)
                removed += 1
    if removed:
        log.info("pruned %d media rows pointing at missing files", removed)
    return removed


def repair(post_public_ids: list[str] | None = None) -> dict:
    """Re-render covers for every post that is missing one on disk, and correct
    stored media metadata that contradicts the URLs it points at."""
    from sqlalchemy import select

    from tgfarm.config import get_config
    from tgfarm.db.models import Post as PostRow
    from tgfarm.db.session import session_scope
    from tgfarm.s5_quality import load_draft

    config = get_config()
    pruned = prune_orphan_media()
    reclassified = reclassify_media_kinds()
    covered = _posts_with_primary_media()
    if post_public_ids is None:
        with session_scope() as session:
            post_public_ids = list(
                session.execute(
                    select(PostRow.public_id).where(PostRow.status != "rejected").limit(500)
                ).scalars().all()
            )

    fixed = 0
    for public_id in post_public_ids:
        if public_id in covered:
            continue
        loaded = load_draft(public_id)
        if loaded is None:
            continue
        draft, channel_id = loaded
        persona = config.persona(channel_id)
        if persona is None:
            continue
        asset = render_for_post(draft, persona)
        if asset.path:
            persist_media(public_id, asset)
            fixed += 1

    return {
        "pruned": pruned,
        "re_rendered": fixed,
        "reclassified": reclassified,
        "checked": len(post_public_ids),
    }


def reclassify_media_kinds() -> int:
    """Re-derive `media_kind` from the stored `media_links`.

    An early version matched `<img src=...>` with the video pattern, so feed
    covers were stored as `video` and S10 planned a video post around a JPEG.
    Re-deriving the kind from the URLs makes the stored metadata self-consistent
    again without needing the feed to republish the entry.
    """
    from sqlalchemy import select

    from tgfarm.core.media import discover_from_feed, dominant_kind
    from tgfarm.db.models import RawItem as RawItemRow
    from tgfarm.db.session import session_scope

    fixed = 0
    with session_scope() as session:
        rows = session.execute(
            select(RawItemRow).where(RawItemRow.media_kind.isnot(None))
        ).scalars().all()
        for row in rows:
            candidates = discover_from_feed({"links": list(row.media_links or [])})
            correct = dominant_kind(candidates)
            if correct != row.media_kind:
                log.info(
                    "media_kind %s -> %s for %s", row.media_kind, correct, row.external_id[:12]
                )
                row.media_kind = correct
                row.media_links = [c.url for c in candidates]
                fixed += 1
    return fixed


def primary_media(post_public_id: str) -> str | None:
    from sqlalchemy import select

    from tgfarm.db.models import Media as MediaRow
    from tgfarm.db.models import Post as PostRow
    from tgfarm.db.session import session_scope

    with session_scope() as session:
        post = session.execute(
            select(PostRow).where(PostRow.public_id == post_public_id)
        ).scalar_one_or_none()
        if post is None:
            return None
        media = session.execute(
            select(MediaRow)
            .where(MediaRow.post_id == post.id, MediaRow.is_primary.is_(True))
            .order_by(MediaRow.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        return media.path if media else None


async def run(post_public_ids: list[str]) -> dict[str, int]:
    """Render a cover for every post that has none.

    Generated art is tried first when an image provider is configured and the post
    is long enough to be worth an illustration; the Pillow template is always the
    fallback, so this stage never fails the cycle.
    """
    from tgfarm.config import get_config
    from tgfarm.core.run import to_thread
    from tgfarm.s5_quality import load_draft

    config = get_config()
    already = await to_thread(_posts_with_primary_media)
    rendered = generated = 0

    for public_id in post_public_ids:
        if public_id in already:
            continue
        loaded = load_draft(public_id)
        if loaded is None:
            continue
        draft, channel_id = loaded
        persona = config.persona(channel_id)
        if persona is None:
            continue

        asset = None
        if _wants_generated_art(draft):
            from tgfarm.core.schemas import MediaAsset
            from tgfarm.llm.images import generate_cover

            art = await generate_cover(
                first_sentence(draft.hook or draft.body, 120),
                first_sentence(draft.body, 160),
                persona.niche,
                persona.palette,
            )
            if art is not None:
                asset = MediaAsset(
                    kind="ai_art",
                    path=str(art.path),
                    width=art.width,
                    height=art.height,
                    sha=art.sha,
                    caption=draft.hook[:200],
                )
                generated += 1

        if asset is None:
            asset = await to_thread(render_for_post, draft, persona)

        if asset.path:
            await to_thread(persist_media, public_id, asset)
            rendered += 1

    from tgfarm.s6_visual.article_images import attach_inline_images

    inline = await attach_inline_images(post_public_ids)
    log.info(
        "s6: rendered %d covers (%d generated art), attached %d inline photos",
        rendered,
        generated,
        inline,
    )
    return {"rendered": rendered, "generated": generated, "inline": inline}


def _wants_generated_art(draft) -> bool:
    """Illustration earns its keep on long posts, not on one-line flashes."""
    return len(draft.body) > 900 or getattr(draft, "angle", "") == "article"


def _posts_with_primary_media() -> set[str]:
    """Posts whose primary media row points at a file that still exists.

    The existence check is the whole point: the database and the file store
    drift apart (a restore, a manual cleanup, a crashed render), and without it
    S6 reports "already rendered" for a cover that no longer exists and the post
    ships without one.
    """
    from sqlalchemy import select

    from tgfarm.db.models import Media as MediaRow
    from tgfarm.db.models import Post as PostRow
    from tgfarm.db.session import session_scope

    with session_scope() as session:
        rows = session.execute(
            select(PostRow.public_id, MediaRow.path)
            .join(MediaRow, MediaRow.post_id == PostRow.id)
            .where(MediaRow.is_primary.is_(True))
        ).all()
    return {public_id for public_id, path in rows if Path(path).exists()}