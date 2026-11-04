"""Auth checks must separate "rejected" from "could not check right now".

Real incident (2026-11-04, farm v18): a VK rate-limit answer at startup was
recorded as ``authorized=False`` and raised the operator alert "переподключи
аккаунт" for a token that worked.  These tests pin the three outcomes:
rejected (False), unknown (None), confirmed (True).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from tgfarm import platform_auth


class _Response:
    def __init__(self, status_code: int, payload) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


class _Client:
    def __init__(self, response: _Response) -> None:
        self._response = response

    async def get(self, url, params=None):  # noqa: ANN001, ANN201 - httpx stand-in
        return self._response


def _json(response: _Response):
    return asyncio.run(platform_auth._response_json(_Client(response), "https://example.org/check"))


def test_rate_limit_and_server_errors_are_not_auth_rejections():
    for status in (408, 425, 429, 500, 502, 503):
        with pytest.raises(platform_auth.CheckUnavailable):
            _json(_Response(status, {}))


def test_auth_rejection_stays_a_definitive_failure():
    for status in (401, 403):
        with pytest.raises(RuntimeError) as caught:
            _json(_Response(status, {}))
        assert not isinstance(caught.value, platform_auth.CheckUnavailable)


def test_unknown_status_code_is_still_a_failure():
    with pytest.raises(RuntimeError) as caught:
        _json(_Response(404, {}))
    assert not isinstance(caught.value, platform_auth.CheckUnavailable)


def _patch_platform(monkeypatch, payload):
    settings = SimpleNamespace(vk_access_token="token", vk_group_id="-1")
    monkeypatch.setattr(platform_auth, "get_settings", lambda: settings)

    async def fake_json(client, url, *, params=None):  # noqa: ANN001, ANN202
        return payload

    monkeypatch.setattr(platform_auth, "_response_json", fake_json)
    monkeypatch.setattr(
        platform_auth,
        "record_auth_check",
        lambda *args, **kwargs: {"checked_at": "2026-11-04T19:13:00+00:00"},
    )


def test_vk_flood_control_is_unknown_not_rejected(monkeypatch):
    _patch_platform(
        monkeypatch,
        {"error": {"error_code": 6, "error_msg": "Too many requests per second"}},
    )
    result = asyncio.run(platform_auth.check_platform_auth("vk"))
    assert result["authorized"] is None
    assert "не отказ токена" in result["detail"]
    assert result["ok"] is None


def test_vk_invalid_token_is_rejected(monkeypatch):
    _patch_platform(
        monkeypatch,
        {"error": {"error_code": 5, "error_msg": "User authorization failed"}},
    )
    result = asyncio.run(platform_auth.check_platform_auth("vk"))
    assert result["authorized"] is False
    assert "VK отклонил запрос" in result["detail"]
    assert "код 5" in result["detail"]


def test_vk_success_is_confirmed(monkeypatch):
    _patch_platform(monkeypatch, {"response": [{"name": "Всякая всячина"}]})
    result = asyncio.run(platform_auth.check_platform_auth("vk"))
    assert result["authorized"] is True
    assert result["account"] == "Всякая всячина"


def test_vk_groups_payload_shapes():
    # VK 5.199 wraps the list in an object; older versions answered a bare list.
    assert platform_auth._vk_groups({"groups": [{"name": "A"}]}) == [{"name": "A"}]
    assert platform_auth._vk_groups([{"name": "A"}]) == [{"name": "A"}]
    assert platform_auth._vk_groups(None) == []
    assert platform_auth._vk_groups({"groups": "broken"}) == []


def test_vk_object_payload_confirms_access(monkeypatch):
    # The live bug: reading only the list shape raised KeyError(0), which was
    # stored as authorized=False and produced a false "reconnect VK" alert.
    _patch_platform(monkeypatch, {"response": {"groups": [{"name": "Всякая всячина"}]}})
    result = asyncio.run(platform_auth.check_platform_auth("vk"))
    assert result["authorized"] is True
    assert result["account"] == "Всякая всячина"
