from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select

from tgfarm.db.models import Channel, Post
from tgfarm.db.session import session_scope
from tgfarm.history_import import (
    HistoricalPublication,
    import_history,
    load_history_export,
    parse_history_payload,
)


def test_parse_telegram_desktop_export_keeps_text_entities_and_ignores_service_messages():
    payload = {
        "chats": {
            "list": [
                {
                    "name": "Архив канала",
                    "type": "public_channel",
                    "messages": [
                        {
                            "id": 41,
                            "type": "message",
                            "date": "2024-01-02T12:30:00",
                            "text": [
                                {"type": "plain", "text": "Исторический пост"},
                                {"type": "bold", "text": " с продолжением"},
                            ],
                        },
                        {"id": 42, "type": "service", "date": "2024-01-03T10:00:00", "text": "joined"},
                    ],
                }
            ]
        }
    }

    rows = parse_history_payload(payload)
    assert len(rows) == 1
    assert rows[0].platform == "telegram"
    assert rows[0].source_channel == "Архив канала"
    assert rows[0].external_id == "41"
    assert rows[0].text == "Исторический пост с продолжением"
    assert rows[0].published_at == datetime(2024, 1, 2, 12, 30, tzinfo=UTC)


def test_parse_vk_wall_response_infers_permalink_and_unix_date():
    rows = parse_history_payload(
        {
            "response": {
                "items": [
                    {
                        "id": 55,
                        "owner_id": -123,
                        "date": 1_700_000_000,
                        "text": "Старый пост ВК",
                    }
                ]
            }
        }
    )
    assert len(rows) == 1
    assert rows[0].platform == "vk"
    assert rows[0].url == "https://vk.com/wall-123_55"
    assert rows[0].published_at == datetime.fromtimestamp(1_700_000_000, tz=UTC)


def test_load_csv_export_supports_utf8_bom_and_common_columns(tmp_path):
    path = tmp_path / "history.csv"
    path.write_text(
        "\ufeffplatform,external_id,published_at,title,text,url\n"
        "dzen,article-1,2024-06-01T09:00:00,Заголовок,Текст статьи,https://example.com/a\n",
        encoding="utf-8",
    )

    rows = load_history_export(path)
    assert len(rows) == 1
    assert rows[0].platform == "dzen"
    assert rows[0].external_id == "article-1"
    assert rows[0].title == "Заголовок"
    assert rows[0].text == "Текст статьи"
    assert rows[0].url == "https://example.com/a"


def test_import_is_preview_only_by_default_then_idempotent_when_applied():
    channel_id = "history_import_" + uuid4().hex[:10]
    with session_scope() as session:
        session.add(Channel(id=channel_id, title="Исторический архив", platform="vk"))

    rows = parse_history_payload(
        [
            {
                "platform": "vk",
                "id": "post-001",
                "date": "2024-04-15T11:00:00Z",
                "title": "Заголовок старого поста",
                "text": "Основной текст публикации.",
                "url": "https://example.com/post-001",
            }
        ]
    )

    preview = import_history(rows, channel_id=channel_id)
    assert preview["mode"] == "dry_run"
    assert preview["planned_imports"] == 1
    assert preview["imported"] == 0
    assert preview["publication_enabled"] is False
    with session_scope() as session:
        assert session.execute(
            select(Post).where(Post.channel_id == channel_id)
        ).scalars().all() == []

    applied = import_history(rows, channel_id=channel_id, apply=True)
    assert applied["mode"] == "apply"
    assert applied["imported"] == 1

    with session_scope() as session:
        post = session.execute(
            select(Post).where(Post.channel_id == channel_id)
        ).scalar_one()
        assert post.status == "published"
        assert post.scheduled_at is None
        assert post.generation_provider == "history_import"
        assert post.quality["imported_history"] is True
        assert post.sources[0]["url"] == "https://example.com/post-001"
        assert post.published_at is not None
        assert post.published_at.replace(tzinfo=UTC) == datetime(2024, 4, 15, 11, tzinfo=UTC)

    repeated = import_history(rows, channel_id=channel_id, apply=True)
    assert repeated["imported"] == 0
    assert repeated["skipped_duplicates"] == 1


def test_import_detects_exact_text_duplicate_even_if_external_id_changed():
    channel_id = "history_dupe_" + uuid4().hex[:10]
    with session_scope() as session:
        session.add(Channel(id=channel_id, title="Дубликаты", platform="telegram"))

    timestamp = datetime(2023, 12, 1, tzinfo=UTC)
    rows = [
        HistoricalPublication(
            platform="telegram",
            source_channel="old",
            external_id="1",
            title="Тема",
            text="Тот же текст поста.",
            published_at=timestamp,
            url="",
        ),
        HistoricalPublication(
            platform="telegram",
            source_channel="old",
            external_id="different-id",
            title="Тема",
            text="Тот же текст поста.",
            published_at=timestamp,
            url="",
        ),
    ]

    preview = import_history(rows, channel_id=channel_id)
    assert preview["planned_imports"] == 1
    assert preview["skipped_duplicates"] == 1


def test_import_rejects_unknown_channel():
    import pytest

    row = HistoricalPublication(
        platform="generic",
        source_channel="",
        external_id="1",
        title="Заголовок",
        text="Текст",
        published_at=None,
        url="",
    )
    with pytest.raises(ValueError, match="не найден"):
        import_history([row], channel_id="no_such_channel_" + uuid4().hex[:8])



def test_fetch_telegram_history_uses_read_only_session_and_returns_oldest_first(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from tgfarm import tguser
    from tgfarm.history_import import fetch_telegram_history

    old = SimpleNamespace(
        id=10,
        message="Самый ранний пост",
        date=datetime(2023, 1, 1, tzinfo=UTC),
    )
    newest = SimpleNamespace(
        id=20,
        message="Следующий пост",
        date=datetime(2023, 1, 2, tzinfo=UTC),
    )
    channel = SimpleNamespace(id=777, title="Мой канал", username="my_public_channel")

    class FakeClient:
        entered = False
        exited = False
        requested = None

        async def __aenter__(self):
            self.entered = True
            return self

        async def __aexit__(self, *_args):
            self.exited = True
            return False

        async def get_entity(self, chat):
            assert chat == "my_public_channel"
            return channel

        def iter_messages(self, entity, *, limit, reverse):
            self.requested = (entity, limit, reverse)

            async def _messages():
                yield old
                yield newest

            return _messages()

    fake = FakeClient()

    async def fake_connect(read_only=True):
        assert read_only is True
        return fake

    monkeypatch.setattr(tguser, "connect", fake_connect)
    rows = asyncio.run(fetch_telegram_history("https://t.me/my_public_channel/123", limit=123))

    assert fake.entered and fake.exited
    assert fake.requested == (channel, 123, True)
    assert [row.external_id for row in rows] == ["10", "20"]
    assert rows[0].source_channel == "Мой канал"
    assert rows[0].url == "https://t.me/my_public_channel/10"
    assert rows[1].published_at == datetime(2023, 1, 2, tzinfo=UTC)


def test_fetch_telegram_history_rejects_unbounded_limit():
    import asyncio
    import pytest

    from tgfarm.history_import import fetch_telegram_history

    with pytest.raises(ValueError, match="от 1 до 5000"):
        asyncio.run(fetch_telegram_history("channel", limit=5001))
