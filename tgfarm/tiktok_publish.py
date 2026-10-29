"""Explicit, consent-gated TikTok direct publishing for original TgFarm videos.

No scheduler calls this module. The dashboard must query creator_info, show the
current privacy options and require an explicit per-video confirmation first.
"""
from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path

import httpx

from tgfarm.core.schemas import CrosspostResult
from tgfarm.core.settings import get_settings

INIT_URL = "https://open.tiktokapis.com/v2/post/publish/video/init/"
STATUS_URL = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"
MIN_CHUNK = 5 * 1024 * 1024
MAX_CHUNK = 64 * 1024 * 1024
POLL_ATTEMPTS = 8
POLL_DELAY_SECONDS = 2


def _truncate_utf16(value: str, max_units: int) -> str:
    output: list[str] = []
    units = 0
    for char in str(value or ""):
        width = len(char.encode("utf-16-le")) // 2
        if units + width > max_units:
            break
        output.append(char)
        units += width
    return "".join(output).strip()


def _video_duration(path: Path) -> float | None:
    """Read renderer metadata when available; never invent a duration."""
    sidecar = path.with_suffix(".json")
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        duration = float((data.get("asset") or {}).get("duration_s"))
        return duration if math.isfinite(duration) and duration > 0 else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _error_code(payload: dict) -> str:
    value = payload.get("error") or {}
    return str(value.get("code") or "").lower() if isinstance(value, dict) else ""


async def _provider_json(response: httpx.Response, label: str) -> dict:
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code >= 400:
        if response.status_code in (401, 403):
            raise RuntimeError(f"TikTok отклонил доступ во время {label}. Проверь токен, scope video.publish и статус приложения.")
        if response.status_code == 429:
            raise RuntimeError(f"TikTok ограничил частоту запросов во время {label}. Повтори позже.")
        raise RuntimeError(f"TikTok ответил HTTP {response.status_code} во время {label}.")
    if not isinstance(data, dict):
        raise RuntimeError(f"TikTok вернул неожиданный ответ во время {label}.")
    code = _error_code(data)
    if code and code != "ok":
        message = {
            "scope_not_authorized": "TikTok не одобрил scope video.publish для этого аккаунта.",
            "access_token_invalid": "TikTok access token истёк или отозван. Переподключи аккаунт.",
            "spam_risk_too_many_pending_share": "У TikTok достигнут лимит незавершённых API-публикаций. Проверь TikTok и повтори позже.",
            "spam_risk_user_banned_from_posting": "TikTok запретил новые публикации для этого аккаунта.",
            "privacy_level_option_mismatch": "Выбранная приватность больше не доступна. Обнови параметры публикации.",
        }.get(code, f"TikTok отклонил {label} (код {code}).")
        raise RuntimeError(message)
    return data


