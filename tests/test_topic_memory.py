from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from tgfarm.db.models import Channel, Post
from tgfarm.db.session import session_scope


def _seed_history(*, now: datetime, channel_id: str | None = None) -> tuple[str, str]:
    channel_id = channel_id or "topic_chain_" + uuid4().hex[:10]
    public_id = uuid4().hex[:12]
    published = now - timedelta(days=9)
    with session_scope() as session:
        session.merge(Channel(id=channel_id, title="Kestrelion Lab", niche="science"))
        session.add(Post(
            public_id=public_id,
            channel_id=channel_id,
            hook="Kestrelion thermal reactor: 3 modules and 100 watts",
            body=(
                "Our archive described the Kestrelion thermal reactor and its "
                "cooling system. The published report said it requires 3 modules "
                "and consumes 100 watts under the old test conditions."
            ),
            status="published",
            published_at=published,
            created_at=published,
            angle="analysis",
            post_format="article",
            sources=[{
                "title": "Original Kestrelion report",
                "url": "https://archive.example.test/kestrelion",
                "source": "lab",
            }],
            claims=[{
                "claim": "Kestrelion thermal reactor requires 3 modules and 100 watts.",
                "source_url": "https://archive.example.test/kestrelion",
                "verified": True,
            }],
        ))
    return channel_id, public_id


def _current_item(now: datetime):
    published = now - timedelta(hours=6)
    return SimpleNamespace(
        source_id="kestrelion_lab",
        external_id="new-report-" + uuid4().hex[:8],
        title="Kestrelion thermal reactor update",
        text=(
            "The Kestrelion thermal reactor now requires 4 modules and 120 watts "
            "in the latest cooling test. Engineers also published a new coolant "
            "control procedure and described its test conditions."
        ),
        url="https://current.example.test/kestrelion-update",
        published_at=published,
        fetched_at=published,
    )


def test_topic_dossier_connects_published_timeline_to_newer_sources():
    from tgfarm.s4_generation.topic_memory import build_topic_dossier

    now = datetime(2026, 10, 28, 12, tzinfo=UTC)
    channel_id, public_id = _seed_history(now=now)
    source = _current_item(now)

    dossier = build_topic_dossier(
        "Kestrelion thermal reactor cooling",
        channel_id=channel_id,
        preferred_channel_id=channel_id,
        current_items=[source],
        now=now,
    )

    assert dossier["status"] == "ok"
    assert dossier["history_count"] == 1
    assert dossier["timeline"][0]["id"] == public_id
    assert dossier["timeline"][0]["angle"] == "analysis"
    assert dossier["updates_found"] == 1
    assert dossier["current_source_count"] == 1
    assert dossier["current_sources"][0]["url"] == source.url
    assert any(idea["kind"] == "update" for idea in dossier["continuation_ideas"])
    assert "Kestrelion thermal reactor" in dossier["brief"]
    assert "Не пересказывай прошлый пост" in dossier["brief"]


def test_topic_dossier_flags_numeric_difference_as_possible_not_proven_change():
    from tgfarm.s4_generation.topic_memory import build_topic_dossier

    now = datetime(2026, 10, 28, 12, tzinfo=UTC)
    channel_id, _public_id = _seed_history(now=now)
    dossier = build_topic_dossier(
        "Kestrelion thermal reactor cooling",
        channel_id=channel_id,
        preferred_channel_id=channel_id,
        current_items=[_current_item(now)],
        now=now,
    )

    checked = next(
        claim for claim in dossier["claim_checks"]
        if "100 watts" in claim["claim"]
    )
    assert checked["current_status"] == "possible_change"
    assert checked["historically_verified"] is True
    assert "метрик" in checked["current_note"]
    assert checked["current_sources"] == ["https://current.example.test/kestrelion-update"]


