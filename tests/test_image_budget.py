from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from sqlalchemy import delete

from tgfarm.db.models import ImageUsage
from tgfarm.db.session import session_scope


def test_parallel_image_reservations_cannot_exceed_daily_budget(monkeypatch):
    from tgfarm.llm import image_budget

    with session_scope() as session:
        session.execute(delete(ImageUsage))

    monkeypatch.setattr(
        image_budget,
        "get_settings",
        lambda: SimpleNamespace(image_daily_budget=2),
    )

    def reserve_one(index: int):
        return image_budget.reserve_image_call("test-provider", f"model-{index}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(reserve_one, range(10)))

    assert sum(allowed for _usage_id, allowed in results) == 2
    with session_scope() as session:
        rows = session.query(ImageUsage).all()
        assert sum(bool(row.charged) for row in rows) == 2
        assert sum(row.status == "blocked" for row in rows) == 8
        assert all(not row.charged for row in rows if row.status == "blocked")

    with session_scope() as session:
        session.execute(delete(ImageUsage))


def test_failed_image_attempt_still_counts_against_budget(monkeypatch):
    from tgfarm.llm import image_budget

    with session_scope() as session:
        session.execute(delete(ImageUsage))

    monkeypatch.setattr(
        image_budget,
        "get_settings",
        lambda: SimpleNamespace(image_daily_budget=1),
    )
    usage_id, allowed = image_budget.reserve_image_call("test-provider", "model-a")
    assert allowed and usage_id is not None

    image_budget.finish_image_call(usage_id, "failed", "TimeoutException")

    with session_scope() as session:
        row = session.get(ImageUsage, usage_id)
        assert row is not None
        assert row.status == "failed"
        assert row.error_kind == "TimeoutException"
        assert row.charged is True
        assert row.finished_at is not None

    _blocked_id, allowed = image_budget.reserve_image_call("test-provider", "model-b")
    assert allowed is False

    with session_scope() as session:
        session.execute(delete(ImageUsage))


@pytest.mark.asyncio
async def test_budgeted_provider_does_not_call_remote_when_daily_cap_is_exhausted(monkeypatch):
    from tgfarm.core.errors import ProviderUnavailable
    from tgfarm.llm import image_budget

    with session_scope() as session:
        session.execute(delete(ImageUsage))

    monkeypatch.setattr(
        image_budget,
        "get_settings",
        lambda: SimpleNamespace(image_daily_budget=0),
    )

    class FakeProvider:
        _model = "model"

        def __init__(self):
            self.called = False

        @image_budget.budgeted_image_call
        async def generate(self, prompt: str, negative: str = ""):
            self.called = True
            return prompt

    provider = FakeProvider()
    with pytest.raises(ProviderUnavailable, match="daily image generation budget exhausted"):
        await provider.generate("never sent")

    assert provider.called is False
    with session_scope() as session:
        row = session.query(ImageUsage).one()
        assert row.status == "blocked"
        assert row.charged is False
        session.execute(delete(ImageUsage))
