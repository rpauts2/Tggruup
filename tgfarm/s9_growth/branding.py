"""S9 — brand identity rotation (group cover / description)."""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from tgfarm.core.logging import get_logger
from tgfarm.core.run import to_thread
from tgfarm.core.settings import get_settings

log = get_logger(__name__)

COVER_W, COVER_H = 1590, 400
ROTATION_DAYS = 15
STATE_FILE = "branding_state.json"


def _state_path() -> Path:
    return get_settings().data_root() / STATE_FILE


def _load_state() -> dict:
    path = _state_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_state(state: dict) -> None:
    _state_path().write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _font(size: int, bold: bool = True):
    candidates = [
        "C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
    ]
    for c in candidates:
        try:
            if Path(c).exists():
                return ImageFont.truetype(c, size)
        except Exception:  # noqa: BLE001
            continue
    return ImageFont.load_default()


def generate_cover(title: str, subtitle: str, seed: int | None = None) -> Path:
    rng = random.Random(seed if seed is not None else int(datetime.now(UTC).timestamp()))
    img = Image.new("RGB", (COVER_W, COVER_H))
    draw = ImageDraw.Draw(img)
    c1 = (rng.randint(10, 40), rng.randint(20, 60), rng.randint(60, 120))
    c2 = (rng.randint(60, 120), rng.randint(20, 60), rng.randint(10, 40))
    for y in range(COVER_H):
        t = y / max(1, COVER_H - 1)
        r = int(c1[0] * (1 - t) + c2[0] * t)
        g = int(c1[1] * (1 - t) + c2[1] * t)
        b = int(c1[2] * (1 - t) + c2[2] * t)
        draw.line([(0, y), (COVER_W, y)], fill=(r, g, b))
    draw.rectangle([(0, COVER_H - 18), (COVER_W, COVER_H)], fill=(255, 255, 255))
    title_font = _font(72)
    sub_font = _font(34, bold=False)
    draw.text((72, 90), title[:40], font=title_font, fill=(255, 255, 255))
    draw.text((72, 190), subtitle[:80], font=sub_font, fill=(220, 230, 255))
    out_dir = get_settings().data_root() / "branding"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"cover_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.png"
    img.save(out, format="PNG")
    return out
import httpx


async def _vk_request(method: str, data: dict) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"https://api.vk.com/method/{method}", data=data)
        resp.raise_for_status()
        return resp.json()


async def rotate_vk_cover(title: str, subtitle: str) -> bool:
    settings = get_settings()
    if not (settings.vk_access_token and settings.vk_group_id):
        log.info("vk branding skipped: not configured")
        return False
    group_id = abs(int(settings.vk_group_id))
    path = await to_thread(generate_cover, title, subtitle)
    image_bytes = path.read_bytes()
    try:
        up = await _vk_request(
            "photos.getOwnerCoverPhotoUploadServer",
            {"group_id": group_id, "crop_x": 0, "crop_y": 0, "crop_x2": COVER_W, "crop_y2": COVER_H,
             "access_token": settings.vk_access_token, "v": "5.199"},
        )
        upload_url = (up.get("response") or {}).get("upload_url")
        if not upload_url:
            log.warning("vk cover upload server missing: %s", up)
            return False
        async with httpx.AsyncClient(timeout=30.0) as client:
            files = {"photo": ("cover.png", image_bytes, "image/png")}
            up_resp = await client.post(upload_url, files=files, timeout=30.0)
            up_data = up_resp.json()
        save = await _vk_request(
            "photos.saveOwnerCoverPhoto",
            {"hash": up_data.get("hash"), "photo": up_data.get("photo"), "group_id": group_id,
             "access_token": settings.vk_access_token, "v": "5.199"},
        )
        if "error" in save:
            log.warning("vk cover save failed: %s", save.get("error"))
            return False
        log.info("vk cover updated: %s", path.name)
        return True
    except Exception as exc:  # noqa: BLE001 - cover upload is best-effort
        log.warning("vk cover upload failed: %s", exc)
        return False


async def update_vk_description(description: str) -> bool:
    settings = get_settings()
    if not (settings.vk_access_token and settings.vk_group_id):
        return False
    group_id = abs(int(settings.vk_group_id))
    resp = await _vk_request(
        "groups.setSettings",
        {"group_id": group_id, "description": description[:500],
         "access_token": settings.vk_access_token, "v": "5.199"},
    )
    if "error" in resp:
        log.warning("vk description update failed: %s", resp.get("error"))
        return False
    log.info("vk description updated")
    return True


def prepare_dzen_cover(title: str, subtitle: str) -> Path:
    return generate_cover(title, subtitle)


async def run_branding_tick() -> dict:
    settings = get_settings()
    state = _load_state()
    last = state.get("last_rotation")
    now = datetime.now(UTC)
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=UTC)
            if (now - last_dt).days < ROTATION_DAYS:
                return {"rotated": False, "reason": "too_soon",
                        "days_left": ROTATION_DAYS - (now - last_dt).days}
        except ValueError:
            pass
    title = "Всякая всячина"
    subtitle = "Обновляем обложку каждые 15 дней"
    vk_ok = await rotate_vk_cover(title, subtitle)
    await update_vk_description(
        "Канал о находках, идеях и полезных материалах. Обновляемся регулярно."
    )
    dzen_path = await to_thread(prepare_dzen_cover, title, subtitle)
    state["last_rotation"] = now.isoformat()
    state["last_cover"] = str(dzen_path)
    _save_state(state)
    log.info("branding rotated; dzen cover ready at %s (manual upload)", dzen_path)
    return {"rotated": True, "vk_ok": vk_ok, "dzen_cover": str(dzen_path)}