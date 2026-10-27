"""Read-only Telegram session, channel reconciliation, and new source collectors."""

from __future__ import annotations

import asyncio
import time

import pytest

from tgfarm.config import SourceSpec
from tgfarm.tg_sync import entity_id


# ------------------------------------------------------------------ read-only
class _FakeClient:
    """Stands in for a Telethon client."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def get_me(self):
        return "me"

    async def get_dialogs(self):
        return []

    async def send_message(self, *a, **k):  # pragma: no cover - must never run
        self.calls.append("send_message")
        return "sent"

    async def disconnect(self):
        self.calls.append("disconnect")


def test_read_allows_reads():
    import asyncio

    from tgfarm.tguser import ReadOnlyClient

    guard = ReadOnlyClient(_FakeClient())
    assert asyncio.run(guard.get_me()) == "me"


@pytest.mark.parametrize(
    "method",
    [
        "send_message", "send_file", "send_photo", "forward_messages",
        "delete_messages", "edit_message", "join_channel", "leave_channel",
        "create_channel", "invite_to_group", "add_contact", "delete_account",
        "log_out",
    ],
)
def test_read_only_blocks_writes(method):
    from tgfarm.tguser import ReadOnlyClient, ReadOnlyViolation

    guard = ReadOnlyClient(_FakeClient())
    with pytest.raises(ReadOnlyViolation):
        getattr(guard, method)


def test_read_only_blocks_attribute_assignment():
    from tgfarm.tguser import ReadOnlyClient, ReadOnlyViolation

    guard = ReadOnlyClient(_FakeClient())
    with pytest.raises(ReadOnlyViolation):
        guard.anything = 1  # noqa: B018


def test_read_only_closes_the_underlying_client():
    import asyncio

    from tgfarm.tguser import ReadOnlyClient

    fake = _FakeClient()
    guard = ReadOnlyClient(fake)

    async def go():
        async with guard:
            pass

    asyncio.run(go())
    assert "disconnect" in fake.calls


def test_session_lives_outside_the_repository():
    """The session file is the account; it must not sit where git can see it."""
    from tgfarm.tguser import session_path

    path = session_path()
    assert "data" in path.parts
    assert path.suffix != ".py"


# ------------------------------------------------------------- id translation
@pytest.mark.parametrize(
    ("api_chat_id", "entity"),
    [
        (-1001234567890, 1234567890),  # channel
        (-1009876543210, 9876543210),  # another channel
        (-987654321, 987654321),  # group
    ],
)
def test_entity_id_translation(api_chat_id, entity):
    """The Bot API and MTProto spell the same chat differently."""
    assert entity_id(api_chat_id) == entity


def test_channel_matching_prefers_username(monkeypatch):
    from tgfarm.tg_sync import _match

    class Chat:
        def __init__(self, id, title, username=None, kind="channel"):
            self.id, self.title, self.username, self.kind = id, title, username, kind
            self.linked_title = None

    chats = [
        Chat(1, "AI & Tech — разбор без хайпа", "other_channel"),
        Chat(2, "Канал технологий", "my_channel"),
    ]
    found = _match("ai_tech_ru", "AI & Tech — разбор без хайпа", "my_channel", chats)
    assert found is not None and found.id == 2


def test_channel_matching_falls_back_to_title():
    from tgfarm.tg_sync import _match

    class Chat:
        def __init__(self, id, title, username=None, kind="channel"):
            self.id, self.title, self.username, self.kind = id, title, username, kind
            self.linked_title = None

    chats = [Chat(7, "Наука — коротко и по делу")]
    found = _match("science_ru", "Наука - коротко и по делу", None, chats)
    assert found is not None and found.id == 7


# ------------------------------------------------------------------ collectors
def spec(kind: str, **params) -> SourceSpec:
    return SourceSpec(id=f"t_{kind}", name=kind, kind=kind, url="", niches=["tech"], params=params)


def test_reddit_url_for_a_subreddit():
    from tgfarm.s1_sources.extra_sources import RedditCollector

    url = RedditCollector().render_url(spec("reddit", subreddit="LocalLLaMA"))
    assert url.startswith("https://www.reddit.com/r/LocalLLaMA/.rss")


def test_reddit_url_for_search():
    from tgfarm.s1_sources.extra_sources import RedditCollector

    url = RedditCollector().render_url(spec("reddit", search="open weights", sort="top"))
    assert "search.rss?q=open%20weights" in url


def test_hackernews_url():
    from tgfarm.s1_sources.extra_sources import HackerNewsCollector

    assert "hnrss.org/frontpage" in HackerNewsCollector().render_url(spec("hackernews", min_points=150))
    query_url = HackerNewsCollector().render_url(spec("hackernews", query="LLM"))
    assert "hnrss.org/newest?q=LLM" in query_url


def test_youtube_url_requires_a_channel():
    from tgfarm.s1_sources.extra_sources import YouTubeCollector

    with pytest.raises(ValueError):
        YouTubeCollector().render_url(spec("youtube"))
    url = YouTubeCollector().render_url(spec("youtube", channel_id="UC123"))
    assert url == "https://www.youtube.com/feeds/videos.xml?channel_id=UC123"


def test_github_url_requires_owner_and_repo():
    from tgfarm.s1_sources.extra_sources import GitHubCollector

    with pytest.raises(ValueError):
        GitHubCollector().render_url(spec("github", owner="vllm-project"))
    url = GitHubCollector().render_url(spec("github", owner="vllm-project", repo="vllm"))
    assert url == "https://github.com/vllm-project/vllm/releases.atom"


def test_telegram_source_refuses_invite_links():
    """An invite link is not a public channel and would need the account to join."""
    from tgfarm.s1_sources.telegram_source import target_of

    invite = SourceSpec(id="x", name="x", kind="telegram", url="https://t.me/+secret")
    with pytest.raises(ValueError):
        target_of(invite)


def test_telegram_source_accepts_public_links():
    from tgfarm.s1_sources.telegram_source import target_of

    public = SourceSpec(id="x", name="x", kind="telegram", url="https://t.me/habr")
    assert target_of(public) == "habr"


def test_source_registry_accepts_t_me_dark2weboriginal(cfg):
    sources = {source.id: source for source in cfg.sources}
    source = sources["tg_dark2weboriginal"]
    assert source.kind == "telegram"
    assert source.url == "https://t.me/Dark2weboriginal"
    assert source.params.get("channel") == "Dark2weboriginal"
    assert source.params.get("handle") == "Dark2weboriginal"


def test_title_is_derived_from_the_first_line():
    from tgfarm.s1_sources.telegram_source import _title_from

    assert _title_from("Первая строка\n\nВторая часть текста") == "Первая строка"
    assert _title_from("Короткая новость. И продолжение с деталями.").startswith("Короткая новость")


# ------------------------------------------------------------------ host pacing
@pytest.mark.asyncio
async def test_same_host_is_paced_and_different_hosts_are_not():
    from tgfarm.s1_sources.base import HostPacer

    pacer = HostPacer(interval=0.4)
    started = time.monotonic()
    await asyncio.gather(
        pacer.wait("https://a.invalid/1"),
        pacer.wait("https://a.invalid/2"),
        pacer.wait("https://b.invalid/1"),
    )
    elapsed = time.monotonic() - started
    # one gap between the two same-host requests, none for the other host
    assert elapsed >= 0.35


def test_host_pacer_uses_a_per_host_override():
    from tgfarm.s1_sources.base import HostPacer

    pacer = HostPacer(interval=2.0)
    assert pacer.interval_for("www.reddit.com") == 10.0
    assert pacer.interval_for("example.invalid") == 2.0


# ------------------------------------------------------------------- markdown
def test_preview_markdown_does_not_italicise_identifiers():
    from tgfarm.api import _telegram_html

    out = _telegram_html("источник gnews_business_ru")
    assert "<i>" not in out
    assert "gnews_business_ru" in out


def test_preview_markdown_still_bolds_and_links():
    from tgfarm.api import _telegram_html

    out = _telegram_html("**жирно** и [источник](https://x.invalid)")
    assert "<b>жирно</b>" in out
    assert '<a href="https://x.invalid">источник</a>' in out


def test_preview_escapes_html():
    from tgfarm.api import _telegram_html

    assert "<script>" not in _telegram_html("<script>alert(1)</script>")