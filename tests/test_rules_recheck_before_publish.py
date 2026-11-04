"""The last-moment rules re-read that stands in front of every external action.

Selection reads the rules minutes before; the native editor then spends that time
filling, uploading, tagging and scheduling.  These tests pin the two guarantees the
re-check exists for: a withdrawn permission stops the action, and a failed *read*
is never treated as a pass.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from tgfarm import community_targets
from tgfarm.community_policy import policy_history


def _page(url: str, *, text: str = "", chunks=None, description: str = ""):
    return {
        "url": url,
        "title": "",
        "description": description,
        "text": text,
        "chunks": list(chunks or []),
        "links": [],
    }


def _target(slug: str = "python") -> dict:
    return {
        "target_type": "community",
        "name": "Python",
        "slug": slug,
        "url": f"https://pikabu.ru/community/{slug}",
        "description": "Посты о Python и разработке.",
        "score": 76,
        "recommended": True,
        "matches": ["python"],
        "suggested_tags": ["python", "programming"],
        "blockers": [],
    }


def _post() -> dict:
    return {
        "public_id": "recheck_pikabu_post",
        "title": "Python automation for developers",
        "body": "A practical guide to Python programming and developer tools.",
        "cta": "",
        "sources": [],
        "hashtags": ["Python"],
        "media": [],
    }


def _community_chunks(rule: str = "Публикуйте только материалы по Python.") -> list[str]:
    return ["О сообществе", "Посты о Python и разработке.", "Правила сообщества", rule]


# ---------------------------------------------------------------------------
# Pikabu: revalidate_pikabu_target
# ---------------------------------------------------------------------------


def test_pikabu_recheck_accepts_a_clean_fresh_read_and_records_history(monkeypatch):
    slug = "recheck_clean"
    reads = []

    async def fake_fetch(url: str):
        reads.append(url)
        if url == community_targets.PIKABU_RULES:
            return _page(url, text="Общие правила Пикабу.")
        return _page(url, description="Сообщество о Python.", chunks=_community_chunks())

    monkeypatch.setattr(community_targets, "_fetch", fake_fetch)

    result = asyncio.run(community_targets.revalidate_pikabu_target(_post(), _target(slug)))

    assert reads == [community_targets.PIKABU_RULES, f"https://pikabu.ru/community/{slug}"]
    assert result["blockers"] == []
    assert result["policy_assessment"]["allow_publish"] is True
    assert result["policy_assessment"]["auto_publish"] is True
    assert result["community_policy"]["target_id"] == slug
    assert result["target"]["rules_available"] is True
    assert result["target"]["recommended"] is True
    # The verdict the publisher acted on is in the history, not only in memory.
    rows = policy_history(platform="pikabu", target_id=slug)
    assert [row["verdict_status"] for row in rows] == ["ok"]
    assert rows[0]["rules_hash"] == result["community_policy"]["rules_hash"]


def test_pikabu_recheck_blocks_when_community_rules_cannot_be_read(monkeypatch):
    slug = "recheck_unreadable"

    async def fake_fetch(url: str):
        if url == community_targets.PIKABU_RULES:
            return _page(url, text="Общие правила Пикабу.")
        raise RuntimeError("community page is down")

    monkeypatch.setattr(community_targets, "_fetch", fake_fetch)

    result = asyncio.run(community_targets.revalidate_pikabu_target(_post(), _target(slug)))

    assert any("не перечитались" in text for text in result["blockers"])
    assert result["policy_assessment"]["allow_publish"] is False
    assert result["policy_assessment"]["status"] == "unknown"
    assert result["target"]["recommended"] is False


def test_pikabu_recheck_blocks_when_the_global_code_cannot_be_read(monkeypatch):
    slug = "recheck_no_code"

    async def fake_fetch(url: str):
        if url == community_targets.PIKABU_RULES:
            raise RuntimeError("code page is down")
        return _page(url, chunks=_community_chunks())

    monkeypatch.setattr(community_targets, "_fetch", fake_fetch)

    result = asyncio.run(community_targets.revalidate_pikabu_target(_post(), _target(slug)))

    assert any("Кодекс Пикабу не перечитался" in text for text in result["blockers"])
    assert result["policy_assessment"]["allow_publish"] is False


def test_pikabu_recheck_sees_a_rule_that_appeared_after_selection(monkeypatch):
    """Advertising was allowed when the community was chosen; now it is not."""
    slug = "recheck_rule_changed"

    async def fake_fetch(url: str):
        if url == community_targets.PIKABU_RULES:
            return _page(url, text="Общие правила Пикабу.")
        return _page(url, chunks=_community_chunks("Реклама и самопродвижение запрещены."))

    monkeypatch.setattr(community_targets, "_fetch", fake_fetch)

    result = asyncio.run(community_targets.revalidate_pikabu_target(_post(), _target(slug)))

    assert any("реклам" in text.lower() for text in result["blockers"])
    assert result["target"]["recommended"] is False
    rows = policy_history(platform="pikabu", target_id=slug)
    assert rows[0]["rules_hash"] == result["community_policy"]["rules_hash"]


@pytest.mark.parametrize(
    "url",
    [
        "http://pikabu.ru/community/python",
        "https://pikabu.ru/tag/python",
        "https://evil.example/community/python",
        "https://pikabu.ru/community/",
    ],
)
def test_pikabu_recheck_refuses_a_non_canonical_destination(monkeypatch, url):
    async def must_not_fetch(_url: str):
        raise AssertionError("a rejected destination must not be fetched")

    monkeypatch.setattr(community_targets, "_fetch", must_not_fetch)

    with pytest.raises(ValueError, match="canonical"):
        asyncio.run(
            community_targets.revalidate_pikabu_target(_post(), {**_target(), "url": url})
        )


def test_pikabu_recheck_refuses_when_the_page_is_not_the_selected_community(monkeypatch):
    async def fake_fetch(url: str):
        return _page(url, text="Общие правила Пикабу.", chunks=_community_chunks())

    monkeypatch.setattr(community_targets, "_fetch", fake_fetch)

    with pytest.raises(ValueError, match="canonical"):
        asyncio.run(
            community_targets.revalidate_pikabu_target(
                _post(), {**_target("python"), "slug": "cooking"}
            )
        )


# ---------------------------------------------------------------------------
# Reddit: the preflight in the automatic crosspost pipeline
# ---------------------------------------------------------------------------


def _reddit_community(**overrides):
    community = {
        "name": "learnpython",
        "about": {
            "title": "Learn Python",
            "public_description": "Python questions and tutorials.",
            "subreddit_type": "public",
            "over18": False,
            "restrict_posting": False,
            "submission_type": "self",
        },
        "rules": [{"short_name": "Be constructive", "description": "Keep posts relevant."}],
        "requirements": {},
        "risk_flags": [],
    }
    community.update(overrides)
    return community


class RedditPipeline:
    """The S9 delivery path with every external call replaced by a recorder."""

    def __init__(self, monkeypatch, public_id: str, post_pk: int) -> None:
        from tgfarm import platforms, s9_growth
        from tgfarm.core.schemas import CrosspostResult
        from tgfarm.s9_growth import reddit_scout

        self.monkeypatch = monkeypatch
        self.s9_growth = s9_growth
        self.reddit_scout = reddit_scout
        self.post = SimpleNamespace(
            id=post_pk,
            public_id=public_id,
            hook="A practical Python debugging workflow",
            body="A source-grounded debugging workflow for Python developers.",
            cta="",
            hashtags=["python"],
            sources=[],
        )
        self.published = []
        self.logged = []

        monkeypatch.setattr(platforms, "is_platform_enabled", lambda name: name == "reddit")
        monkeypatch.setattr(s9_growth, "get_settings", lambda: SimpleNamespace(live=True))
        monkeypatch.setattr(s9_growth, "_load_post", lambda _post_id: self.post)
        monkeypatch.setattr(s9_growth, "already_done", lambda _p, _n: False)
        monkeypatch.setattr(s9_growth, "begin_crosspost", lambda _p, _n: 1)
        def record_log(_public_id, result, attempt=1):
            self.logged.append((result, attempt))

        monkeypatch.setattr(s9_growth, "log_result", record_log)

        async def build_submission(_public_id):
            return {
                "ok": True,
                "subreddit": "learnpython",
                "title": self.post.hook,
                "body": "A native, source-grounded explanation.",
                "reddit_kind": "self",
                "flair": {},
            }

        monkeypatch.setattr(reddit_scout, "build_automatic_submission", build_submission)

        adapter = s9_growth.ADAPTERS["reddit"]
        monkeypatch.setattr(adapter, "configured", lambda: True)

        async def publish(payload):
            self.published.append(payload)
            return CrosspostResult(
                platform="reddit",
                status="posted",
                remote_id="reddit-recheck-1",
                url="https://www.reddit.com/r/learnpython/comments/reddit-recheck-1/",
            )

        monkeypatch.setattr(adapter, "publish", publish)

    def inspect(self, community=None, *, error=None):
        reads = []

        async def inspect_community(subreddit):
            reads.append(subreddit)
            if error is not None:
                raise error
            return dict(_reddit_community() if community is None else community)

        self.monkeypatch.setattr(self.reddit_scout, "inspect_community", inspect_community)
        return reads

    def submission(self, **overrides):
        base = {
            "ok": True,
            "subreddit": "learnpython",
            "title": self.post.hook,
            "body": "A native, source-grounded explanation.",
            "reddit_kind": "self",
            "flair": {},
        }
        base.update(overrides)

        async def build_submission(_public_id):
            return dict(base)

        self.monkeypatch.setattr(self.reddit_scout, "build_automatic_submission", build_submission)

    def run(self):
        return asyncio.run(self.s9_growth.crosspost_post(self.post.id, ["reddit"]))


def test_reddit_preflight_publishes_when_the_fresh_read_still_allows_it(monkeypatch):
    pipeline = RedditPipeline(monkeypatch, "reddit_recheck_ok", 401)
    reads = pipeline.inspect()

    results = pipeline.run()

    assert [result.status for result in results] == ["posted"]
    assert reads == ["learnpython"]
    assert len(pipeline.published) == 1
    meta = pipeline.published[0].meta
    assert meta["community_policy"]["target_id"] == "learnpython"
    assert meta["community_policy_assessment"]["allow_publish"] is True


def test_reddit_preflight_stops_the_submit_when_rules_are_withdrawn(monkeypatch):
    pipeline = RedditPipeline(monkeypatch, "reddit_recheck_no_rules", 402)
    reads = pipeline.inspect(_reddit_community(rules=[]))

    results = pipeline.run()

    assert [result.status for result in results] == ["skipped"]
    assert reads == ["learnpython"]
    assert pipeline.published == []
    assert "rules" in results[0].error.lower()
    assert results[0].retry_after_seconds
    assert [entry[0].status for entry in pipeline.logged] == ["skipped"]


def test_reddit_preflight_stops_when_the_community_closes(monkeypatch):
    community = _reddit_community()
    community["about"]["restrict_posting"] = True
    pipeline = RedditPipeline(monkeypatch, "reddit_recheck_restricted", 403)
    pipeline.inspect(community)

    results = pipeline.run()

    assert [result.status for result in results] == ["skipped"]
    assert pipeline.published == []
    assert "закрыт" in results[0].error


def test_reddit_preflight_requires_a_flair_that_selection_did_not_pick(monkeypatch):
    pipeline = RedditPipeline(monkeypatch, "reddit_recheck_flair_missing", 404)
    pipeline.inspect(_reddit_community(requirements={"is_flair_required": True}))

    results = pipeline.run()

    assert [result.status for result in results] == ["skipped"]
    assert pipeline.published == []
    assert "flair" in results[0].error


def test_reddit_preflight_honours_the_flair_selected_at_scouting(monkeypatch):
    pipeline = RedditPipeline(monkeypatch, "reddit_recheck_flair_ok", 405)
    pipeline.inspect(_reddit_community(requirements={"is_flair_required": True}))
    pipeline.submission(flair={"flair_id": "tutorial-1", "flair_text": "Tutorial"})

    results = pipeline.run()

    assert [result.status for result in results] == ["posted"]
    assert pipeline.published[0].meta["community_policy_assessment"]["allow_publish"] is True


def test_reddit_preflight_treats_a_failed_read_as_a_refusal(monkeypatch):
    pipeline = RedditPipeline(monkeypatch, "reddit_recheck_read_error", 406)
    pipeline.inspect(error=ConnectionError("reddit is not answering"))

    results = pipeline.run()

    assert [result.status for result in results] == ["skipped"]
    assert pipeline.published == []
    assert "recheck" in results[0].error.lower()


def test_reddit_preflight_records_the_rules_it_saw(monkeypatch):
    pipeline = RedditPipeline(monkeypatch, "reddit_recheck_history", 407)
    pipeline.inspect()

    pipeline.run()

    rows = policy_history(platform="reddit", target_id="learnpython")
    assert rows and rows[0]["verdict_status"] == "ok"


# ---------------------------------------------------------------------------
# The capability answer the scout and the tailor give
# ---------------------------------------------------------------------------


def test_capability_names_every_missing_condition(monkeypatch):
    from tgfarm import platforms
    from tgfarm.core import settings as settings_module
    from tgfarm.s9_growth import reddit_scout

    monkeypatch.setattr(reddit_scout, "ADAPTERS", {"reddit": SimpleNamespace(configured=lambda: False)})
    monkeypatch.setattr(settings_module, "get_settings", lambda: SimpleNamespace(live=False))
    monkeypatch.setattr(platforms, "is_platform_enabled", lambda _name: False)

    capability = reddit_scout.automatic_publish_capability()

    assert capability["can_auto_publish"] is False
    assert capability["blockers"] == [
        "режим фермы не live",
        "Reddit выключен в центре площадок",
        "Reddit OAuth не настроен",
    ]


def test_capability_is_true_only_when_live_enabled_and_configured(monkeypatch):
    from tgfarm import platforms
    from tgfarm.core import settings as settings_module
    from tgfarm.s9_growth import reddit_scout

    monkeypatch.setattr(reddit_scout, "ADAPTERS", {"reddit": SimpleNamespace(configured=lambda: True)})
    monkeypatch.setattr(settings_module, "get_settings", lambda: SimpleNamespace(live=True))
    monkeypatch.setattr(platforms, "is_platform_enabled", lambda _name: True)

    capability = reddit_scout.automatic_publish_capability()

    assert capability["can_auto_publish"] is True
    assert capability["blockers"] == []
    assert capability["publisher"] == "reddit_api"


def test_capability_reports_the_missing_adapter(monkeypatch):
    from tgfarm import platforms
    from tgfarm.core import settings as settings_module
    from tgfarm.s9_growth import reddit_scout

    monkeypatch.setattr(reddit_scout, "ADAPTERS", {})
    monkeypatch.setattr(settings_module, "get_settings", lambda: SimpleNamespace(live=True))
    monkeypatch.setattr(platforms, "is_platform_enabled", lambda _name: True)

    capability = reddit_scout.automatic_publish_capability()

    assert capability["can_auto_publish"] is False
    assert capability["publisher"] == ""
    assert capability["blockers"] == ["для Reddit нет адаптера публикации"]