async def publish_tiktok_video(
    video_path: str | Path,
    *,
    caption: str,
    privacy_level: str,
) -> CrosspostResult:
    """Upload a local MP4 and direct-post it, reconciling ambiguous outcomes safely."""
    from tgfarm.tiktok_oauth import get_tiktok_creator_info, get_valid_tiktok_access_token

    settings = get_settings()
    path = Path(video_path).expanduser()
    if not path.is_file():
        return CrosspostResult(
            platform="tiktok",
            status="failed",
            error="Оригинальное видео не найдено на диске. Создай ролик повторно.",
        )
    size = path.stat().st_size
    if size <= 0:
        return CrosspostResult(platform="tiktok", status="permanent", error="Видео пустое.")
    max_bytes = max(1, int(settings.video_max_mb)) * 1024 * 1024
    if size > max_bytes:
        return CrosspostResult(
            platform="tiktok",
            status="permanent",
            error=f"Видео больше лимита TgFarm ({settings.video_max_mb} MiB). Уменьши размер и повтори.",
        )
    clean_caption = _truncate_utf16(caption, 2200)
    if not clean_caption:
        return CrosspostResult(platform="tiktok", status="permanent", error="Добавь подпись к видео.")
    if not str(privacy_level or "").strip():
        return CrosspostResult(platform="tiktok", status="permanent", error="Выбери уровень приватности TikTok.")

    try:
        token = await get_valid_tiktok_access_token()
        # TikTok requires a fresh creator-info query before the export/publish UI.
        creator = await get_tiktok_creator_info(access_token=token)
    except Exception as exc:  # noqa: BLE001 - no video bytes have been sent yet
        return CrosspostResult(
            platform="tiktok",
            status="permanent",
            error=str(exc)[:300] if isinstance(exc, RuntimeError) else "Не удалось проверить TikTok перед публикацией.",
        )

    options = creator.get("privacy_level_options") or []
    if privacy_level not in options:
        return CrosspostResult(
            platform="tiktok",
            status="permanent",
            error="TikTok изменил доступные настройки приватности. Открой подготовку видео и проверь их заново.",
        )
    duration = _video_duration(path)
    max_duration = int(creator.get("max_video_post_duration_sec") or 0)
    if duration is not None and max_duration > 0 and duration > max_duration:
        return CrosspostResult(
            platform="tiktok",
            status="permanent",
            error=f"Длительность видео {duration:.1f} с, а текущий предел этого аккаунта {max_duration} с.",
        )

    chunk_size = size if size < MIN_CHUNK else min(MAX_CHUNK, 10 * 1024 * 1024)
    total_chunks = math.ceil(size / chunk_size)
    payload = {
        "post_info": {
            "title": clean_caption,
            "privacy_level": privacy_level,
            "disable_comment": bool(creator.get("comment_disabled")),
            "disable_duet": bool(creator.get("duet_disabled")),
            "disable_stitch": bool(creator.get("stitch_disabled")),
        },
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": size,
            "chunk_size": chunk_size,
            "total_chunk_count": total_chunks,
        },
        "post_mode": "DIRECT_POST",
        "media_type": "VIDEO",
        # This renderer creates an AI-generated typography video. Declare it
        # so TikTok can label the content appropriately.
        "is_aigc": True,
    }

    upload_started = False
    publish_id = ""
    try:
        async with httpx.AsyncClient(timeout=45.0, follow_redirects=False) as client:
            init_response = await client.post(
                INIT_URL,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"},
                json=payload,
            )
            init_data = await _provider_json(init_response, "инициализации Direct Post")
            data = init_data.get("data") or {}
            publish_id = str(data.get("publish_id") or "").strip()
            upload_url = str(data.get("upload_url") or "").strip()
            if not publish_id or not upload_url:
                return CrosspostResult(
                    platform="tiktok",
                    status="failed",
                    error="TikTok не выдал параметры загрузки. Проверь настройки приложения и попробуй ещё раз.",
                )

            # Transfer one chunk at a time. If the response is lost after bytes
            # were sent, the upload outcome is unknown and a duplicate is unsafe.
            with path.open("rb") as handle:
                for index in range(total_chunks):
                    start = index * chunk_size
                    chunk = handle.read(min(chunk_size, size - start))
                    if not chunk:
                        return CrosspostResult(
                            platform="tiktok",
                            status="failed",
                            error="Не удалось прочитать полный видеофайл.",
                        )
                    end = start + len(chunk) - 1
                    upload_started = True
                    response = await client.put(
                        upload_url,
                        content=chunk,
                        headers={
                            "Content-Type": "video/mp4",
                            "Content-Length": str(len(chunk)),
                            "Content-Range": f"bytes {start}-{end}/{size}",
                        },
                    )
                    if response.status_code >= 400:
                        try:
                            response_payload = response.json()
                        except ValueError:
                            response_payload = {}
                        code = _error_code(response_payload) if isinstance(response_payload, dict) else ""
                        if response.status_code in (401, 403, 413) or code in {
                            "scope_not_authorized", "access_token_invalid"
                        }:
                            return CrosspostResult(
                                platform="tiktok",
                                status="permanent",
                                error=f"TikTok отклонил передачу видео (HTTP {response.status_code}). Проверь права и формат файла.",
                                remote_id=publish_id,
                            )
                        return CrosspostResult(
                            platform="tiktok",
                            status="unknown",
                            error="Соединение оборвалось при передаче видео. Проверь TikTok перед любым повтором.",
                            remote_id=publish_id,
                        )

            # Processing may outlive this request. Poll briefly, then preserve
            # publish_id as an unknown result instead of triggering another post.
            last_state = ""
            for attempt in range(POLL_ATTEMPTS):
                status_response = await client.post(
                    STATUS_URL,
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"},
                    json={"publish_id": publish_id},
                )
                status_payload = await _provider_json(status_response, "проверки статуса")
                status_data = status_payload.get("data") or {}
                last_state = str(status_data.get("status") or "").upper()
                if last_state == "PUBLISH_COMPLETE":
                    ids = status_data.get("publicaly_available_post_id") or status_data.get("publicly_available_post_id") or []
                    if isinstance(ids, str):
                        ids = [ids]
                    video_id = str(ids[0]) if ids else publish_id
                    username = str(creator.get("username") or "")
                    url = f"https://www.tiktok.com/@{username}/video/{video_id}" if username and ids else None
                    return CrosspostResult(
                        platform="tiktok",
                        status="posted",
                        remote_id=video_id,
                        url=url,
                    )
                if last_state == "FAILED":
                    reason = str(status_data.get("fail_reason") or "TikTok завершил публикацию ошибкой.")
                    return CrosspostResult(
                        platform="tiktok",
                        status="failed",
                        error=reason[:260],
                        remote_id=publish_id,
                    )
                if attempt < POLL_ATTEMPTS - 1:
                    await asyncio.sleep(POLL_DELAY_SECONDS)

            return CrosspostResult(
                platform="tiktok",
                status="unknown",
                error=(
                    "TikTok принял видео, но итог ещё не подтверждён. Проверь профиль перед повтором. "
                    f"Последнее состояние: {last_state or 'неизвестно'}."
                ),
                remote_id=publish_id,
            )
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        log.warning("tiktok direct post network outcome unknown (%s)", type(exc).__name__)
        return CrosspostResult(
            platform="tiktok",
            status="unknown" if upload_started or publish_id else "failed",
            error=(
                "Неизвестно, завершилась ли загрузка TikTok. Проверь аккаунт перед повтором."
                if upload_started or publish_id
                else "TikTok не ответил до начала передачи видео. Повтори позднее."
            ),
            remote_id=publish_id or None,
        )
    except RuntimeError as exc:
        return CrosspostResult(
            platform="tiktok",
            status="unknown" if upload_started else "permanent",
            error=str(exc)[:300],
            remote_id=publish_id or None,
        )
