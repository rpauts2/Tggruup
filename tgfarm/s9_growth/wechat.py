"""WeChat Official Account article publisher (not WeChat Channels or mass messaging)."""
from __future__ import annotations

import asyncio
import html
import io
import re
import time
from pathlib import Path

import httpx
from PIL import Image

from tgfarm.core.schemas import CrosspostPayload, CrosspostResult
from tgfarm.core.settings import get_settings

API_BASE = "https://api.weixin.qq.com"
THUMB_MAX_BYTES = 10 * 1024 * 1024
#: WeChat errcodes that mean "the request was fine, the service is not": -1 is
#: its system-busy answer, 45009 the daily quota, 45011 the per-minute one.
_WECHAT_TRANSIENT_CODES = frozenset({-1, 45009, 45011})


class WeChatAPIError(RuntimeError):
    def __init__(self, code: int, message: str = '') -> None:
        self.code = int(code)
        self.message = str(message or '')[:180]
        super().__init__(f'WeChat API errcode={self.code}')


class WeChatOfficialAccountAdapter:
    """Create and publish one article via the owner's Official Account API permissions."""

    platform = 'wechat'
    max_title = 64
    max_body = 20_000
    poll_attempts = 5
    poll_delay_seconds = 2.0

    def __init__(self) -> None:
        self._access_token = ''
        self._expires_at = 0.0
        self._token_lock = asyncio.Lock()

    def _credentials(self) -> tuple[str, str, str]:
        settings = get_settings()
        return (
            str(settings.wechat_app_id or '').strip(),
            str(settings.wechat_app_secret or '').strip(),
            str(settings.wechat_author or '').strip(),
        )

    def configured(self) -> bool:
        app_id, app_secret, _author = self._credentials()
        return bool(app_id and app_secret)

    @staticmethod
    def _check_response(data: object) -> dict:
        if not isinstance(data, dict):
            raise ValueError('WeChat returned a non-object response.')
        try:
            code = int(data.get('errcode') or 0)
        except (TypeError, ValueError):
            code = -1
        if code != 0:
            raise WeChatAPIError(code, str(data.get('errmsg') or ''))
        return data

    @staticmethod
    def _api_error_result(exc: WeChatAPIError, *, draft_id: str | None = None) -> CrosspostResult:
        # Codes WeChat documents as "try again later", not as a rejection of the
        # request: -1 is its system-busy answer (and what `_check_response`
        # substitutes for an errcode it cannot read), 45009 is the daily call
        # quota and 45011 the per-minute one. Every one of them used to come back
        # `permanent`, which the journal suppresses for good, so a busy minute at
        # WeChat silently ended a delivery that had nothing wrong with it.
        if exc.code in _WECHAT_TRANSIENT_CODES:
            cooldown = 60 if exc.code == 45011 else 3600 if exc.code == 45009 else 300
            return CrosspostResult(
                platform='wechat',
                status='failed',
                remote_id=draft_id,
                error=f'WeChat is temporarily unavailable (errcode {exc.code}); retry is scheduled.',
                retry_after_seconds=cooldown,
            )
        if exc.code == 48001 and draft_id:
            return CrosspostResult(
                platform='wechat', status='permanent', remote_id=draft_id,
                error=('WeChat saved the article draft, but this Official Account does not have '
                       'freepublish permission (48001). Finish it in MP admin; automatic retries are blocked.'),
            )
        if exc.code == 40164:
            hint = 'Public server IP is not in the Official Account API allowlist (40164).'
        elif exc.code in {40001, 40014, 42001}:
            hint = 'WeChat rejected the access token. Recheck AppID/AppSecret and token permissions.'
        elif exc.code == 45004:
            hint = 'WeChat rejected the article digest because it is too long (45004).'
        elif exc.code == 48001:
            hint = 'This Official Account is not authorized for the requested API operation (48001).'
        else:
            hint = f'WeChat API rejected the request (errcode {exc.code}).'
        return CrosspostResult(platform='wechat', status='permanent', remote_id=draft_id, error=hint)

    async def _get_access_token(self, client: httpx.AsyncClient) -> str:
        app_id, app_secret, _author = self._credentials()
        if self._access_token and self._expires_at > time.monotonic() + 60:
            return self._access_token
        async with self._token_lock:
            if self._access_token and self._expires_at > time.monotonic() + 60:
                return self._access_token
            response = await client.post(
                f'{API_BASE}/cgi-bin/stable_token',
                json={
                    'grant_type': 'client_credential',
                    'appid': app_id,
                    'secret': app_secret,
                    'force_refresh': False,
                },
            )
            data = self._check_response(response.json())
            token = str(data.get('access_token') or '').strip()
            if not token:
                raise ValueError('WeChat stable_token response did not include access_token.')
            try:
                lifetime = max(120, int(data.get('expires_in') or 7200))
            except (TypeError, ValueError):
                lifetime = 7200
            self._access_token = token
            self._expires_at = time.monotonic() + lifetime - 60
            return token

    @staticmethod
    def _prepare_thumbnail(path_value: str) -> bytes:
        path = Path(path_value)
        if not path.is_file():
            raise ValueError('WeChat cover image is missing on disk.')
        if path.stat().st_size <= 0 or path.stat().st_size > 20 * 1024 * 1024:
            raise ValueError('WeChat cover image must be non-empty and at most 20 MB before compression.')
        try:
            with Image.open(path) as source:
                image = source.convert('RGB')
                image.thumbnail((900, 383), Image.Resampling.LANCZOS)
        except Exception as exc:
            raise ValueError('WeChat cover is not a supported image.') from exc
        for quality in (82, 74, 66, 58, 50, 42, 34):
            output = io.BytesIO()
            image.save(output, format='JPEG', quality=quality, optimize=True)
            result = output.getvalue()
            if 0 < len(result) <= THUMB_MAX_BYTES:
                return result
        raise ValueError('Could not compress the WeChat thumbnail below the 10 MB upload limit.')

    @staticmethod
    def _inline_text(value: str) -> str:
        safe = html.escape(value, quote=False)
        pieces = safe.split('**')
        if len(pieces) > 1:
            safe = ''.join(
                piece if index % 2 == 0 else '<strong>' + piece + '</strong>'
                for index, piece in enumerate(pieces)
            )
        return safe

    @classmethod
    def _markdown_to_html(cls, title: str, body: str) -> str:
        blocks = [f'<h1>{html.escape(title, quote=False)}</h1>']
        paragraph: list[str] = []
        bullets: list[str] = []

        def flush_paragraph() -> None:
            if paragraph:
                blocks.append('<p>' + '<br/>'.join(cls._inline_text(x) for x in paragraph) + '</p>')
                paragraph.clear()

        def flush_bullets() -> None:
            if bullets:
                blocks.append('<ul>' + ''.join('<li>' + cls._inline_text(x) + '</li>' for x in bullets) + '</ul>')
                bullets.clear()

        for raw in str(body or '').splitlines():
            line = raw.strip()
            if not line:
                flush_paragraph()
                flush_bullets()
                continue
            heading = re.match(r'^#{1,3}\s+(.+)$', line)
            bullet = re.match(r'^(?:[-*]|\d+[.)])\s+(.+)$', line)
            quote = line.startswith('> ')
            if heading:
                flush_paragraph()
                flush_bullets()
                blocks.append('<h2>' + cls._inline_text(heading.group(1)) + '</h2>')
            elif bullet:
                flush_paragraph()
                bullets.append(bullet.group(1))
            elif quote:
                flush_paragraph()
                flush_bullets()
                blocks.append('<blockquote><p>' + cls._inline_text(line[2:]) + '</p></blockquote>')
            else:
                flush_bullets()
                paragraph.append(line)
        flush_paragraph()
        flush_bullets()
        return ''.join(blocks)

    @staticmethod
    def _digest(body: str) -> str:
        plain = re.sub(r'[*#>`_]', '', str(body or ''))
        plain = re.sub(r'\s+', ' ', plain).strip()
        return plain if len(plain) <= 120 else plain[:117].rstrip() + '...'

    async def publish(self, payload: CrosspostPayload) -> CrosspostResult:
        if not self.configured():
            return CrosspostResult(platform=self.platform, status='skipped')
        title = str(payload.title or '').strip()
        body = str(payload.text or '').strip()
        if not title or len(title) > self.max_title:
            return CrosspostResult(platform=self.platform, status='permanent', error='WeChat article titles must contain 1–64 characters.')
        if not body or len(body) > self.max_body:
            return CrosspostResult(platform=self.platform, status='permanent', error='WeChat article body is empty or too long.')
        _app_id, _app_secret, author = self._credentials()
        if len(author) > 16:
            return CrosspostResult(platform=self.platform, status='permanent', error='WeChat author name must be at most 16 characters.')
        image_path = str(payload.image_path or (payload.meta or {}).get('image_path') or '').strip()
        if not image_path:
            return CrosspostResult(platform=self.platform, status='skipped', error='WeChat Official Account articles require a cover image.')
        try:
            thumb_bytes = self._prepare_thumbnail(image_path)
        except ValueError as exc:
            return CrosspostResult(platform=self.platform, status='permanent', error=str(exc))

        content_html = self._markdown_to_html(title, body)
        digest = self._digest(body)
        async with httpx.AsyncClient(timeout=35.0, follow_redirects=False) as client:
            try:
                token = await self._get_access_token(client)
            except WeChatAPIError as exc:
                return self._api_error_result(exc)
            except Exception as exc:
                return CrosspostResult(platform=self.platform, status='failed', error=f'WeChat token request failed ({type(exc).__name__}).', retry_after_seconds=300)

            try:
                upload = await client.post(
                    f'{API_BASE}/cgi-bin/material/add_material',
                    params={'access_token': token, 'type': 'image'},
                    files={'media': ('tgfarm-cover.jpg', thumb_bytes, 'image/jpeg')},
                )
                media = self._check_response(upload.json())
                thumb_id = str(media.get('media_id') or '').strip()
                if not thumb_id:
                    return CrosspostResult(platform=self.platform, status='permanent', error='WeChat cover upload returned no media_id.')
            except WeChatAPIError as exc:
                return self._api_error_result(exc)
            except Exception as exc:
                return CrosspostResult(platform=self.platform, status='failed', error=f'WeChat cover upload failed ({type(exc).__name__}).', retry_after_seconds=300)

            article = {
                'article_type': 'news',
                'title': title,
                'author': author,
                'digest': digest,
                'content': content_html,
                'content_source_url': '',
                'thumb_media_id': thumb_id,
                'need_open_comment': 0,
                'only_fans_can_comment': 0,
            }
            try:
                draft_response = await client.post(
                    f'{API_BASE}/cgi-bin/draft/add',
                    params={'access_token': token},
                    json={'articles': [article]},
                )
                draft = self._check_response(draft_response.json())
                draft_id = str(draft.get('media_id') or '').strip()
                if not draft_id:
                    return CrosspostResult(platform=self.platform, status='unknown', error='WeChat draft/add returned no media_id.')
            except WeChatAPIError as exc:
                return self._api_error_result(exc)
            except Exception as exc:
                return CrosspostResult(platform=self.platform, status='unknown', error=f'WeChat draft creation outcome is unknown ({type(exc).__name__}); reconcile in MP admin before retrying.')

            try:
                submit_response = await client.post(
                    f'{API_BASE}/cgi-bin/freepublish/submit',
                    params={'access_token': token},
                    json={'media_id': draft_id},
                )
                submitted = self._check_response(submit_response.json())
                publish_id = str(submitted.get('publish_id') or '').strip()
                if not publish_id:
                    return CrosspostResult(platform=self.platform, status='unknown', remote_id=draft_id, error='WeChat accepted no publish_id; verify the draft in MP admin before retrying.')
            except WeChatAPIError as exc:
                return self._api_error_result(exc, draft_id=draft_id)
            except Exception as exc:
                return CrosspostResult(platform=self.platform, status='unknown', remote_id=draft_id, error=f'WeChat publish submission outcome is unknown ({type(exc).__name__}); do not retry before checking MP admin.')

            for _attempt in range(self.poll_attempts):
                try:
                    await asyncio.sleep(self.poll_delay_seconds)
                    status_response = await client.post(
                        f'{API_BASE}/cgi-bin/freepublish/get',
                        params={'access_token': token},
                        json={'publish_id': publish_id},
                    )
                    status_data = self._check_response(status_response.json())
                except Exception:
                    return CrosspostResult(platform=self.platform, status='scheduled', remote_id=publish_id, error='WeChat accepted the publish job; status polling is temporarily unavailable.')
                try:
                    publish_status = int(status_data.get('publish_status', -1))
                except (TypeError, ValueError):
                    publish_status = -1
                if publish_status == 0:
                    details = status_data.get('article_detail') or {}
                    items = details.get('item') or []
                    first = items[0] if items and isinstance(items[0], dict) else {}
                    article_id = str(first.get('article_id') or publish_id)
                    article_url = str(first.get('article_url') or '').strip()
                    return CrosspostResult(platform=self.platform, status='posted', remote_id=article_id, url=article_url if article_url.startswith('https://') else None)
                if publish_status in {2, 3, 4, 5, 6}:
                    return CrosspostResult(platform=self.platform, status='permanent', remote_id=publish_id, error=f'WeChat publish job ended with publish_status={publish_status}; inspect the Official Account publish center.')
            return CrosspostResult(platform=self.platform, status='scheduled', remote_id=publish_id, error='WeChat accepted the article; processing continues in the Official Account publish queue.')


__all__ = ['WeChatOfficialAccountAdapter']
