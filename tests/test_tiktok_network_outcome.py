"""TikTok's ambiguous-outcome branch must return a result, not raise.

`publish_tiktok_video` keeps the upload-safe branches: a lost connection before
any byte is sent is `failed`, a lost connection after it is `unknown`, and
neither may be retried blindly. The network branch logged through `log`, which
this module never created, so the branch that exists to protect a half-sent
upload crashed with NameError instead of describing the outcome.
"""

from __future__ import annotations

import httpx


async def test_network_failure_before_upload_returns_a_result(tmp_path, monkeypatch):
    from tgfarm import tiktok_oauth, tiktok_publish

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00" * 4096)

    async def fake_token(*, force_refresh: bool = False) -> str:
        return "token"

    async def fake_creator(*, access_token: str | None = None) -> dict:
        return {"privacy_level_options": ["SELF_ONLY"], "max_video_post_duration_sec": 0}

    monkeypatch.setattr(tiktok_oauth, "get_valid_tiktok_access_token", fake_token)
    monkeypatch.setattr(tiktok_oauth, "get_tiktok_creator_info", fake_creator)

    class FailingClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> bool:
            return False

        async def post(self, *args, **kwargs):
            raise httpx.ConnectTimeout("no route to host")

    monkeypatch.setattr(tiktok_publish.httpx, "AsyncClient", FailingClient)

    result = await tiktok_publish.publish_tiktok_video(
        video, caption="Подпись", privacy_level="SELF_ONLY"
    )

    assert result.status == "failed"
    assert "не ответил" in result.error
