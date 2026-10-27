"""OpenCode Zen routing, JSON extraction and the image provider contract."""

from __future__ import annotations

import json
import time

import pytest

from tgfarm.core.errors import ProviderUnavailable
from tgfarm.llm.extract import extract_json, extract_object, json_summary
from tgfarm.llm.images import NEGATIVE, build_prompt, get_provider
from tgfarm.llm.opencode import (
    DEFAULT_MODEL,
    FAST_MODEL,
    QUALITY_MODEL,
    OpenCodeZen,
    api_family,
    is_paid,
)


# ------------------------------------------------------------ family routing
@pytest.mark.parametrize(
    ("model", "family"),
    [
        ("gpt-5.5", "openai_responses"),
        ("gpt-6.1-sol", "openai_responses"),
        ("o3-mini", "openai_responses"),
        ("claude-sonnet-4-6", "anthropic"),
        ("claude-haiku-4-5", "anthropic"),
        ("qwen3.8-max", "anthropic"),
        ("gemini-3.1-pro", "gemini"),
        ("gemini-3.5-flash-lite", "gemini"),
        ("kimi-k2.6", "openai_chat"),
        ("glm-5.1", "openai_chat"),
        ("deepseek-v4-pro", "openai_chat"),
        ("space-bunny-free", "openai_chat"),
        ("", "openai_chat"),
    ],
)
def test_api_family_routing(model, family):
    assert api_family(model) == family


def test_free_models_are_recognised():
    assert not is_paid("space-bunny-free")
    assert not is_paid("glm-5.1-flash-free")
    assert is_paid("claude-sonnet-4-6")


def test_model_constants_are_distinct():
    assert len({DEFAULT_MODEL, FAST_MODEL, QUALITY_MODEL}) >= 2


@pytest.mark.asyncio
async def test_zen_is_skipped_without_a_token():
    """Zen answers 403 unauthenticated even for `-free` models, so the provider
    must report itself unavailable instead of burning a request per call."""
    assert not await OpenCodeZen("", "claude-sonnet-4-6", 30.0).available()


@pytest.mark.asyncio
async def test_zen_is_available_with_a_token():
    assert await OpenCodeZen("sk-test", "claude-sonnet-4-6", 30.0).available()


@pytest.mark.asyncio
async def test_zen_goes_down_after_a_failure():
    provider = OpenCodeZen("sk-test", "claude-sonnet-4-6", 30.0)
    provider._down = time.monotonic() + 300
    assert not await provider.available()


# ------------------------------------------------------------ response shapes
def test_extracts_anthropic_shape():
    payload = {"content": [{"type": "text", "text": "привет"}]}
    assert OpenCodeZen._extract(payload, "anthropic") == "привет"


def test_extracts_openai_responses_shape():
    payload = {
        "output": [
            {"content": [{"type": "output_text", "text": "часть1"}]},
            {"content": [{"type": "output_text", "text": " часть2"}]},
        ]
    }
    assert OpenCodeZen._extract(payload, "openai_responses") == "часть1\n часть2"


def test_extracts_openai_responses_flat_field():
    assert OpenCodeZen._extract({"output_text": "быстро"}, "openai_responses") == "быстро"


def test_extracts_gemini_shape():
    payload = {"candidates": [{"content": {"parts": [{"text": "ок"}]}}]}
    assert OpenCodeZen._extract(payload, "gemini") == "ок"


def test_extracts_openai_chat_shape():
    payload = {"choices": [{"message": {"content": "да"}}]}
    assert OpenCodeZen._extract(payload, "openai_chat") == "да"


def test_extracts_openai_chat_content_parts():
    payload = {"choices": [{"message": {"content": [{"text": "a"}, {"text": "b"}]}}]}
    assert OpenCodeZen._extract(payload, "openai_chat") == "a\nb"


@pytest.mark.parametrize(
    "payload",
    [
        {"choices": []},
        {"candidates": []},
        {"output": []},
        {"content": []},
        {},
    ],
)
def test_extract_is_total(payload):
    """A malformed or empty response must not raise inside the extractor."""
    for family in ("anthropic", "openai_responses", "gemini", "openai_chat"):
        OpenCodeZen._extract(payload, family)


# ------------------------------------------------------------------- json
def test_extract_json_plain():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_from_fenced_block():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_embedded_in_prose():
    text = 'Вот результат:\n{"hook": "x", "body": "y"}\nГотово.'
    assert extract_object(text) == {"hook": "x", "body": "y"}


def test_extract_json_closes_truncated_tail():
    """Output cut short by max_tokens still carries the fields that finished."""
    assert extract_json('{"a": 1, "b": 2') == {"a": 1, "b": 2}


def test_extract_json_drops_a_dangling_key():
    assert extract_json('{"hook": "заголовок", "body":') == {"hook": "заголовок"}


def test_extract_json_refuses_a_cut_string():
    """A fragment with no completed value is worse than a provider failover:
    an empty draft would pass through as a post with no hook."""
    with pytest.raises(ProviderUnavailable):
        extract_json('{"hook": "загол')


def test_extract_json_rejects_empty_fragment():
    with pytest.raises(ProviderUnavailable):
        extract_json('{"a": ')


def test_extract_json_rejects_non_json():
    with pytest.raises(ProviderUnavailable):
        extract_json("вообще не json")


def test_extract_object_rejects_array():
    with pytest.raises(ProviderUnavailable):
        extract_object("[1, 2, 3]")


def test_json_summary_degrades_gracefully():
    assert json_summary('{"a":1}') == json.dumps({"a": 1}, ensure_ascii=False)
    assert json_summary("мусор") == "мусор"


# ----------------------------------------------------------------- images
def test_image_provider_absent_without_config():
    """No configuration means the template renderer stays in charge."""
    assert get_provider() is None


def test_prompt_forbids_text_in_generated_art():
    """Image models put lettering on everything unless told not to."""
    prompt = build_prompt("Заголовок", "Подзаголовок", "tech", {"accent": "#4C8DFF"})
    lowered = prompt.lower()
    # Формулировка запрета может меняться, сам запрет — часть контракта.
    assert "no readable text" in lowered or "no lettering" in lowered
    # the negative prompt lists what must not appear, so bare terms are correct
    negative = NEGATIVE.lower()
    assert "text" in negative and "letters" in negative and "watermark" in negative


def test_prompt_uses_persona_palette():
    prompt = build_prompt("X", "Y", "crypto", {"accent": "#F5A524", "bg": "#14100A"})
    assert "#F5A524" in prompt
    assert "#14100A" in prompt


def test_prompt_is_niche_specific():
    crypto = build_prompt("X", "Y", "crypto", {})
    health = build_prompt("X", "Y", "health", {})
    assert crypto != health