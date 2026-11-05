"""The article identifier a Dzen publication names has to reach the journal.

Production measurement (read-only copy of the live database, 2026-11-05): of 37
`posted` Dzen rows only 8 stored `remote_id`/`url`. The reason is not a dropped
write — `log_result` has always copied both columns — it is that the publisher
never learned the id: the post-publish page exposes its article links as
root-relative hrefs (`/a/auXXXX`) and the absolute-only extractor skipped every
one of them, then returned `None` as "published, URL unknown".

Everything here is offline: a fake page object stands in for the browser and the
publisher coroutine is monkeypatched. No browser is launched and nothing is
published anywhere (§47).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from tgfarm.db.models import Channel, CrosspostLog, Post
from tgfarm.db.session import session_scope
from tgfarm.dzen_web import (
    DzenPublishResult,
    DzenWebPublisher,
    canonical_publication_url,
    pick_publication_ref,
    publication_ref,
)

TITLE = "Почему тариф на отопление вырос втрое за один квартал"
OUR_REF = "auOurArticle"


class _FakeLink:
    """One anchor: `get_attribute('href')` and `inner_text()`, nothing else."""

    def __init__(self, href: str, text: str) -> None:
        self._href = href
        self._text = text

    async def get_attribute(self, name: str) -> str | None:
        return self._href if name == "href" else None

    async def inner_text(self) -> str:
        return self._text


class _FakeSet:
    def __init__(self, items: list[_FakeLink], *, count: int = 1) -> None:
        self._items = items
        self._count = count

    async def all(self) -> list[_FakeLink]:
        return list(self._items)

    async def count(self) -> int:
        return self._count


def _page(url: str, links: list[tuple[str, str]], *, confirm: int = 1) -> SimpleNamespace:
    """A page whose URL and anchors are exactly what the test says they are.

    Only the three calls `_wait_published` makes are implemented, so this helper
    also documents what the publisher is allowed to ask the DOM for.
    """
    items = [_FakeLink(href, text) for href, text in links]
    return SimpleNamespace(
        url=url,
        locator=lambda _selector: _FakeSet(items),
        get_by_text=lambda _pattern: _FakeSet(items, count=confirm),
    )


def _publisher() -> DzenWebPublisher:
    publisher = DzenWebPublisher(profile_dir=".")

    async def _no_captcha(_page):
        return None

    # A captcha wait means a human in front of a browser; here nobody is waiting.
    publisher._wait_out_captcha = _no_captcha  # type: ignore[method-assign]
    publisher.publish_timeout_s = 1
    return publisher


# ---------------------------------------------------------------- id extraction
def test_publication_ref_reads_every_shape_of_the_same_article():
    assert publication_ref("https://dzen.ru/a/auCMDks643SJZZBM") == "auCMDks643SJZZBM"
    assert publication_ref("/a/auCMDks643SJZZBM") == "auCMDks643SJZZBM"
    assert publication_ref("/media/farm/long-story-slug") == "long-story-slug"
    assert publication_ref("https://dzen.ru/publisher/123/a/auCMDks643SJZZBM") == "auCMDks643SJZZBM"


def test_publication_ref_refuses_a_link_that_is_not_an_article():
    """The editor's own paths must never be stored as somebody's identifier."""
    assert publication_ref("https://dzen.ru/profile/editor/create") is None
    assert publication_ref("https://dzen.ru/publisher/id/271828/stat-analytics") is None
    assert publication_ref("") is None
    assert publication_ref(None) is None


def test_remote_id_of_the_publisher_is_the_key_the_analytics_side_looks_up():
    from tgfarm.s8_analytics.dzen_web import publication_id

    url = "https://dzen.ru/a/auCMDks643SJZZBM"
    assert DzenWebPublisher._remote_id(url) == publication_id(url) == "auCMDks643SJZZBM"


def test_canonical_url_round_trips_an_identifier():
    assert canonical_publication_url("auTest1") == "https://dzen.ru/a/auTest1"
    assert canonical_publication_url(None) is None
    assert canonical_publication_url("") is None


# ------------------------------------------------------------------ page choice
def test_pick_ref_uses_the_headline_when_the_list_holds_many_articles():
    """The post-publish page is the publications list: ours is not the only link."""
    candidates = [
        (f"/a/{OUR_REF}", TITLE),
        ("/a/auOlderArticleOne", "Прошлый материал про дорогу и бюджет"),
        ("/a/auOlderArticleTwo", "Ещё один прошлый материал про мост"),
    ]
    assert pick_publication_ref(candidates, TITLE) == OUR_REF


def test_pick_ref_survives_a_truncated_headline():
    """Dzen cuts long titles with an ellipsis in that very list."""
    candidates = [
        (f"/a/{OUR_REF}", TITLE[:40] + "…"),
        ("/a/auOlderArticleOne", "Прошлый материал про дорогу и бюджет"),
    ]
    assert pick_publication_ref(candidates, TITLE) == OUR_REF


def test_pick_ref_refuses_to_guess_between_unrelated_links():
    """No headline evidence and more than one candidate: nothing is stored.

    A wrong identifier is worse than none, because the analytics side trusts an
    id match and stops checking the headline.
    """
    candidates = [
        ("/a/auOlderArticleOne", "Прошлый материал про дорогу"),
        ("/a/auOlderArticleTwo", "Ещё один материал про мост"),
    ]
    assert pick_publication_ref(candidates, TITLE) is None
    assert pick_publication_ref([], TITLE) is None


def test_pick_ref_accepts_the_only_link_on_the_page():
    assert pick_publication_ref([(f"/a/{OUR_REF}", "Что угодно")], "Заголовок без связи") == OUR_REF


def test_pick_ref_breaks_a_repeated_headline_on_document_order():
    """The list is newest-first, so the first match is the article we just wrote."""
    candidates = [
        (f"/a/{OUR_REF}", TITLE),
        ("/a/auSameTitleOlder", TITLE),
    ]
    assert pick_publication_ref(candidates, TITLE) == OUR_REF


# --------------------------------------------------------------- wait_published
def test_wait_published_keeps_the_id_from_relative_links():
    """The regression: `?state=published` used to return None with links in hand."""
    page = _page(
        "https://dzen.ru/publisher/271828?state=published",
        [(f"/a/{OUR_REF}", TITLE), ("/a/auOlderArticleOne", "Прошлая статья про дорогу")],
    )
    url = asyncio.run(_publisher()._wait_published(page, TITLE))  # noqa: SLF001

    assert url == f"https://dzen.ru/a/{OUR_REF}"
    assert DzenWebPublisher._remote_id(url) == OUR_REF


def test_wait_published_still_prefers_the_article_url_of_the_page():
    page = _page(f"https://dzen.ru/a/{OUR_REF}?utm_source=share", [], confirm=0)
    url = asyncio.run(_publisher()._wait_published(page, TITLE))  # noqa: SLF001

    assert url == f"https://dzen.ru/a/{OUR_REF}"


def test_wait_published_reports_success_without_an_id_when_nothing_names_one():
    """`None` still means published: the article is live, only its key is lost."""
    page = _page("https://dzen.ru/publisher/271828?state=published", [("/profile/editor", TITLE)])
    url = asyncio.run(_publisher()._wait_published(page, TITLE))  # noqa: SLF001

    assert url is None


# ---------------------------------------------------------------- journal write
def _seed_channel() -> tuple[str, int]:
    suffix = uuid4().hex[:8]
    channel_id = f"dzpub_{suffix}"
    public_id = f"dzpub{suffix}"
    with session_scope() as session:
        session.add(Channel(id=channel_id, title="Дзен", niche="tech", platform="dzen"))
        session.add(
            Post(
                public_id=public_id,
                channel_id=channel_id,
                status="scheduled",
                hook=TITLE,
                body="Текст",
            )
        )
        post_id = session.execute(select(Post.id).where(Post.public_id == public_id)).scalar_one()
    return public_id, int(post_id)


def _journal(public_id: str) -> list[CrosspostLog]:
    with session_scope() as session:
        return list(
            session.execute(
                select(CrosspostLog)
                .where(CrosspostLog.post_public_id == public_id, CrosspostLog.platform == "dzen")
                .order_by(CrosspostLog.id)
            ).scalars()
        )


def _publish(monkeypatch, result: DzenPublishResult, post_id: int) -> dict:
    from tgfarm import s7_publish

    async def _fake_publish(self, *args, **kwargs):  # noqa: ANN001, ARG002
        return result

    monkeypatch.setattr(DzenWebPublisher, "publish", _fake_publish)
    monkeypatch.setattr(s7_publish, "get_settings", lambda: SimpleNamespace(live=True))
    monkeypatch.setattr(s7_publish, "_vk_has_own_channel", lambda: True)
    return asyncio.run(s7_publish._publish_dzen(post_id, " assembled text ", cover_path="cover.png"))


def test_successful_dzen_publish_stores_the_identifier(monkeypatch):
    public_id, post_id = _seed_channel()

    outcome = _publish(
        monkeypatch,
        DzenPublishResult(status="posted", url=f"https://dzen.ru/a/{OUR_REF}", remote_id=OUR_REF),
        post_id,
    )

    assert outcome["ok"] is True
    assert outcome["remote_id"] == OUR_REF
    rows = _journal(public_id)
    assert [row.status for row in rows] == ["posted"]
    assert rows[0].remote_id == OUR_REF
    assert rows[0].url == f"https://dzen.ru/a/{OUR_REF}"


def test_publish_derives_the_identifier_from_a_url_the_publisher_left_unparsed(monkeypatch):
    """Either column is enough: the journal still gets both, in canonical form.

    A root-relative href is exactly what the publisher used to hand back as
    nothing at all, and a last-segment read would be wrong for a media story, so
    the durable row is completed from whichever value exists.
    """
    public_id, post_id = _seed_channel()

    _publish(
        monkeypatch,
        DzenPublishResult(status="posted", url="/a/auDerivedFromHref"),
        post_id,
    )

    rows = _journal(public_id)
    assert rows[0].remote_id == "auDerivedFromHref"
    assert rows[0].url == "https://dzen.ru/a/auDerivedFromHref"


def test_publish_without_an_identifier_still_journals_the_publication(monkeypatch):
    """No id is a measurement problem, not a publication failure."""
    public_id, post_id = _seed_channel()

    outcome = _publish(monkeypatch, DzenPublishResult(status="posted"), post_id)

    assert outcome["ok"] is True
    rows = _journal(public_id)
    assert len(rows) == 1
    assert rows[0].status == "posted"
    assert rows[0].remote_id is None


def test_a_retry_keeps_the_identifier_the_first_attempt_stored(monkeypatch):
    """Nothing later about the same post blanks what an earlier row proved.

    CrosspostLog is append-only: a second attempt adds a row, it does not rewrite
    the first, so the identifier survives a retry, a timeout, and an operator
    confirmation that had nothing to copy.
    """
    public_id, post_id = _seed_channel()

    _publish(
        monkeypatch,
        DzenPublishResult(status="posted", url=f"https://dzen.ru/a/{OUR_REF}", remote_id=OUR_REF),
        post_id,
    )
    _publish(monkeypatch, DzenPublishResult(status="posted"), post_id)

    rows = _journal(public_id)
    assert [row.status for row in rows] == ["posted", "posted"]
    assert rows[0].remote_id == OUR_REF
    assert rows[1].remote_id is None

    # ...and the analytics side still finds the article by that identifier.
    from tgfarm.s8_analytics.dzen_web import (
        DzenPublicationMetrics,
        _journal_id_index,
        record_publication_metrics,
    )

    with session_scope() as session:
        by_remote, _by_url = _journal_id_index(session)
    assert by_remote.get(OUR_REF) == public_id

    recorded = record_publication_metrics(
        [
            DzenPublicationMetrics(
                url=f"https://dzen.ru/a/{OUR_REF}",
                remote_id=OUR_REF,
                title="Нечитаемая ячейка Студии",
                impressions=500,
                opens=50,
                views=30,
                reactions=7,
                comments=2,
                forwards=1,
            )
        ]
    )
    assert recorded["saved"] == 1
    assert recorded["matched_by_remote_id"] == 1
    assert recorded["matched_by_title"] == 0


@pytest.mark.parametrize("status", ["posted", "unknown", "publishing"])
def test_journal_index_ignores_dry_runs_and_keeps_real_attempts(status: str):
    """`dry-<post id>` is a run marker; a real attempt is a durable identifier."""
    from tgfarm.s8_analytics.dzen_web import _journal_id_index

    public_id, _post_id = _seed_channel()
    with session_scope() as session:
        session.add(
            CrosspostLog(
                post_public_id=public_id,
                platform="dzen",
                status=status,
                remote_id="auOnAnyRow",
                url="https://dzen.ru/a/auOnAnyRow",
            )
        )
        session.add(
            CrosspostLog(
                post_public_id=public_id,
                platform="dzen",
                status="dry_run",
                remote_id=f"dry-{public_id}",
            )
        )
        session.add(
            CrosspostLog(
                post_public_id="dzpub_missing",
                platform="vk",
                status="posted",
                remote_id="auVkOnlyId",
            )
        )
        by_remote, by_url = _journal_id_index(session)

    assert by_remote.get("auOnAnyRow") == public_id
    assert by_url.get("https://dzen.ru/a/auOnAnyRow") == public_id
    assert f"dry-{public_id}" not in by_remote
    assert "auVkOnlyId" not in by_remote
