"""Consent-gated Douyin video creation using the Open Platform API."""
from __future__ import annotations
from pathlib import Path
import httpx
from tgfarm.core.schemas import CrosspostResult
from tgfarm.core.settings import get_settings

UPLOAD_URL = "https://open.douyin.com/video/upload/"
CREATE_URL = "https://open.douyin.com/video/create/"
MAX_SINGLE_UPLOAD_BYTES = 50 * 1024 * 1024


def _data(response: httpx.Response, operation: str) -> dict:
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"Douyin вернул некорректный ответ: {operation}.") from exc
    if response.status_code >= 400:
        raise RuntimeError(f"Douyin HTTP {response.status_code}: {operation}.")
    if not isinstance(payload, dict):
        raise RuntimeError(f"Douyin вернул неожиданный ответ: {operation}.")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    extra = payload.get("extra") if isinstance(payload.get("extra"), dict) else {}
    code = data.get("error_code")
    if code in (None, "", 0, "0"):
        code = extra.get("error_code", 0)
    try:
        code = int(code or 0)
    except (TypeError, ValueError):
        code = -1
    if code:
        message = str(data.get("description") or extra.get("description") or "ошибка API")
        raise RuntimeError(f"Douyin отклонил {operation} (код {code}): {message[:160]}")
    return data


async def publish_douyin_video(video_path: str | Path, *, caption: str) -> CrosspostResult:
    from tgfarm.douyin_oauth import get_valid_douyin_access_token
    path = Path(video_path).expanduser()
    platform = "douyin"
    if not path.is_file():
        return CrosspostResult(platform=platform, status="failed", error="Оригинальный MP4 не найден.")
    if path.suffix.lower() not in {".mp4", ".webm"}:
        return CrosspostResult(platform=platform, status="permanent", error="Douyin поддерживает MP4 или WebM.")
    size = path.stat().st_size
    if size <= 0:
        return CrosspostResult(platform=platform, status="permanent", error="Видео пустое.")
    limit = min(max(1, int(get_settings().video_max_mb)) * 1024 * 1024, MAX_SINGLE_UPLOAD_BYTES)
    if size > limit:
        return CrosspostResult(platform=platform, status="permanent", error="Файл больше 50 MiB. Для него нужен chunk upload, который ещё не включён.")
    text_value = str(caption or "").strip()
    if not text_value:
        return CrosspostResult(platform=platform, status="permanent", error="Добавь подпись к видео.")
    if len(text_value) > 1000:
        return CrosspostResult(platform=platform, status="permanent", error="Подпись Douyin не должна превышать 1000 символов.")
    settings = get_settings()
    open_id = str(settings.douyin_open_id or "").strip()
    try:
        token = await get_valid_douyin_access_token()
    except Exception as exc:
        return CrosspostResult(platform=platform, status="failed", error=str(exc)[:300])
    try:
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=False) as client:
            mime = "video/mp4" if path.suffix.lower() == ".mp4" else "video/webm"
            with path.open("rb") as handle:
                upload = await client.post(
                    UPLOAD_URL, params={"open_id": open_id}, headers={"access-token": token},
                    files={"video": (path.name, handle, mime)},
                )
            uploaded = _data(upload, "загрузка видео")
            video_object = uploaded.get("video") if isinstance(uploaded.get("video"), dict) else {}
            video_id = str(video_object.get("video_id") or "")
            if not video_id:
                return CrosspostResult(platform=platform, status="failed", error="Douyin не вернул video_id; create_video не выполнялся.")
            try:
                created = await client.post(
                    CREATE_URL, params={"open_id": open_id},
                    headers={"access-token": token, "Content-Type": "application/json"},
                    json={"video_id": video_id, "text": text_value},
                )
            except httpx.ConnectError:
                return CrosspostResult(platform=platform, status="failed", error="Не удалось соединиться с Douyin при создании видео.")
            except (httpx.TimeoutException, httpx.RequestError):
                return CrosspostResult(platform=platform, status="unknown", error="Douyin мог принять create request до потери ответа. Проверь профиль и не повторяй вслепую.")
            try:
                result = _data(created, "создание видео")
            except RuntimeError as exc:
                if created.status_code in (408, 425) or created.status_code >= 500:
                    return CrosspostResult(platform=platform, status="unknown", error="Douyin мог принять видео до ошибки ответа. Проверь аккаунт перед повтором.")
                return CrosspostResult(platform=platform, status="failed", error=str(exc)[:300])
            item_id = str(result.get("item_id") or "")
            if not item_id:
                return CrosspostResult(platform=platform, status="unknown", error="Douyin не вернул item_id. Проверь аккаунт перед повтором.")
            return CrosspostResult(platform=platform, status="posted", remote_id=item_id)
    except httpx.ConnectError:
        return CrosspostResult(platform=platform, status="failed", error="Не удалось подключиться к Douyin для загрузки файла.")
    except httpx.TimeoutException:
        return CrosspostResult(platform=platform, status="failed", error="Загрузка файла в Douyin не завершилась.")
    except (OSError, httpx.RequestError) as exc:
        return CrosspostResult(platform=platform, status="failed", error=f"Ошибка загрузки видео ({type(exc).__name__}).")
    except RuntimeError as exc:
        return CrosspostResult(platform=platform, status="failed", error=str(exc)[:300])
