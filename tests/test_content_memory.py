from datetime import UTC, datetime
from uuid import uuid4

from tgfarm.db.models import Channel, Post
from tgfarm.db.session import session_scope
from tgfarm.llm.embedding import get_embedder
from tgfarm.db.vector import pack


def _published_post(channel_id: str, hook: str, body: str, *, public_id: str | None = None) -> str:
    public_id = public_id or uuid4().hex[:12]
    now = datetime.now(UTC)
    with session_scope() as session:
        session.merge(Channel(id=channel_id, title=f"Канал {channel_id}", niche="tech"))
        session.add(Post(
            public_id=public_id,
            channel_id=channel_id,
            hook=hook,
            body=body,
            status="published",
            published_at=now,
            created_at=now,
            sources=[{"title": "Источник первичного материала", "url": "https://example.com/source"}],
            claims=[{"claim": "Подтверждённый факт из публикации", "source_url": "https://example.com/source", "verified": True}],
            embedding=pack(get_embedder().embed(f"{hook}\n{body}")),
        ))
    return public_id


def test_archive_searches_old_publications_and_returns_full_provenance():
    from tgfarm.s4_generation.memory import rebuild_archive_index, search_archive

    public_id = _published_post(
        "memory_archive_a",
        "Новая архитектура квантового процессора",
        "Компания представила экспериментальный квантовый процессор Aurora-X. В публикации разобраны устройство, ограничения и первичные результаты.",
    )
    rebuilt = rebuild_archive_index()
    assert rebuilt["available"] is True
    assert rebuilt["complete"] is True

    result = search_archive("Aurora-X квантовый процессор ограничения", limit=10)
    found = next(item for item in result["results"] if item["id"] == public_id)
    assert found["body"].startswith("Компания представила")
    assert found["sources"][0]["url"] == "https://example.com/source"
    assert found["claims"][0]["verified"] is True
    assert result["index"]["complete"] is True


def test_fts_triggers_keep_memory_in_sync_after_a_post_is_published_or_edited():
    from tgfarm.s4_generation.memory import search_archive

    public_id = uuid4().hex[:12]
    now = datetime.now(UTC)
    with session_scope() as session:
        session.merge(Channel(id="memory_archive_b", title="B", niche="tech"))
        session.add(Post(
            public_id=public_id, channel_id="memory_archive_b", hook="Draft heading",
            body="Draft body text", status="draft", created_at=now,
        ))

    # First query builds/backfills the search index, then a model update runs the DB trigger.
    search_archive("Draft heading", limit=10)
    with session_scope() as session:
        row = session.query(Post).filter_by(public_id=public_id).one()
        row.status = "published"
        row.published_at = datetime.now(UTC)
        row.hook = "Полностью новый орбитальный двигатель"
        row.body = "Новый двигатель проверили в вакуумной камере."
    result = search_archive("орбитальный двигатель вакуумной камере", limit=10)
    assert any(item["id"] == public_id for item in result["results"])


def test_prompt_memory_warns_against_reusing_archived_facts_as_evidence():
    from types import SimpleNamespace

    from tgfarm.s4_generation.memory import build_prompt_memory

    _published_post(
        "memory_archive_c",
        "Aurora-X и новое охлаждение",
        "Aurora-X использует экспериментальную систему охлаждения для квантового процессора.",
    )
    block = build_prompt_memory([
        SimpleNamespace(
            title="Aurora-X: система охлаждения",
            text="Разработчики представили новое охлаждение для процессора Aurora-X.",
        )
    ], "memory_archive_c")
    assert "АРХИВ ПРЕДЫДУЩИХ ПУБЛИКАЦИЙ" in block
    assert "НЕ источник новых фактов" in block
    assert "текущего блока ИСТОЧНИКИ" in block
    assert "команды" in block


def test_archive_duplicate_guard_finds_exact_republication():
    from tgfarm.s4_generation.memory import find_near_duplicate

    hook = "Aurora-X получил новую систему охлаждения"
    body = "Инженеры представили экспериментальную систему охлаждения квантового процессора Aurora-X. Она снижает нагрев при длительных вычислениях."
    _published_post("memory_archive_d", hook, body)

    vec = get_embedder().embed(f"{hook}\n{body}")
    match = find_near_duplicate(f"{hook}\n{body}", "memory_archive_d", vec)
    assert match is not None
    assert match["method"] in {"embedding", "lexical"}
    assert match["similarity"] >= 0.82


def test_archive_duplicate_guard_does_not_reject_a_different_story():
    from tgfarm.s4_generation.memory import find_near_duplicate

    _published_post(
        "memory_archive_e",
        "Aurora-X получил новую систему охлаждения",
        "Инженеры представили экспериментальную систему охлаждения квантового процессора Aurora-X.",
    )
    unrelated = "В городе открыли новый парк с велодорожками, фонтаном и площадками для детей."
    assert find_near_duplicate(unrelated, "memory_archive_e", get_embedder().embed(unrelated)) is None
