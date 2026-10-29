"""Render original text-led vertical videos for short-form platforms.

This module creates its own typography cards and never downloads/reuses footage.
FFmpeg is required for MP4 encoding. No fake file is returned when encoding fails.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from tgfarm.core.settings import get_settings

WIDTH, HEIGHT, FPS = 1080, 1920, 30
DEFAULT_SCENE_SECONDS = 3.5
MAX_SCENES = 7
MAX_WORDS_PER_SCENE = 22
PALETTE = {
    "background": (11, 16, 32),
    "panel": (20, 28, 51),
    "foreground": (239, 243, 255),
    "accent": (82, 145, 255),
    "muted": (153, 168, 202),
}
_FONT_CACHE: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}
_FONT_BOLD = (
    "C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/calibrib.ttf", "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
)
_FONT_REGULAR = (
    "C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/calibri.ttf", "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
)


@dataclass(frozen=True)
class ShortScene:
    role: str
    kicker: str
    text: str


@dataclass(frozen=True)
class ShortVideoArtifact:
    path: str
    title: str
    width: int
    height: int
    fps: int
    duration_s: float
    scene_count: int
    bytes: int
    sha256: str
    reused_existing: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


def _clean_text(value: str | None) -> str:
    value = html.unescape(str(value or ""))
    value = re.sub(r"<[^>]*>", " ", value)
    value = re.sub(r"https?://\S+", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\[[^\]]{1,40}\]\([^)]+\)", " ", value)
    value = re.sub(r"[\*_#>]+", " ", value).replace(chr(96), " ")
    return re.sub(r"\s+", " ", value).strip()


def _body_chunks(body: str, max_words: int) -> list[str]:
    text = _clean_text(body)
    if not text:
        return []
    sentences = [p.strip() for p in re.split(r"(?<=[.!?…])\s+|[\n\r]+", text) if p.strip()]
    if not sentences:
        sentences = [text]
    chunks: list[str] = []
    current: list[str] = []
    count = 0
    for sentence in sentences:
        words = sentence.split()
        if len(words) > max_words:
            if current:
                chunks.append(" ".join(current))
                current, count = [], 0
            chunks.extend(
                " ".join(words[i:i + max_words])
                for i in range(0, len(words), max_words)
            )
            continue
        if current and count + len(words) > max_words:
            chunks.append(" ".join(current))
            current, count = [], 0
        current.extend(words)
        count += len(words)
    if current:
        chunks.append(" ".join(current))
    return chunks


def build_storyboard(
    title: str,
    body: str,
    *,
    max_scenes: int = MAX_SCENES,
    max_words_per_scene: int = MAX_WORDS_PER_SCENE,
) -> list[ShortScene]:
    """Turn an existing post into concise caption scenes without inventing facts."""
    clean_title, clean_body = _clean_text(title), _clean_text(body)
    if not clean_title:
        raise ValueError("Для видео нужен заголовок поста.")
    if not clean_body:
        raise ValueError("У поста нет текста для видео. Выдуманная заглушка не создаётся.")
    limit = min(MAX_SCENES, max(3, int(max_scenes)))
    word_limit = max(8, int(max_words_per_scene))
    chunks = _body_chunks(clean_body, word_limit)
    content_limit = max(1, limit - 2)
    if len(chunks) > content_limit:
        kept = chunks[:content_limit]
        tail = " ".join(chunks[content_limit:])
        if tail and len(kept[-1].split()) < word_limit:
            kept[-1] = " ".join((kept[-1] + " " + tail).split()[:word_limit])
        chunks = kept
    scenes = [ShortScene("hook", "КОРОТКО О ГЛАВНОМ", clean_title[:180])]
    scenes.extend(
        ShortScene("fact", f"ФАКТ {index:02d}", chunk)
        for index, chunk in enumerate(chunks, start=1)
    )
    scenes.append(
        ShortScene("close", "СОХРАНИ ГЛАВНОЕ",
                   "Вернись к полной публикации, чтобы перечитать детали и источники.")
    )
    return scenes[:limit]


def _font(size: int, bold: bool) -> ImageFont.FreeTypeFont:
    settings = get_settings()
    key = (f"{'bold' if bold else 'regular'}:{settings.font_path}", size)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    candidates = ([settings.font_path] if settings.font_path else [])
    candidates.extend(_FONT_BOLD if bold else _FONT_REGULAR)
    for name in candidates:
        if name and Path(name).is_file():
            try:
                font = ImageFont.truetype(name, size=size)
                _FONT_CACHE[key] = font
                return font
            except OSError:
                pass
    font = ImageFont.load_default()
    _FONT_CACHE[key] = font  # type: ignore[assignment]
    return font  # type: ignore[return-value]


def _wrap_pixels(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.splitlines() or [text]:
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if current and draw.textlength(candidate, font=font) > max_width:
                lines.append(current)
                current = word
            else:
                current = candidate
            if draw.textlength(current, font=font) > max_width:
                fragment = ""
                for char in current:
                    test = fragment + char
                    if fragment and draw.textlength(test, font=font) > max_width:
                        lines.append(fragment)
                        fragment = char
                    else:
                        fragment = test
                current = fragment
        if current:
            lines.append(current)
    return lines or [""]


def _draw_scene(scene: ShortScene, index: int, total: int, path: Path) -> None:
    image = Image.new("RGB", (WIDTH, HEIGHT), PALETTE["background"])
    draw = ImageDraw.Draw(image)
    top, bottom = PALETTE["background"], (21, 37, 69)
    for y in range(HEIGHT):
        t = y / max(1, HEIGHT - 1)
        color = tuple(int(top[i] * (1 - t) + bottom[i] * t) for i in range(3))
        draw.line((0, y, WIDTH, y), fill=color)
    draw.ellipse((610, 120, 1230, 740), fill=(17, 35, 68))
    draw.ellipse((-350, 1240, 420, 2010), fill=(14, 29, 55))
    draw.rounded_rectangle((64, 76, 1016, 104), radius=14, fill=(40, 52, 78))
    draw.rounded_rectangle(
        (64, 76, 64 + int(952 * index / max(1, total)), 104),
        radius=14, fill=PALETTE["accent"],
    )
    draw.text((70, 155), "TGFARM  /  SHORTS", font=_font(32, True), fill=PALETTE["foreground"])
    draw.text((1010, 165), f"{index:02d}/{total:02d}", font=_font(26, True),
              fill=PALETTE["muted"], anchor="ra")
    draw.rounded_rectangle((64, 330, 1016, 1530), radius=44, fill=PALETTE["panel"],
                           outline=(51, 70, 106), width=3)
    draw.rounded_rectangle((112, 405, 126, 469), radius=7, fill=PALETTE["accent"])
    draw.text((154, 418), scene.kicker, font=_font(29, True), fill=PALETTE["accent"])

    size = 78 if scene.role == "hook" else 67
    minimum = 47
    font = _font(size, True)
    lines = _wrap_pixels(draw, scene.text, font, 810)
    while len(lines) > 8 and size > minimum:
        size -= 4
        font = _font(size, True)
        lines = _wrap_pixels(draw, scene.text, font, 810)
    if len(lines) > 8:
        lines = lines[:8]
        lines[-1] = lines[-1].rstrip(" .") + "…"
    line_height = int(size * 1.22)
    y = 535 + max(0, (780 - len(lines) * line_height) // 2)
    for line in lines:
        draw.text((112, y), line, font=font, fill=PALETTE["foreground"])
        y += line_height
    draw.text((70, 1665), "ПОЛНАЯ ПУБЛИКАЦИЯ  •  ДЕТАЛИ И КОНТЕКСТ",
              font=_font(23, False), fill=PALETTE["muted"])
    draw.text((70, 1785), "Главное без лишнего шума.",
              font=_font(34, True), fill=PALETTE["foreground"])
    image.save(path, format="PNG", optimize=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def render_short_video(
    title: str,
    body: str,
    *,
    output_path: str | Path | None = None,
    max_scenes: int = MAX_SCENES,
    scene_seconds: float = DEFAULT_SCENE_SECONDS,
    ffmpeg_bin: str | None = None,
    force: bool = False,
) -> ShortVideoArtifact:
    """Render a silent 9:16 MP4 composed of original animated typography cards."""
    settings = get_settings()
    scenes = build_storyboard(title, body, max_scenes=max_scenes)
    duration_limit = max(1, int(settings.video_max_duration_s))
    scene_seconds = max(1.0, min(float(scene_seconds), duration_limit / len(scenes)))
    frames_per_scene = max(30, int(round(scene_seconds * FPS)))
    scene_seconds = frames_per_scene / FPS
    duration = round(len(scenes) * scene_seconds, 2)

    if output_path is None:
        script_hash = hashlib.sha256(
            (_clean_text(title) + "\n" + _clean_text(body)).encode("utf-8")
        ).hexdigest()[:12]
        output_path = settings.video_root() / "rendered" / f"short_{script_hash}.mp4"
    target = Path(output_path).expanduser().resolve()
    if target.suffix.lower() != ".mp4":
        raise ValueError("Видео нужно сохранять в формате MP4.")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and target.stat().st_size > 0 and not force:
        return ShortVideoArtifact(
            str(target), _clean_text(title)[:180], WIDTH, HEIGHT, FPS, duration,
            len(scenes), target.stat().st_size, _sha256(target), True,
        )

    binary = ffmpeg_bin or shutil.which("ffmpeg")
    if not binary:
        raise RuntimeError("FFmpeg не найден. Установи FFmpeg и добавь ffmpeg в PATH.")

    with tempfile.TemporaryDirectory(prefix="tgfarm-short-") as temp:
        frame_dir = Path(temp)
        for index, scene in enumerate(scenes):
            _draw_scene(scene, index + 1, len(scenes), frame_dir / f"scene_{index:03d}.png")
        input_pattern = frame_dir / "scene_%03d.png"
        vf = (
            "zoompan="
            f"z='min(1+mod(on,{frames_per_scene})*0.0005,1.05)':"
            "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
            f"d={frames_per_scene}:s={WIDTH}x{HEIGHT}:fps={FPS},format=yuv420p"
        )
        command = [
            binary, "-hide_banner", "-loglevel", "error", "-y",
            "-framerate", f"1/{scene_seconds:g}", "-start_number", "0",
            "-i", str(input_pattern), "-vf", vf, "-an", "-c:v", "libx264",
            "-preset", "medium", "-crf", "23", "-r", str(FPS),
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(target),
        ]
        try:
            completed = subprocess.run(
                command, check=False, capture_output=True, text=True,
                timeout=max(120, int(duration * 3)),
            )
        except subprocess.TimeoutExpired as exc:
            target.unlink(missing_ok=True)
            raise RuntimeError("FFmpeg превысил лимит времени рендеринга видео.") from exc
        except OSError as exc:
            target.unlink(missing_ok=True)
            raise RuntimeError(f"Не удалось запустить FFmpeg: {type(exc).__name__}.") from exc
        if completed.returncode != 0:
            target.unlink(missing_ok=True)
            lines = (completed.stderr or "").strip().splitlines()
            hint = lines[-1][:240] if lines else "неизвестная ошибка"
            raise RuntimeError(f"FFmpeg не смог собрать видео: {hint}")
        if not target.is_file() or target.stat().st_size == 0:
            target.unlink(missing_ok=True)
            raise RuntimeError("FFmpeg завершился без ошибки, но MP4 не появился.")
        with target.open("rb") as handle:
            header = handle.read(12)
        if len(header) < 8 or header[4:8] != b"ftyp":
            target.unlink(missing_ok=True)
            raise RuntimeError("FFmpeg создал файл, который не похож на MP4.")

    artifact = ShortVideoArtifact(
        str(target), _clean_text(title)[:180], WIDTH, HEIGHT, FPS, duration,
        len(scenes), target.stat().st_size, _sha256(target),
    )
    target.with_suffix(".json").write_text(
        json.dumps(
            {
                "version": 1,
                "format": "tgfarm-original-short",
                "asset": artifact.as_dict(),
                "scene_seconds": scene_seconds,
                "scenes": [asdict(scene) for scene in scenes],
                "audio": "none",
                "source_video_reused": False,
            },
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    return artifact


def render_post_short(
    post_public_id: str,
    *,
    force: bool = False,
    output_path: str | Path | None = None,
) -> ShortVideoArtifact:
    """Render a saved post and attach the resulting artifact as non-primary media."""
    from sqlalchemy import select
    from tgfarm.db.models import Post as PostRow
    from tgfarm.db.session import session_scope

    with session_scope() as session:
        post = session.execute(
            select(PostRow).where(PostRow.public_id == post_public_id)
        ).scalar_one_or_none()
        if post is None:
            raise ValueError(f"Пост {post_public_id!r} не найден.")
        title, body = str(post.hook or ""), str(post.body or "")

    if output_path is None:
        identity = hashlib.sha256(str(post_public_id).encode("utf-8")).hexdigest()[:10]
        script_hash = hashlib.sha256(
            (_clean_text(title) + "\n" + _clean_text(body)).encode("utf-8")
        ).hexdigest()[:12]
        output_path = get_settings().video_root() / "rendered" / f"post_{identity}_{script_hash}.mp4"
    artifact = render_short_video(title, body, output_path=output_path, force=force)

    from tgfarm.core.schemas import MediaAsset
    from tgfarm.s6_visual import persist_media
    persist_media(
        post_public_id,
        MediaAsset(
            kind="video", path=artifact.path, width=artifact.width, height=artifact.height,
            sha=artifact.sha256, caption="Original text-led vertical video; silent, 1080x1920.",
        ),
        is_primary=False,
    )
    return artifact


__all__ = [
    "MAX_SCENES", "ShortScene", "ShortVideoArtifact",
    "build_storyboard", "render_post_short", "render_short_video",
]
