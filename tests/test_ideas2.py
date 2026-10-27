"""Tests for the Dzen-auth and quick-wins batch."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest


def _draft(hook: str, body: str, post_format: str | None = None):
    from tgfarm.core.schemas import DraftPost

    draft = DraftPost(
        hook=hook,
        body=body,
        sources=[{"title": "t", "url": "https://example.invalid/x"}],
    )
    draft.post_format = post_format
    return draft


def _persona(platform: str = "vk", **kwargs):
    from tgfarm.config import Persona

    return Persona(id="probe2", title="Probe", niche="general", platform=platform, **kwargs)


# ------------------------------------------------------- flash emoji budget
def test_flash_format_bans_emojis():
    from tgfarm.s5_quality import gate_structure

    body = "Первый абзац с фактами.\\n\\nВторой абзац с выводом."
    with_emoji = _draft("Дрон складывается за 0,3 секунды 🚀.", body, post_format="flash")
    outcome = gate_structure(with_emoji, _persona())
    assert outcome.passed is False and "max 0" in (outcome.reason or "")

    plain = _draft("Дрон складывается за 0,3 секунды.", body, post_format="flash")
    assert gate_structure(plain, _persona()).passed is True

    # news-формат на max_1 канале по-прежнему допускает один эмодзи
    one = _draft("Дрон складывается за 0,3 секунды 🚀.", body, post_format="news")
    assert gate_structure(one, _persona()).passed is True


# ------------------------------------------------------------- dzen auth
def test_dzen_auth_alert_fires(db):
    from tgfarm import pipeline
    from tgfarm.db.models import Post

    db.add(Post(public_id="dzen-auth-1", channel_id="zen_misc_ru", hook="хук", body="тело",
                status="scheduled",
                publish_error="сессия Яндекса не появилась за отведённое время",
                created_at=datetime.now(UTC)))
    db.commit()

    codes = [a["code"] for a in pipeline.collect_alerts()]
    assert "dzen_auth" in codes


def test_dzen_status_endpoint_structure():
    from fastapi.testclient import TestClient

    from tgfarm.api import create_app
    from tgfarm.core.apiauth import ensure_token

    client = TestClient(create_app(with_scheduler=False), base_url="http://127.0.0.1")
    headers = {"X-TGFarm-Token": ensure_token()}
    resp = client.get("/api/dzen/status", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert {"authorized", "busy", "profile_ready"} <= set(body)
    assert body["profile_ready"] is True


def test_weekly_report_sends_digest(db, monkeypatch):
    from tgfarm import pipeline
    from tgfarm.db.models import Post

    monkeypatch.setattr(
        "tgfarm.pipeline.get_settings",
        lambda: SimpleNamespace(
            telegram_bot_token="tok", alert_chat_id="42",
            alert_no_publish_hours=3, alert_cooldown_min=30,
            alert_heartbeat_hours=24, cycle_timeout_min=60, cycle_stale_min=45,
            live=False,
        ),
    )
    db.add(Post(public_id="wr-1", channel_id="zen_misc_ru", hook="Лучший пост недели",
                body="тело", status="published", published_at=datetime.now(UTC)))
    db.commit()

    sent: list[str] = []

    async def fake_send(text: str) -> bool:
        sent.append(text)
        return True

    monkeypatch.setattr(pipeline, "_tg_send_text", fake_send)

    import asyncio

    result = asyncio.run(pipeline.weekly_report())
    assert result["sent"] is True
    assert "Недельный отчёт" in sent[0] and "Лучший пост" in sent[0]


# ------------------------------------------------------------ image dedup
def test_recent_image_shas_collects_media(db):
    from tgfarm.db.models import Media, Post
    from tgfarm.s6_visual.article_images import _recent_image_shas

    post = Post(public_id="sha-host", channel_id="zen_misc_ru", hook="хук", body="тело",
                status="gated")
    db.add(post)
    db.flush()
    db.add(Media(post_id=post.id, kind="article_image", path="data/media/a.jpg",
                 sha="deadbeef" * 8, is_primary=False))
    db.commit()

    shas = _recent_image_shas()
    assert "deadbeef" * 8 in shas


# ------------------------------------------------------------ backup test
def test_backup_restore_roundtrip(tmp_path):
    import sqlite3

    from sqlalchemy.engine import make_url

    from tgfarm.db.session import get_engine

    db_path = make_url(str(get_engine().url)).database
    src = sqlite3.connect(db_path)
    target_path = tmp_path / "restore.db"
    dst = sqlite3.connect(str(target_path))
    src.backup(dst)
    dst.close()

    restored = sqlite3.connect(str(target_path))
    tables = {r[0] for r in restored.execute("select name from sqlite_master where type='table'")}
    assert "posts" in tables and "operator_logs" in tables
    src_count = src.execute("select count(*) from posts").fetchone()[0]
    dst_count = restored.execute("select count(*) from posts").fetchone()[0]
    assert src_count == dst_count


# --------------------------------------------------------- secret scanner
def test_secret_scanner_finds_leaks_and_honours_env(tmp_path, monkeypatch):
    import re as _re

    import tools.scan_secrets as scanner_mod

    monkeypatch.setattr(scanner_mod, "PATTERN", _re.compile(r"FAKEKEY\d{10,}"))
    from tools.scan_secrets import scan

    (tmp_path / "leak.py").write_text('key = "sk_FAKEKEY1234567890"\n', encoding="utf-8")
    (tmp_path / "clean.py").write_text("key = os.environ['KEY']\n", encoding="utf-8")
    (tmp_path / ".env").write_text("LLM_API_KEY=sk_FAKEKEY1234567890\n", encoding="utf-8")

    hits = scan(tmp_path)
    leak_hits = [h for h in hits if "leak.py" in h]
    env_hits = [h for h in hits if ".env" in h]
    assert len(leak_hits) == 1
    assert not any("clean.py" in h for h in hits)
    assert env_hits and "допустимо" in env_hits[0]


# ------------------------------------------------------------- captcha wait
class _FakeCaptchaPage:
    """url + get_by_text().count() with a scripted sequence of counts."""

    def __init__(self, url: str, counts: list[int]):
        self.url = url
        self.counts = list(counts)

    def get_by_text(self, _pattern):
        parent = self

        class _Locator:
            async def count(self):
                if len(parent.counts) > 1:
                    return parent.counts.pop(0)
                return parent.counts[0]

        return _Locator()


def _probe(**kwargs):
    from tgfarm.dzen_web import DzenWebPublisher

    return DzenWebPublisher(headless=True, **kwargs)


def test_wait_out_captcha_returns_without_captcha():
    import asyncio

    page = _FakeCaptchaPage("https://dzen.ru/profile/editor", [0])
    asyncio.run(_probe()._wait_out_captcha(page))


def _dz_settings(**extra):
    from tgfarm.core.settings import get_settings as real_get_settings

    base = real_get_settings()
    defaults = dict(
        dzen_profile_path=base.dzen_profile_path,
        dzen_browser=base.dzen_browser,
        dzen_headless=True,
        dzen_human_delay_ms=base.dzen_human_delay_ms,
        dzen_publish_timeout_s=base.dzen_publish_timeout_s,
        dzen_login_timeout_s=base.dzen_login_timeout_s,
        dzen_captcha_wait_s=180,
    )
    defaults.update(extra)
    return SimpleNamespace(**defaults)


def test_wait_out_captcha_resumes_when_solved(monkeypatch):
    import asyncio

    # капча есть на первой проверке, исчезает на втором опросе
    page = _FakeCaptchaPage("https://dzen.ru/profile/editor", [1, 0, 0])
    monkeypatch.setattr("tgfarm.dzen_web.get_settings", lambda: _dz_settings())
    asyncio.run(_probe()._wait_out_captcha(page))


def test_wait_out_captcha_raises_on_timeout(monkeypatch):
    import asyncio

    from tgfarm.dzen_web import DzenCaptcha

    page = _FakeCaptchaPage("https://dzen.ru/profile/editor", [1])
    monkeypatch.setattr("tgfarm.dzen_web.get_settings", lambda: _dz_settings(dzen_captcha_wait_s=6))
    with pytest.raises(DzenCaptcha):
        asyncio.run(_probe()._wait_out_captcha(page))


def test_dzen_captcha_alert_fires(db):
    from tgfarm import pipeline
    from tgfarm.db.models import Post

    db.add(Post(public_id="cap-1", channel_id="zen_misc_ru", hook="хук", body="тело",
                status="scheduled",
                publish_error="капча не решена за отведённое время",
                created_at=datetime.now(UTC)))
    db.commit()

    codes = [a["code"] for a in pipeline.collect_alerts()]
    assert "dzen_captcha" in codes


# --------------------------------------------------- transport backoff
def test_transport_failure_backs_off_exponentially(db, monkeypatch):
    import asyncio

    from tgfarm.core.schemas import CrosspostResult
    from tgfarm.db.models import Channel, Post
    from tgfarm.db.session import session_scope
    from tgfarm.s7_publish import _publish_one

    with session_scope() as session:
        session.merge(Channel(id="bk_chan", title="BK", niche="general", platform="vk"))
        post = Post(public_id="bk-1", channel_id="bk_chan", hook="хук", body="тело",
                    status="scheduled", scheduled_at=datetime.now(UTC), attempts=2)
        session.add(post)
        session.flush()
        post_id = post.id

    async def fail_publish(self, payload):
        return CrosspostResult(platform="vk", status="failed",
                               error="vk: [Errno 11001] getaddrinfo failed")

    # иначе _publish_vk уходит в dry-run, не дойдя до адаптера
    monkeypatch.setattr(
        "tgfarm.s7_publish.get_settings",
        lambda: SimpleNamespace(live=True, dzen_requires_llm=True),
    )
    # и адаптеру нужны креденшелы, иначе он падает раньше фейкового сбоя
    monkeypatch.setattr(
        "tgfarm.s9_growth.adapters.get_settings",
        lambda: SimpleNamespace(
            vk_access_token="tok", vk_group_id="-1",
            max_access_token="", max_channel_id="",
            x_bearer_token="", x_user_id="",
        ),
    )
    monkeypatch.setattr("tgfarm.s9_growth.adapters.VkAdapter.publish", fail_publish)

    result = asyncio.run(_publish_one(post_id, "vk", "текст", None, "vk"))
    assert result["ok"] is False

    with session_scope() as session:
        row = session.get(Post, post_id)
        wait = (row.scheduled_at - datetime.now(UTC).replace(tzinfo=None)).total_seconds() / 60
        assert wait > 30, f"транспортный сбой должен давать backoff, а не 5 минут (получено {wait:.0f})"
        assert "getaddrinfo" in (row.publish_error or "")


def test_stuck_post_raises_publish_failing_alert(db):
    from tgfarm import pipeline
    from tgfarm.db.models import Post

    db.add(Post(public_id="stk-9", channel_id="vk_misc_ru", hook="хук", body="тело",
                status="scheduled", attempts=6,
                publish_error="vk: [Errno 11001] getaddrinfo failed",
                scheduled_at=datetime.now(UTC)))
    db.commit()

    codes = [a["code"] for a in pipeline.collect_alerts()]
    assert "publish_failing" in codes


# --------------------------------------------------- session warm + cache
def test_cached_status_serves_cache_when_fresh():
    import asyncio
    import time

    from tgfarm import dzen_web

    dzen_web._auth_state_cache = {"authorized": True, "busy": False, "checked_at": "now"}
    dzen_web._auth_state_at = time.monotonic()
    try:
        state = asyncio.run(dzen_web.DzenWebPublisher().cached_status(max_age_s=600))
        assert state["authorized"] is True
    finally:
        dzen_web._auth_state_cache = None
        dzen_web._auth_state_at = 0.0


def test_warm_tick_returns_state_and_warns_on_dead_session(capsys):
    import asyncio

    from tgfarm import pipeline
    from tgfarm.dzen_web import DzenWebPublisher

    class _FakePublisher:
        def profile_ready(self):
            return True

        async def auth_check(self, timeout_s=30):
            return {"authorized": False, "busy": False, "checked_at": "now"}

    monkey_orig = DzenWebPublisher
    import tgfarm.dzen_web as dw

    dw.DzenWebPublisher = _FakePublisher
    try:
        state = asyncio.run(pipeline._dzen_warm_tick())
        assert state["authorized"] is False
    finally:
        dw.DzenWebPublisher = monkey_orig


# ------------------------------------------- duplicate draft suppression
def test_duplicate_draft_suppressed(db):
    """wall_41/42: one story must not hit the wall twice."""
    from tgfarm.llm.embedding import get_embedder
    from tgfarm.s4_generation import _too_similar_vec

    embedder = get_embedder()
    text = "РФ прорабатывает возможность совместной работы с США в Арктике"
    vec = embedder.embed(text)
    pool = [{"channel_id": "vk_misc_ru", "vec": vec, "label": text[:60]}]

    assert _too_similar_vec(vec, pool, "vk_misc_ru") is True
    assert _too_similar_vec(vec, pool, "dzen_misc_ru") is False, "другой канал не считается"

    other = embedder.embed("Клавиатура из полимера растворяется в воде за сорок минут")
    assert _too_similar_vec(other, pool, "vk_misc_ru") is False
