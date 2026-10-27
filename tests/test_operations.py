"""Adaptive cadence, editorial priority and operational audit."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import delete, select

from tgfarm.db.models import AIUsage, Channel, Cluster, Post, RawItem
from tgfarm.db.session import session_scope


def _row(**kwargs):
    base = {"min_interval_s": 600, "idle_streak": 0, "failures": 0}
    base.update(kwargs)
    return SimpleNamespace(**base)


def _settings(**kwargs):
    base = {
        "source_adaptive_cadence": True,
        "source_idle_backoff_max": 6,
        "source_failure_backoff_max": 4,
        "ai_daily_budget": 30,
        "ai_policy": "selective",
        "provider_chain": ["longcat", "offline"],
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_effective_interval_backs_off_then_caps():
    from tgfarm.s1_sources import effective_interval

    settings = _settings()
    assert effective_interval(_row(), settings) == 600
    assert effective_interval(_row(idle_streak=2, failures=1), settings) == 4800
    assert effective_interval(_row(idle_streak=99, failures=99), settings) == 21_600
    assert effective_interval(
        _row(idle_streak=9), _settings(source_adaptive_cadence=False)
    ) == 600


def test_editorial_prefers_corroborated_story_and_cools_repeats():
    from tgfarm.editorial import queue

    suffix = uuid4().hex[:8]
    with session_scope() as session:
        first = Cluster(
            public_id=f"ed-one-{suffix}",
            label="one source",
            status="ready",
            heat=0.5,
            last_seen_at=datetime.now(UTC),
        )
        second = Cluster(
            public_id=f"ed-many-{suffix}",
            label="three sources",
            status="ready",
            heat=0.5,
            last_seen_at=datetime.now(UTC),
        )
        session.add_all([first, second])
        session.flush()
        for index in range(3):
            session.add(
                RawItem(
                    source_id=f"ed-src-{index}",
                    external_id=f"ed-{suffix}-{index}",
                    url=f"https://example.invalid/ed/{suffix}/{index}",
                    title=f"story {index}",
                )
            )
        session.flush()
        # The queries use cluster ids; re-query after flush.
        stored = session.execute(
            select(Cluster).where(Cluster.public_id.in_([first.public_id, second.public_id]))
        ).scalars().all()
        rows = session.execute(
            select(RawItem).where(RawItem.external_id.like(f"ed-{suffix}-%"))
        ).scalars().all()
        by_id = {cluster.public_id: cluster for cluster in stored}
        for item in rows:
            item.cluster_id = by_id[second.public_id].id
        session.add(
            Post(
                public_id=f"ed-post-{suffix}",
                channel_id="zen_misc_ru",
                cluster_public_id=second.public_id,
                status="gated",
            )
        )

    ranked = queue(limit=10)
    ids = [row["cluster_public_id"] for row in ranked]
    assert ids.index(first.public_id) < ids.index(second.public_id)
    second_row = next(row for row in ranked if row["cluster_public_id"] == second.public_id)
    assert second_row["cooling"] is True
    assert second_row["sources"] == 3


def test_content_audit_reports_production_and_ai_usage():
    from tgfarm.s8_analytics.audit import content_audit

    now = datetime.now(UTC)
    suffix = uuid4().hex[:8]
    channel_id = f"audit-{suffix}"
    with session_scope() as session:
        session.execute(delete(AIUsage))
        session.add(Channel(id=channel_id, title="Audit", platform="dzen"))
        session.add_all(
            [
                Post(
                    public_id=f"aud-rej-{suffix}",
                    channel_id=channel_id,
                    status="rejected",
                    reject_reason="tone: too dry",
                    angle="analysis",
                    post_format="article",
                    generation_provider="longcat",
                    created_at=now,
                ),
                Post(
                    public_id=f"aud-pub-{suffix}",
                    channel_id=channel_id,
                    status="published",
                    angle="news",
                    post_format="news",
                    generation_provider="offline",
                    published_at=now,
                    created_at=now,
                ),
            ]
        )
        source = RawItem(
            source_id=f"audit-src-{suffix}",
            external_id=f"audit-a-{suffix}",
            url=f"https://example.invalid/audit/{suffix}/a",
            title="audit item",
            fetched_at=now,
        )
        session.add(source)
        session.flush()
        session.add(
            RawItem(
                source_id=f"audit-src-{suffix}",
                external_id=f"audit-b-{suffix}",
                url=f"https://example.invalid/audit/{suffix}/b",
                title="audit duplicate",
                fetched_at=now,
                duplicate_of=source.id,
            )
        )
        session.add_all(
            [
                AIUsage(
                    stage="s4_draft",
                    provider="longcat",
                    model="LongCat-2.5-Preview",
                    charged=True,
                    used=True,
                    reason="used",
                    created_at=now,
                ),
                AIUsage(
                    stage="s5_tone",
                    charged=False,
                    used=False,
                    reason="fallback:offline",
                    created_at=now,
                ),
            ]
        )

    report = content_audit(1, channel_id=channel_id, now=now)
    assert report["posts"]["total"] == 2
    assert report["posts"]["by_status"] == {"published": 1, "rejected": 1}
    assert report["posts"]["rejection_reasons"][0]["reason"] == "tone"
    assert report["cadence"]["published"] == 1
    assert report["sources"]["duplicates"] >= 1
    assert report["ai"]["by_stage"]["s4_draft"]["used"] == 1
    assert report["ai"]["by_stage"]["s5_tone"]["fallbacks"] == 1
    assert report["ai"]["summary"]["charged"] == 1


def test_editorial_queue_prioritizes_news_and_ai_scores():
    from tgfarm.editorial import queue

    suffix = uuid4().hex[:8]
    now = datetime.now(UTC)
    
    # Isolate from clusters left by earlier editorial tests
    with session_scope() as session:
        ready_ids = [r[0] for r in session.execute(select(Cluster.public_id).where(Cluster.status == "ready")).all()]
        if ready_ids:
            session.execute(delete(Post).where(Post.cluster_public_id.in_(ready_ids)))
            session.execute(delete(RawItem).where(RawItem.cluster_id.in_(session.execute(select(Cluster.id).where(Cluster.status == "ready")).scalars().all())))
        session.execute(delete(Cluster).where(Cluster.status == "ready"))
    
    with session_scope() as session:
        news_cluster = Cluster(
            public_id=f"news-good-ai-{suffix}",
            label="News with good AI scores",
            status="ready",
            heat=0.6,
            last_seen_at=now,
        )
        session.add(news_cluster)
        session.flush()
        session.add(Post(
            public_id=f"post-news-good-ai-{suffix}",
            channel_id="zen_misc_ru",
            cluster_public_id=news_cluster.public_id,
            status="gated",
            angle="news",
            quality={"scores": {"relevance": 0.9, "quality": 0.8}},
            created_at=now
        ))

        # Cluster with analysis angle and average AI scores
        analysis_cluster = Cluster(
            public_id=f"analysis-avg-ai-{suffix}",
            label="Analysis with average AI scores",
            status="ready",
            heat=0.7,
            last_seen_at=now,
        )
        session.add(analysis_cluster)
        session.flush()
        session.add(Post(
            public_id=f"post-analysis-avg-ai-{suffix}",
            channel_id="zen_misc_ru",
            cluster_public_id=analysis_cluster.public_id,
            status="gated",
            angle="analysis",
            quality={"scores": {"relevance": 0.6, "quality": 0.7}},
            created_at=now
        ))

        # Cluster with news angle but no AI scores (offline fallback)
        news_offline_cluster = Cluster(
            public_id=f"news-offline-{suffix}",
            label="News from offline generation",
            status="ready",
            heat=0.5,
            last_seen_at=now,
        )
        session.add(news_offline_cluster)
        session.flush()
        session.add(Post(
            public_id=f"post-news-offline-{suffix}",
            channel_id="zen_misc_ru",
            cluster_public_id=news_offline_cluster.public_id,
            status="gated",
            angle="news",
            quality={"scores": {}},
            created_at=now
        ))

    ranked = queue(limit=10)
    # Expect news_cluster to be ranked higher than analysis_cluster due to news boost and good AI scores
    assert ranked[0]["cluster_public_id"] == news_cluster.public_id
    # Expect analysis_cluster to be ranked higher than news_offline_cluster due to higher heat and AI scores
    assert ranked[1]["cluster_public_id"] == analysis_cluster.public_id
    # Ensure the news_offline_cluster is still in the queue, even with lower scores
    assert ranked[2]["cluster_public_id"] == news_offline_cluster.public_id


def test_ai_policy_budget_handling_and_warnings(monkeypatch, caplog):
    from tgfarm.llm import policy
    from tgfarm.core.settings import get_settings, reset_settings_cache

    # Reset settings cache to apply test settings
    reset_settings_cache()

    # Patch get_settings to return a mock with a low budget
    mock_settings = _settings(ai_daily_budget=1, ai_policy="selective", online=True)
    monkeypatch.setattr("tgfarm.core.settings.get_settings", lambda: mock_settings)

    # Clear AIUsage to start with a clean slate for budget counting
    with session_scope() as session:
        session.execute(delete(AIUsage))
    
    caplog.set_level("WARNING")

    # First reservation should be allowed
    decision1 = policy.reserve("s4_draft", purpose="draft")
    assert decision1.allowed is True
    assert "AI: approaching budget limit" not in caplog.text # Should not warn yet
    
    # Second reservation should hit the budget limit and log a warning
    decision2 = policy.reserve("s4_draft", purpose="draft")
    assert decision2.allowed is False
    assert "AI: budget exhausted (1/1) for stage s4_draft" in caplog.text

    # Third reservation should also be disallowed and log a warning
    caplog.clear()
    decision3 = policy.reserve("s4_draft", purpose="draft")
    assert decision3.allowed is False
    assert "AI: budget exhausted (1/1) for stage s4_draft" in caplog.text

    # Finalize the first usage (simulating actual AI use)
    policy.finalize(decision1.usage_id, provider="longcat", model="LongCat-2.5-Preview", used=True)

    # Even after finalizing, budget remains exhausted if charged_today still reflects it
    # For this test, we assume charged_today reflects the original reservation counts.
    caplog.clear()
    decision4 = policy.reserve("s4_draft", purpose="draft")
    assert decision4.allowed is False
    assert "AI: budget exhausted (1/1) for stage s4_draft" in caplog.text


