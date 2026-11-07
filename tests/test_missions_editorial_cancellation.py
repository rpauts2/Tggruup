"""Cancellation must release the editorial AI reservation and propagate."""
import asyncio
from types import SimpleNamespace

import pytest

from tgfarm.llm import policy
from tgfarm.platform_editorial import adapt_payload_with_ai


def test_cancellation_closes_reservation(monkeypatch):
    finalized = []
    monkeypatch.setattr(policy, "should_use_ai", lambda *a, **k: (True, "test"))
    monkeypatch.setattr(policy, "reserve", lambda *a, **k: SimpleNamespace(allowed=True, usage_id=77))
    monkeypatch.setattr(policy, "finalize", lambda *a, **k: finalized.append((a, k)))
    monkeypatch.setattr(
        "tgfarm.core.settings.get_settings",
        lambda: SimpleNamespace(llm_platform_editor_model=""),
    )

    class CancelledLLM:
        async def complete_json(self, *a, **k):
            raise asyncio.CancelledError

    monkeypatch.setattr("tgfarm.llm.client.get_llm", lambda: CancelledLLM())
    payload = SimpleNamespace(platform="bluesky", title="Source title", text="Source factual text.",
                              image_path=None, video_path=None, link=None, hashtags=[], meta={"keep": True})
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(adapt_payload_with_ai(payload, allow_ai=True))
    assert len(finalized) == 1
    assert finalized[0][0] == (77,)
    assert finalized[0][1]["used"] is False
    assert payload.meta == {"keep": True}
