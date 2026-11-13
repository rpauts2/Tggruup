"""Render original text-led vertical videos for short-form platforms.

This module creates its own typography cards and never downloads/reuses footage.
FFmpeg is required for MP4 encoding. No fake file is returned when encoding fails.

Two stages sit on top of the encode: a caption track (`build_srt`/`write_srt`,
burned in on request) and a post-render QC pass (`probe_video`/`qc_video`). QC
answers `ok: False` when ffprobe is missing, because an unverified file must
never be reported as a verified one.
"""
from __future__ import annotations

import hashlib
import html
import json
import math
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from tgfarm.core.settings import get_settings

WIDTH, HEIGHT, FPS = 1080, 1920, 30
DEFAULT_SCENE_SECONDS = 3.5
MAX_SCENES = 7
MAX_WORDS_PER_SCENE = 22
MAX_MANIFEST_BYTES = 256 * 1024
PALETTE = {
    "background": (11, 16, 32),
    "panel": (20, 28, 51),
    "foreground": (239, 243, 255),
    "accent": (82, 145, 255),
    "muted": (153, 168, 202),
}
#: libass style for the burned-in track, in the units libass actually lays an
#: SRT out in: an SRT script defaults to a 384x288 canvas, and the cue is scaled
#: onto the 1080x1920 frame by 1920/288 ≈ 6.67. Frame-pixel numbers are therefore
#: wrong by that factor — `Fontsize=30,MarginV=300` puts the text off the bottom
#: of the screen and the renderer silently falls back to its own default, which
#: printed the caption straight over the card's footer line. Measured on this
#: build: `Fontsize=7` is ~28 px of glyph and one MarginV unit is ~6.7 px, so the
#: cue's lower line sits at `1920 - MarginV*6.67`. `MarginV=40` lands the block in
#: the empty band between the panel (ends 1530) and the footer (starts 1665);
#: `Alignment=2` pins the last line there and shorter cues rise into the same band.
SUBTITLE_STYLE = (
    "Fontsize=7,PrimaryColour=&H00FFFFFF,OutlineColour=&H96000000,"
    "BorderStyle=1,Outline=1,Shadow=0,Alignment=2,MarginL=13,MarginR=13,MarginV=40"
)
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
    # Excluded from equality and hashing: they are a report about the file, not
    # part of its identity, and a dict field would make the artifact unhashable.
    qc: dict | None = field(default=None, compare=False, hash=False)
    subtitles: dict | None = field(default=None, compare=False, hash=False)

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


def _srt_timestamp(seconds: float) -> str:
    """Format seconds as SRT's `HH:MM:SS,mmm` — a comma, never a dot."""
    total_ms = max(0, int(round(float(seconds) * 1000)))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole:02d},{millis:03d}"


# C0/C1 controls minus the whitespace `_clean_text` already collapses. SRT is a
# line-oriented text format: a stray NUL or ESC inside a cue makes players drop
# the cue or the whole file, and the source text is untrusted post content.
_SRT_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def build_srt(scenes: Sequence[ShortScene], *, scene_seconds: float) -> str:
    """Turn storyboard scenes into an SRT track, one cue per scene.

    The defect this keeps dead: captions that drift away from the picture. The
    cue grid is derived from the same `scene_seconds` the encoder used for its
    frame count, so cue *i* ends exactly where cue *i+1* starts and the last cue
    ends on the last frame — a caption file timed by a guess about reading speed
    is worse than no caption file, because it looks authoritative.

    Text goes through `_clean_text` for the same reason the cards do: a URL or a
    `<b>` tag in a caption is a leak, not a caption. A scene whose text cleans
    down to nothing is dropped rather than emitted as an empty cue, and the
    numbering stays contiguous for players that index by cue number.
    """
    step = float(scene_seconds)
    if not math.isfinite(step) or step <= 0:
        raise ValueError("scene_seconds должен быть положительным числом.")
    blocks: list[str] = []
    for index, scene in enumerate(scenes):
        text = _SRT_CONTROL_RE.sub(" ", _clean_text(scene.text)).strip()
        if not text:
            continue
        blocks.append(
            f"{len(blocks) + 1}\n"
            f"{_srt_timestamp(index * step)} --> {_srt_timestamp((index + 1) * step)}\n"
            f"{text}\n"
        )
    return "\n".join(blocks)


