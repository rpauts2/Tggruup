"""Regression coverage for native editorial metadata handling.

These tests use the public ``adapt_payload_with_ai`` entry point and replace
all policy/LLM dependencies locally; no credentials or network are involved.
"""
from __future__ import annotations

import asyncio
import copy
import socket
from types import SimpleNamespace

import pytest

from tgfarm.llm import policy
from tgfarm.platform_editorial import adapt_payload_with_ai


@pytest.fixture(autouse=True)
def _deny_network(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("Network access is forbidden in editorial regression tests")

    original_connect = socket.socket.connect
    original_socketpair = socket.socketpair

    def local_socketpair(*args, **kwargs):
        # Windows implements asyncio's internal wakeup pair via loopback TCP.
        # Permit only that synchronous stdlib operation, not application I/O.
        with monkeypatch.context() as context:
            context.setattr(socket.socket, "connect", original_connect)
            return original_socketpair(*args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket, "socketpair", local_socketpair)


def _payload(metadata: dict) -> SimpleNamespace:
    return SimpleNamespace(
        platform="bluesky",
        title="Source-backed title",
        text="A factual source-backed explanation. https://example.org/source",
        image_path=None,
        video_path=None,
        link="https://example.org/source",
        hashtags=["topic"],
        meta=metadata,
    )


def _enable_deterministic_ai(monkeypatch, response: dict) -> None:
    monkeypatch.setattr(policy, "should_use_ai", lambda *args, **kwargs: (True, "regression"))
    monkeypatch.setattr(
        policy,
        "reserve",
        lambda *args, **kwargs: SimpleNamespace(allowed=True, usage_id=17, reason="reserved"),
    )
    monkeypatch.setattr(policy, "finalize", lambda *args, **kwargs: None)

    class FakeLLM:
        async def complete_json(self, *args, **kwargs):
            return (
                response,
                SimpleNamespace(provider="regression-provider", model="regression-model", latency_ms=1),
            )

    monkeypatch.setattr("tgfarm.llm.client.get_llm", lambda: FakeLLM())
    monkeypatch.setattr(
        "tgfarm.core.settings.get_settings",
        lambda: SimpleNamespace(llm_platform_editor_model="regression-editor"),
    )


def test_successful_native_adaptation_preserves_user_metadata_and_nested_input(monkeypatch):
    user_metadata = {
        "post_public_id": "metadata-regression-success",
        "custom": {"labels": ["keep"], "options": {"tone": "calm"}},
    }
    original_metadata = copy.deepcopy(user_metadata)
    payload = _payload(user_metadata)
    _enable_deterministic_ai(
        monkeypatch,
        {"title": "Native title", "body": "Native body", "notes": []},
    )

    result = asyncio.run(adapt_payload_with_ai(payload, allow_ai=True))

    assert result["status"] == "ai_native"
    assert result["used_ai"] is True
    assert payload.meta["post_public_id"] == original_metadata["post_public_id"]
    assert payload.meta["custom"] == original_metadata["custom"]
    adaptation = payload.meta["editorial_adaptation"]
    expected_adaptation = {
        "status": "ai_native",
        "platform": "bluesky",
        "format": "короткий пост",
        "provider": "regression-provider",
        "model": "regression-model",
    }
    assert {key: adaptation[key] for key in expected_adaptation} == expected_adaptation
    assert user_metadata == original_metadata
    payload.meta["custom"]["labels"].append("result-only")
    payload.meta["custom"]["options"]["tone"] = "changed"
    assert user_metadata == original_metadata


@pytest.mark.parametrize(
    "rewritten_body, reason_fragment",
    [
        ("Invented 999 facts.", "числовые значения"),
        ("Unverified reference. https://unknown.example/new", "URL"),
    ],
)
def test_guardrail_rejection_preserves_metadata_and_is_not_success(
    monkeypatch, rewritten_body, reason_fragment
):
    user_metadata = {
        "post_public_id": "metadata-regression-guardrail",
        "custom": {"source": {"ids": ["a", "b"]}},
    }
    original_metadata = copy.deepcopy(user_metadata)
    payload = _payload(user_metadata)
    _enable_deterministic_ai(
        monkeypatch,
        {
            "title": "Native title",
            "body": rewritten_body,
            "notes": [],
        },
    )

    result = asyncio.run(adapt_payload_with_ai(payload, allow_ai=True))

    assert result["status"] == "deterministic_guardrail"
    assert result["used_ai"] is False
    assert result["status"] != "ai_native"
    adaptation = payload.meta["editorial_adaptation"]
    assert adaptation["status"] == "deterministic_guardrail"
    assert adaptation["provider"] == "regression-provider"
    assert adaptation["model"] == "regression-model"
    assert reason_fragment in adaptation["reason"]
    assert adaptation["reason"] == result["reason"]
    assert payload.title == "Source-backed title"
    assert "999" not in payload.text
    assert payload.meta["post_public_id"] == original_metadata["post_public_id"]
    assert payload.meta["custom"] == original_metadata["custom"]
    assert user_metadata == original_metadata
    payload.meta["custom"]["source"]["ids"].append("result-only")
    assert user_metadata == original_metadata


def test_deterministic_fallback_keeps_metadata_and_nested_values(monkeypatch):
    user_metadata = {
        "post_public_id": "metadata-regression-fallback",
        "custom": {"nested": {"enabled": True}},
    }
    original_metadata = copy.deepcopy(user_metadata)
    payload = _payload(user_metadata)
    monkeypatch.setattr(policy, "should_use_ai", lambda *args, **kwargs: (True, "regression"))
    monkeypatch.setattr(
        policy,
        "reserve",
        lambda *args, **kwargs: SimpleNamespace(allowed=True, usage_id=18, reason="reserved"),
    )
    monkeypatch.setattr(policy, "finalize", lambda *args, **kwargs: None)

    class BrokenLLM:
        async def complete_json(self, *args, **kwargs):
            raise RuntimeError("offline provider unavailable")

    monkeypatch.setattr("tgfarm.llm.client.get_llm", lambda: BrokenLLM())
    monkeypatch.setattr(
        "tgfarm.core.settings.get_settings",
        lambda: SimpleNamespace(llm_platform_editor_model="regression-editor"),
    )

    result = asyncio.run(adapt_payload_with_ai(payload, allow_ai=True))

    assert result["status"] == "deterministic_fallback"
    assert result["used_ai"] is False
    assert payload.meta == original_metadata
    assert payload.meta["custom"] == original_metadata["custom"]
    payload.meta["custom"]["nested"]["enabled"] = False
    assert user_metadata == original_metadata


def test_settings_failure_after_reserve_releases_budget(monkeypatch):
    """A config error after budget reservation must not leak the reserved usage."""
    payload = _payload({"post_public_id": "metadata-regression-settings"})
    monkeypatch.setattr(policy, "should_use_ai", lambda *args, **kwargs: (True, "regression"))
    monkeypatch.setattr(
        policy,
        "reserve",
        lambda *args, **kwargs: SimpleNamespace(allowed=True, usage_id=19, reason="reserved"),
    )
    finalized = []

    def fake_finalize(usage_id, **kwargs):
        finalized.append((usage_id, bool(kwargs.get("used"))))

    monkeypatch.setattr(policy, "finalize", fake_finalize)

    def broken_settings():
        raise RuntimeError("settings store unavailable")

    monkeypatch.setattr("tgfarm.core.settings.get_settings", broken_settings)

    result = asyncio.run(adapt_payload_with_ai(payload, allow_ai=True))

    assert result["status"] == "deterministic_fallback"
    assert result["used_ai"] is False
    assert finalized == [(19, False)]
    assert payload.title == "Source-backed title"
    assert "https://example.org/source" in payload.text
    assert payload.meta == {"post_public_id": "metadata-regression-settings"}