def test_topic_dossier_reads_recent_source_items_from_database():
    from tgfarm.db.models import RawItem
    from tgfarm.s4_generation.topic_memory import build_topic_dossier

    now = datetime(2026, 10, 28, 12, tzinfo=UTC)
    channel_id, _public_id = _seed_history(now=now)
    fetched = now - timedelta(hours=3)
    with session_scope() as session:
        session.add(RawItem(
            source_id="kestrelion_live_source",
            external_id="db-current-" + uuid4().hex[:8],
            url="https://db-current.example.test/kestrelion",
            title="Kestrelion thermal reactor update",
            text=(
                "Kestrelion thermal reactor now requires 4 modules and 120 watts "
                "in a recent cooling test. Engineers described a new coolant control procedure."
            ),
            lang="en",
            published_at=fetched,
            fetched_at=fetched,
        ))

    dossier = build_topic_dossier(
        "Kestrelion thermal reactor cooling",
        channel_id=channel_id,
        preferred_channel_id=channel_id,
        now=now,
    )
    assert dossier["current_source_count"] >= 1
    assert any("db-current.example.test/kestrelion" in (row.get("url") or "") for row in dossier["current_sources"])
    assert dossier["updates_found"] >= 1


def test_topic_dossier_does_not_claim_fresh_confirmation_when_no_current_items_exist():
    from tgfarm.s4_generation.topic_memory import build_topic_dossier

    now = datetime(2026, 10, 28, 12, tzinfo=UTC)
    channel_id, _public_id = _seed_history(now=now)
    dossier = build_topic_dossier(
        "Kestrelion thermal reactor cooling",
        channel_id=channel_id,
        preferred_channel_id=channel_id,
        current_items=[],
        now=now,
    )

    assert dossier["history_count"] == 1
    assert dossier["current_source_count"] == 0
    assert dossier["updates_found"] == 0
    assert dossier["claim_checks"][0]["current_status"] == "not_rechecked"
    assert any(idea["kind"] == "refresh" for idea in dossier["continuation_ideas"])


def test_topic_context_can_be_added_to_generation_prompt():
    from tgfarm.s4_generation.topic_memory import build_topic_context

    now = datetime.now(UTC)
    channel_id, _public_id = _seed_history(now=now)
    source = _current_item(now)
    block = build_topic_context([source], channel_id)

    assert "ДОСЬЕ ТЕМЫ" in block
    assert "ХРОНОЛОГИЯ" in block
    assert "ТРЕБОВАНИЯ К ПРОДОЛЖЕНИЮ" in block
    assert "текущий блок ИСТОЧНИКИ" in block


def test_topic_dossier_api_returns_history_for_authenticated_request():
    from fastapi.testclient import TestClient

    from tgfarm.core.apiauth import HEADER, ensure_token
    from tgfarm.api import create_app

    now = datetime.now(UTC)
    channel_id, public_id = _seed_history(now=now)
    with TestClient(create_app(with_scheduler=False), base_url="http://127.0.0.1") as client:
        response = client.get(
            "/api/memory/topic",
            params={"query": "Kestrelion thermal reactor cooling", "channel_id": channel_id},
            headers={HEADER: ensure_token()},
        )

    assert response.status_code == 200
    dossier = response.json()
    assert dossier["status"] == "ok"
    assert dossier["history_count"] == 1
    assert dossier["timeline"][0]["id"] == public_id


def test_topic_archive_pages_through_the_candidate_window_and_marks_truncation(monkeypatch):
    import tgfarm.s4_generation.topic_memory as topic_memory

    calls = []
    total = 425

    def fake_search(query, *, channel_id=None, preferred_channel_id=None, limit=20, offset=0):
        calls.append((limit, offset))
        end = min(total, offset + limit)
        return {
            "total": total,
            "results": [{"id": f"topic-post-{i}", "score": 0.5} for i in range(offset, end)],
        }

    monkeypatch.setattr(topic_memory, "search_archive", fake_search)
    result = topic_memory._search_topic_archive(
        "Kestrelion reactor",
        channel_id=None,
        preferred_channel_id=None,
        limit=300,
    )
    assert result["scanned"] == 300
    assert result["truncated"] is True
    assert calls == [(100, 0), (100, 100), (100, 200)]


def test_topic_dossier_handles_short_or_empty_queries_without_scanning_archive():
    from tgfarm.s4_generation.topic_memory import build_topic_dossier

    empty = build_topic_dossier("AI")
    assert empty["status"] == "query_too_short"
    assert empty["timeline"] == []
    assert empty["brief"] == ""