def write_srt(scenes: Sequence[ShortScene], path: str | Path, *, scene_seconds: float) -> Path:
    """Write the caption track as UTF-8 without a BOM — players choke on one.

    `newline="\\n"` is explicit because the default text mode rewrites `\\n` to
    `\\r\\n` on Windows: the same post would then produce a different caption
    file on the operator laptop than in CI, and the sidecar's hash would stop
    being a property of the content.
    """
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(build_srt(scenes, scene_seconds=scene_seconds))
    return target


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


# `subprocess.run` is captured once, on purpose. The encoder's call is the seam
# tests replace to fake an FFmpeg build, and the probe must not inherit that
# fake: a verification step that the same double can satisfy proves nothing
# about the file on disk.
_RUN = subprocess.run


def _float_or_none(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _ffmpeg_hint(stderr: str | None) -> str:
    lines = (stderr or "").strip().splitlines()
    return lines[-1][:240] if lines else "неизвестная ошибка"


def _ffmpeg_filter_path(path: Path) -> str:
    """Escape a path for an FFmpeg filtergraph option value.

    The filtergraph parser splits options on `:` and reads `\\` as an escape, so
    a Windows path (`C:\\Users\\...`) has to become `C\\:/Users/...` before it can
    be handed to `subtitles=filename=...`. Forward slashes are accepted by
    FFmpeg on every platform, which removes the other half of the problem.
    """
    text = str(path).replace("\\", "/")
    return text.replace(":", "\\:").replace("'", "\\'")


def _encode(
    binary: str,
    *,
    input_pattern: Path,
    vf: str,
    frames_per_scene: int,
    target: Path,
    timeout_s: float,
) -> subprocess.CompletedProcess:
    """Run one FFmpeg pass. Kept separate so the caption retry reuses the shape."""
    command = [
        binary, "-hide_banner", "-loglevel", "error", "-y",
        "-framerate", f"{FPS}/{frames_per_scene}", "-start_number", "0",
        "-i", str(input_pattern), "-vf", vf, "-an", "-c:v", "libx264",
        "-preset", "medium", "-crf", "23", "-r", str(FPS),
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(target),
    ]
    try:
        return subprocess.run(
            command, check=False, capture_output=True, text=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        target.unlink(missing_ok=True)
        raise RuntimeError("FFmpeg превысил лимит времени рендеринга видео.") from exc
    except OSError as exc:
        target.unlink(missing_ok=True)
        raise RuntimeError(f"Не удалось запустить FFmpeg: {type(exc).__name__}.") from exc


def probe_video(path: str | Path, *, ffprobe_bin: str | None = None) -> dict:
    """Read the container's real facts with ffprobe instead of trusting the render.

    The defect this keeps dead: a render that "succeeded" because FFmpeg exited
    zero, while the file is a truncated or audio-only container. Every number
    here comes from the file itself; when ffprobe is missing or refuses the
    file, the answer is `ok: False` with a reason, never a guessed duration.
    """
    target = Path(path).expanduser()
    if not target.is_file():
        return {"ok": False, "reason": "file not found"}
    size_bytes = target.stat().st_size
    if size_bytes == 0:
        return {"ok": False, "reason": "file is empty"}
    binary = ffprobe_bin or shutil.which("ffprobe")
    if not binary:
        return {"ok": False, "reason": "ffprobe unavailable"}
    command = [
        binary, "-hide_banner", "-loglevel", "error",
        "-print_format", "json", "-show_format", "-show_streams", str(target),
    ]
    try:
        completed = _RUN(command, check=False, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "ffprobe timed out"}
    except OSError as exc:
        return {"ok": False, "reason": f"ffprobe could not start: {type(exc).__name__}"}
    if completed.returncode != 0:
        return {"ok": False, "reason": "ffprobe rejected the file"}
    try:
        payload = json.loads(completed.stdout or "")
    except (json.JSONDecodeError, TypeError):
        return {"ok": False, "reason": "ffprobe returned unreadable JSON"}
    if not isinstance(payload, dict):
        return {"ok": False, "reason": "ffprobe returned unreadable JSON"}
    streams = payload.get("streams") or []
    video = next(
        (stream for stream in streams if stream.get("codec_type") == "video"), None
    )
    if video is None:
        return {"ok": False, "reason": "no video stream"}
    duration = _float_or_none((payload.get("format") or {}).get("duration"))
    if duration is None:
        duration = _float_or_none(video.get("duration"))
    if duration is None:
        return {"ok": False, "reason": "duration is unknown"}
    return {
        "ok": True,
        "duration_s": round(duration, 3),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "has_audio": any(stream.get("codec_type") == "audio" for stream in streams),
        "size_bytes": size_bytes,
    }


def qc_video(path: str | Path, *, max_duration_s: float, max_mb: float) -> dict:
    """Check a finished file against the platform limits and report what was checked.

    The defect this keeps dead: a QC that answers `ok: True` because it had
    nothing to check with. When ffprobe is unavailable the verdict is
    `ok: False, reason: "ffprobe unavailable"` — an unverified file must never
    be presented as a verified one, and the caller can tell the two apart by the
    reason string.

    A missing audio track and a landscape frame are warnings, not refusals: a
    silent vertical short is legal, and a 16:9 file is a judgement call for the
    operator rather than a broken render.
    """
    target = Path(path).expanduser()
    checks: dict[str, object] = {"exists": target.is_file()}
    warnings: list[str] = []
    if not target.is_file():
        return {"ok": False, "checks": checks, "warnings": warnings, "reason": "file not found"}
    size_bytes = target.stat().st_size
    checks["non_empty"] = size_bytes > 0
    checks["size_bytes"] = size_bytes
    if size_bytes == 0:
        return {"ok": False, "checks": checks, "warnings": warnings, "reason": "file is empty"}
    duration_limit = float(max_duration_s)
    size_limit = float(max_mb) * 1024 * 1024
    if duration_limit <= 0 or size_limit <= 0:
        return {
            "ok": False, "checks": checks, "warnings": warnings,
            "reason": "limits must be positive",
        }
    probe = probe_video(target)
    if not probe.get("ok"):
        return {
            "ok": False, "checks": checks, "warnings": warnings,
            "reason": str(probe.get("reason") or "probe failed"),
        }
    duration = float(probe["duration_s"])
    width, height = int(probe["width"]), int(probe["height"])
    checks["duration_s"] = duration
    checks["duration_within_limit"] = 0 < duration <= duration_limit
    checks["size_within_limit"] = size_bytes <= size_limit
    checks["vertical"] = height > width
    checks["has_audio"] = bool(probe["has_audio"])
    if not checks["vertical"]:
        warnings.append(f"not vertical: {width}x{height}")
    if not checks["has_audio"]:
        warnings.append("no audio track")
    problems: list[str] = []
    if duration <= 0:
        problems.append("duration is zero")
    elif duration > duration_limit:
        problems.append(f"duration {duration:.1f}s exceeds {duration_limit:.0f}s")
    if size_bytes > size_limit:
        problems.append(f"size {size_bytes / (1024 * 1024):.1f} MB exceeds {max_mb:g} MB")
    return {
        "ok": not problems,
        "checks": checks,
        "warnings": warnings,
        "reason": "; ".join(problems),
    }


def _check_adjacent_sidecars(target: Path) -> None:
    """Do not read or publish a manifest/caption through a redirected path."""
    for suffix in (".json", ".srt"):
        sidecar = target.with_suffix(suffix)
        try:
            resolved = sidecar.resolve()
            redirected = (
                sidecar.is_symlink() or resolved != sidecar or resolved.parent != target.parent
            )
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"Unsafe adjacent sidecar: {sidecar}") from exc
        if redirected:
            raise ValueError(f"Redirected adjacent sidecar: {sidecar}")


def render_short_video(
    title: str,
    body: str,
    *,
    output_path: str | Path | None = None,
    max_scenes: int = MAX_SCENES,
    scene_seconds: float = DEFAULT_SCENE_SECONDS,
    ffmpeg_bin: str | None = None,
    force: bool = False,
    burn_subtitles: bool = False,
) -> ShortVideoArtifact:
    """Render a silent 9:16 MP4 composed of original animated typography cards.

    `burn_subtitles` adds a `subtitles=` filter to the same FFmpeg pass, so the
    captions cost no second encode. Burning is best-effort by design: a build
    without libass, or a path the filtergraph cannot escape, must not lose the
    video that was already encoded. The outcome is reported in
    `artifact.subtitles` (`{"burned": bool, "reason": str, "path": str}`) rather
    than swallowed, so "no captions" is never mistaken for "captions on".

    The finished file is always probed by `qc_video` and the verdict lands in
    `artifact.qc`; a failed QC does not delete the render, it makes the failure
    visible to the caller.
    """
    settings = get_settings()
    scenes = build_storyboard(title, body, max_scenes=max_scenes)
    duration_limit = max(1, int(settings.video_max_duration_s))
    if duration_limit < len(scenes):
        raise ValueError(
            "VIDEO_MAX_DURATION_S слишком мал: нужно минимум по 1 секунде на каждую сцену."
        )
    max_frames_per_scene = max(30, int(duration_limit * FPS / len(scenes)))
    requested_frames = max(30, int(round(float(scene_seconds) * FPS)))
    frames_per_scene = min(requested_frames, max_frames_per_scene)
    scene_seconds = frames_per_scene / FPS
    duration = round(len(scenes) * scene_seconds, 2)

    render_spec = {
        "version": 2,
        "format": "tgfarm-original-short",
        "scenes": [asdict(scene) for scene in scenes],
        "frames_per_scene": frames_per_scene,
        "width": WIDTH, "height": HEIGHT, "fps": FPS,
        "font_path": getattr(settings, "font_path", ""),
        "burn_subtitles": burn_subtitles,
        "subtitle_style": SUBTITLE_STYLE,
    }
    fingerprint = hashlib.sha256(
        json.dumps(render_spec, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if output_path is None:
        output_path = settings.video_root() / "rendered" / f"short_{fingerprint[:12]}.mp4"
    target = Path(output_path).expanduser().resolve()
    if target.suffix.lower() != ".mp4":
        raise ValueError("Видео нужно сохранять в формате MP4.")
    _check_adjacent_sidecars(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not force:
        try:
            # Sidecars are boundary data, not authority to open a different file.
            with target.open("rb") as handle:
                existing_header = handle.read(12)
            with target.with_suffix(".json").open("rb") as handle:
                manifest_bytes = handle.read(MAX_MANIFEST_BYTES + 1)
            if len(manifest_bytes) > MAX_MANIFEST_BYTES:
                raise ValueError("Render manifest exceeds the cache size limit.")
            manifest = json.loads(manifest_bytes.decode("utf-8"))
            saved = manifest["asset"]
            saved_subtitles = saved["subtitles"]
            expected = {
                "path": str(target), "title": _clean_text(title)[:180],
                "width": WIDTH, "height": HEIGHT, "fps": FPS,
                "duration_s": duration, "scene_count": len(scenes),
                "bytes": target.stat().st_size, "sha256": _sha256(target),
            }
            valid = (
                len(existing_header) == 12 and existing_header[4:8] == b"ftyp"
                and type(manifest["version"]) is int and manifest["version"] == render_spec["version"]
                and manifest["format"] == render_spec["format"]
                and manifest["fingerprint"] == fingerprint
                and manifest["scenes"] == render_spec["scenes"]
                and type(manifest["scene_seconds"]) in (int, float)
                and manifest["scene_seconds"] == scene_seconds
                and math.isfinite(manifest["scene_seconds"])
                and all(type(saved[key]) is type(value) and saved[key] == value
                        for key, value in expected.items())
                and type(saved["reused_existing"]) is bool and not saved["reused_existing"]
                and isinstance(saved_subtitles, dict)
                and type(saved_subtitles["burned"]) is bool
                and isinstance(saved_subtitles["reason"], str)
            )
            if valid and burn_subtitles:
                sidecar = target.with_suffix(".srt")
                valid = (
                    saved_subtitles["path"] == str(sidecar)
                    and (not saved_subtitles["reason"] if saved_subtitles["burned"]
                         else bool(saved_subtitles["reason"]))
                    and sidecar.read_bytes() == build_srt(scenes, scene_seconds=scene_seconds).encode("utf-8")
                )
            elif valid:
                valid = saved_subtitles == {"burned": False, "reason": "not requested", "path": None}
            if valid:
                return ShortVideoArtifact(
                    **{key: saved[key] for key in expected}, reused_existing=True,
                    qc=qc_video(
                        target,
                        max_duration_s=settings.video_max_duration_s,
                        max_mb=settings.video_max_mb,
                    ),
                    subtitles=saved_subtitles,
                )
        except (OSError, UnicodeError, ValueError, TypeError, KeyError, RecursionError):
            # Missing, old, malformed or mismatched evidence means a new render.
            pass

    binary = ffmpeg_bin or shutil.which("ffmpeg")
    if not binary:
        raise RuntimeError("FFmpeg не найден. Установи FFmpeg и добавь ffmpeg в PATH.")

    subtitles: dict = {"burned": False, "reason": "not requested", "path": None}
    # All scratch, including the encoded MP4, stays on the destination filesystem.
    # A failed rerender must never overwrite or unlink the previous user's render.
    with tempfile.TemporaryDirectory(prefix="tgfarm-short-", dir=target.parent) as temp:
        frame_dir = Path(temp)
        staged_target = frame_dir / target.name
        for index, scene in enumerate(scenes):
            _draw_scene(scene, index + 1, len(scenes), frame_dir / f"scene_{index:03d}.png")
        input_pattern = frame_dir / "scene_%03d.png"
        vf = (
            "zoompan="
            f"z='min(1+mod(on,{frames_per_scene})*0.0005,1.05)':"
            "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
            f"d={frames_per_scene}:s={WIDTH}x{HEIGHT}:fps={FPS},format=yuv420p"
        )
        if burn_subtitles:
            # The SRT lives next to the frames for the duration of the encode and
            # is copied out afterwards, so a failed burn leaves no half-written
            # sidecar beside the MP4.
            srt_path = write_srt(scenes, frame_dir / "captions.srt", scene_seconds=scene_seconds)
            style = (
                f"force_style='{SUBTITLE_STYLE}'"
                if SUBTITLE_STYLE
                else ""
            )
            vf = (
                f"{vf},subtitles=filename='{_ffmpeg_filter_path(srt_path)}'"
                + (f":{style}" if style else "")
            )
            subtitles = {"burned": True, "reason": "", "path": None}
        timeout_s = max(120, int(duration * 3))
        encode_deadline = time.monotonic() + timeout_s
        completed = _encode(
            binary, input_pattern=input_pattern, vf=vf,
            frames_per_scene=frames_per_scene, target=staged_target, timeout_s=timeout_s,
        )
        if completed.returncode != 0 and burn_subtitles:
            # A missing libass or an unescapable path is a caption problem, not a
            # render problem: retry once without the filter instead of losing the
            # video, and record why the captions are absent.
            subtitles = {
                "burned": False,
                "reason": f"ffmpeg rejected the subtitles filter: {_ffmpeg_hint(completed.stderr)}",
                "path": None,
            }
            remaining_s = encode_deadline - time.monotonic()
            if remaining_s <= 0:
                raise RuntimeError("FFmpeg превысил лимит времени рендеринга видео.")
            completed = _encode(
                binary, input_pattern=input_pattern, vf=vf.split(",subtitles=")[0],
                frames_per_scene=frames_per_scene, target=staged_target, timeout_s=remaining_s,
            )
        if completed.returncode != 0:
            raise RuntimeError(f"FFmpeg не смог собрать видео: {_ffmpeg_hint(completed.stderr)}")
        if not staged_target.is_file() or staged_target.stat().st_size == 0:
            raise RuntimeError("FFmpeg завершился без ошибки, но MP4 не появился.")
        with staged_target.open("rb") as handle:
            header = handle.read(12)
        if len(header) != 12 or header[4:8] != b"ftyp":
            raise RuntimeError("FFmpeg создал файл, который не похож на MP4.")
        if burn_subtitles:
            # Stage the uploadable track even when the burn fell back. Neither
            # sidecar is published until the encoded MP4 has been validated.
            write_srt(scenes, staged_target.with_suffix(".srt"), scene_seconds=scene_seconds)
            subtitles["path"] = str(target.with_suffix(".srt"))

        artifact = ShortVideoArtifact(
            str(target), _clean_text(title)[:180], WIDTH, HEIGHT, FPS, duration,
            len(scenes), staged_target.stat().st_size, _sha256(staged_target),
            qc=qc_video(
                staged_target,
                max_duration_s=settings.video_max_duration_s,
                max_mb=settings.video_max_mb,
            ),
            subtitles=subtitles,
        )
        staged_target.with_suffix(".json").write_text(
            json.dumps(
                {
                    "version": render_spec["version"],
                    "format": render_spec["format"],
                    "fingerprint": fingerprint,
                    # `asset` already carries `qc` and `subtitles`; repeating them at
                    # the top level would give the manifest two places to disagree.
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
        _check_adjacent_sidecars(target)
        replacements = [(staged_target, target)]
        if burn_subtitles:
            replacements.append((staged_target.with_suffix(".srt"), target.with_suffix(".srt")))
        replacements.append((staged_target.with_suffix(".json"), target.with_suffix(".json")))
        # Backups live in a dedicated subdirectory: an output basename must end
        # in `.mp4`, so no legal destination name can collide with this namespace
        # (a flat `previous{suffix}` name overwrites the staged `previous.mp4`).
        backup_dir = frame_dir / "backups"
        backup_dir.mkdir()
        backups = {}
        for _, destination in replacements:
            backup = backup_dir / destination.name if destination.exists() else None
            if backup is not None:
                shutil.copy2(destination, backup)
            backups[destination] = backup
        published = []
        try:
            for staged, destination in replacements:
                staged.replace(destination)
                published.append(destination)
        except OSError as publication_error:
            # A failed restore must not prevent attempts to recover the other
            # destinations. Only new destinations may be removed during rollback.
            restore_errors = []
            for destination in reversed(published):
                backup = backups[destination]
                try:
                    if backup is not None:
                        backup.replace(destination)
                    else:
                        destination.unlink(missing_ok=True)
                except OSError as restore_error:
                    restore_errors.append((destination, restore_error))
            if restore_errors:
                # Move remaining backups outside TemporaryDirectory so its cleanup
                # cannot delete the only remaining old bytes. Backups consumed by
                # successful restores are no longer in this directory.
                recovery = Path(tempfile.mkdtemp(prefix="tgfarm-short-recovery-", dir=target.parent))
                for backup in backup_dir.iterdir():
                    backup.replace(recovery / backup.name)
                details = "; ".join(
                    f"{destination}: {error}" for destination, error in restore_errors
                )
                raise OSError(
                    f"{publication_error}; restoration failed for {details}. "
                    f"Exact previous bytes retained in {recovery}."
                ) from publication_error
            raise
        return artifact


def render_post_short(
    post_public_id: str,
    *,
    force: bool = False,
    output_path: str | Path | None = None,
    burn_subtitles: bool = False,
) -> ShortVideoArtifact:
    """Render a saved post and attach the resulting artifact as non-primary media.

    `burn_subtitles` is passed through to `render_short_video` untouched, so the
    API and the CLI can offer captions without this function deciding for them:
    the automatic delivery stage leaves it `False`, because there the render is
    nobody's explicit choice and a build without libass must not silently change
    what a channel publishes.
    """
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
    artifact = render_short_video(
        title, body, output_path=output_path, force=force, burn_subtitles=burn_subtitles
    )

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
    "build_srt", "build_storyboard", "probe_video", "qc_video",
    "render_post_short", "render_short_video", "write_srt",
]
