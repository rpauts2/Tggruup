"""Branding tick smoke test."""

from __future__ import annotations

from pathlib import Path

import pytest

from tgfarm.s9_growth import branding


class _Settings:
    def __init__(self, root: Path):
        self._root = root
        self.vk_access_token = "token"
        self.vk_group_id = "-123"

    def data_root(self) -> Path:
        return self._root


@pytest.mark.asyncio
async def test_run_branding_tick_rotates_and_saves_state(tmp_path, monkeypatch):
    # Fake settings
    monkeypatch.setattr(branding, "get_settings", lambda: _Settings(tmp_path))

    # Fake cover generator
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 10)

    def _gen(title, subtitle, seed=None):
        return cover

    monkeypatch.setattr(branding, "generate_cover", _gen)

    # Fake VK requests: upload server + save
    async def _vk_request(method, data):
        if method == "photos.getOwnerCoverPhotoUploadServer":
            return {"response": {"upload_url": "http://example/upload"}}
        if method == "photos.saveOwnerCoverPhoto":
            return {"response": 1}
        if method == "groups.setSettings":
            return {"response": 1}
        return {}

    monkeypatch.setattr(branding, "_vk_request", _vk_request)

    # Fake httpx client for upload
    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, files=None, timeout=None):
            class _Resp:
                def json(self):
                    return {"hash": "h", "photo": "p"}
            return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    result = await branding.run_branding_tick()
    assert result["rotated"] is True
    assert (tmp_path / "branding_state.json").exists()