"""Discovery of top public Telegram communities.

The list is dynamic, so the only thing worth pinning is which chats are allowed
into it: our own channels and groups must never come back as upstream material,
otherwise the farm starts harvesting its own posts.
"""

from __future__ import annotations

import pytest

from tgfarm.s1_sources import telegram_top
from tgfarm.tguser import TgChat

OWN_CHANNEL_ID = -1001111111111


class _Settings:
    telegram_top_groups_enabled = True
    telegram_top_groups_limit = 10
    telegram_top_group_min_participants = 1000

    def __init__(self, channel_ids: list[int] | None = None) -> None:
        self._channel_ids = list(channel_ids or [])

    def channel_ids(self) -> list[int]:
        return list(self._channel_ids)


class _Config:
    sources: list = []


def _install(monkeypatch, chats: list[TgChat], channel_ids: list[int] | None = None) -> None:
    monkeypatch.setattr(telegram_top, "get_settings", lambda: _Settings(channel_ids))
    monkeypatch.setattr(telegram_top, "get_config", lambda: _Config())
    monkeypatch.setattr(telegram_top, "_list_chats_sync", lambda: list(chats))


def _chat(chat_id: int, username: str, participants: int = 50_000) -> TgChat:
    return TgChat(
        id=chat_id,
        title=f"title {username}",
        username=username,
        kind="channel",
        participants=participants,
    )


def test_our_own_publishing_channel_is_not_offered_as_a_source(monkeypatch):
    _install(
        monkeypatch,
        [_chat(OWN_CHANNEL_ID, "own_channel"), _chat(-1002222222222, "elsewhere")],
        channel_ids=[OWN_CHANNEL_ID],
    )

    picked = telegram_top.discover_top_chats()

    assert [c["username"] for c in picked] == ["elsewhere"]


def test_a_chat_without_a_username_is_skipped(monkeypatch):
    _install(monkeypatch, [_chat(-1003333333333, "")])

    assert telegram_top.discover_top_chats() == []


def test_small_chats_and_private_chats_are_skipped(monkeypatch):
    _install(
        monkeypatch,
        [
            _chat(-1004444444444, "small", participants=10),
            TgChat(id=-1005555555555, title="private", username="private", kind="chat"),
        ],
    )

    assert telegram_top.discover_top_chats() == []


def test_limit_stops_the_scan(monkeypatch):
    _install(
        monkeypatch,
        [_chat(-1006666666661, "one"), _chat(-1006666666662, "two")],
    )

    picked = telegram_top.discover_top_chats(limit=1)

    assert [c["username"] for c in picked] == ["one"]


def test_min_participants_is_honoured(monkeypatch):
    _install(monkeypatch, [_chat(-1007777777777, "medium", participants=2_000)])

    assert telegram_top.discover_top_chats(min_participants=5_000) == []
    assert len(telegram_top.discover_top_chats(min_participants=1_500)) == 1


def test_discovery_refuses_without_an_authorized_session(monkeypatch):
    from tgfarm import tguser

    monkeypatch.setattr(tguser, "is_authorized", lambda: False)

    with pytest.raises(RuntimeError):
        telegram_top._list_chats_sync()
