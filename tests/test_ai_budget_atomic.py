from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from sqlalchemy import delete

from tgfarm.db.models import AIUsage
from tgfarm.db.session import session_scope


def test_parallel_ai_reservations_cannot_exceed_daily_budget(monkeypatch):
    from tgfarm.llm import policy

    with session_scope() as session:
        session.execute(delete(AIUsage))

    monkeypatch.setattr(
        policy,
        "get_settings",
        lambda: SimpleNamespace(
            online=True,
            ai_policy="always",
            provider_chain=["openai", "offline"],
            ai_daily_budget=3,
        ),
    )

    def reserve_one(index: int):
        return policy.reserve(
            "s4_draft",
            purpose=f"parallel-test-{index}",
            provider="openai",
            model="test-model",
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        decisions = list(pool.map(reserve_one, range(12)))

    assert sum(decision.allowed for decision in decisions) == 3
    assert all(decision.usage_id is not None for decision in decisions)

    with session_scope() as session:
        rows = session.query(AIUsage).all()
        charged = [row for row in rows if row.charged]
        refused = [row for row in rows if row.reason == "budget exhausted (3/day)"]

    assert len(charged) == 3
    assert len(refused) == 9
    assert all(not row.charged and not row.used for row in refused)

    # Keep the shared test database clean for unrelated AI usage/report tests.
    with session_scope() as session:
        session.execute(delete(AIUsage))
