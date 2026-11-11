"""Browser-level visual smoke tests for the current desktop dashboard.

The suite is opt-in so the normal fast unit-test run does not require a browser
binary. CI's dedicated UI Visual QA workflow installs Chromium and enables it.
"""
from __future__ import annotations

import json
import os
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("TGFARM_VISUAL_QA") != "1",
    reason="set TGFARM_VISUAL_QA=1 in the dedicated browser QA job",
)

ROOT = Path(__file__).resolve().parents[2]
ARTIFACT_DIR = ROOT / "artifacts" / "ui-visual"


class DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_GET(self):  # noqa: N802 - stdlib handler contract
        path = unquote(urlsplit(self.path).path)
        if path == "/":
            self.path = "/tgfarm/ui/index.html"
        elif path.startswith("/ui/"):
            self.path = "/tgfarm" + path
        return super().do_GET()

    def log_message(self, _format, *_args):
        pass


@pytest.fixture()
def dashboard_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _mock_api(route):
    """Use deterministic fixtures; no account or live service is touched."""
    path = urlsplit(route.request.url).path
    payload = {
        "/api/status": {
            "mode": "off",
            "scheduler_running": False,
            "channels": 2,
            "sources": 67,
            "embedder": "local",
            "alerts": [],
            "kpis": {
                "sources": 54,
                "sources_total": 67,
                "ready_clusters": 3,
                "clusters": 12,
                "scheduled": 4,
                "gated": 3,
                "published": 18,
                "posts": 25,
                "rejected": 2,
                "reject_rate": 0.08,
                "avg_er": None,
                "covers": 14,
                "videos": 1,
                "crossposts": 8,
            },
        },
        "/api/connect/matrix": {
            "platforms": [
                {
                    "id": "telegram", "name": "Telegram", "category": "Мессенджер",
                    "mode": "token", "authorized": True, "account": "@tgfarm_demo",
                    "hint": "Доступ подтверждён. Публикация через Bot API.",
                },
                {
                    "id": "vk", "name": "VK", "category": "Социальная сеть",
                    "mode": "vkhost", "authorized": False,
                    "hint": "Нужен пользовательский токен для выбранных операций.",
                },
                {
                    "id": "dzen", "name": "Дзен", "category": "Статьи и блоги",
                    "mode": "browser", "profile_saved": True,
                    "hint": "Браузерный профиль сохранён, требуется проверка сессии.",
                },
                {
                    "id": "reddit", "name": "Reddit", "category": "Сообщества",
                    "mode": "oauth", "authorized": None,
                    "hint": "Подключение через OAuth и выбранные правила сообщества.",
                },
            ],
        },
    }.get(path, {})
    route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))


def _capture_view(page, view: str, viewport: tuple[int, int]):
    page.set_viewport_size({"width": viewport[0], "height": viewport[1]})
    page.locator(f'.tab[data-view="{view}"]').click()
    page.locator(f'.view[data-view="{view}"].is-active').wait_for()
    page.wait_for_timeout(120)
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    page.screenshot(
        path=str(ARTIFACT_DIR / f"{viewport[0]}x{viewport[1]}-{view}.png"),
        full_page=True,
        animations="disabled",
    )


def test_dashboard_views_render_without_browser_runtime_errors(dashboard_url):
    from playwright.sync_api import sync_playwright

    browser_errors: list[str] = []
    failed_static_requests: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        page.on("pageerror", lambda error: browser_errors.append(str(error)))
        page.on(
            "requestfailed",
            lambda request: failed_static_requests.append(request.url)
            if "/ui/" in request.url
            else None,
        )
        page.route("**/api/**", _mock_api)

        response = page.goto(dashboard_url, wait_until="networkidle")
        assert response is not None and response.status == 200
        assert page.title().lower().startswith("tgfarm")
        page.locator(".tab[data-view='overview']").wait_for()

        for viewport in ((1440, 960), (1280, 800)):
            for view in ("overview", "team", "posts", "connect", "platforms"):
                _capture_view(page, view, viewport)
                assert page.locator(f'.view[data-view="{view}"]').is_visible()
                if view == "overview":
                    grid = page.locator(".command-shortcuts-grid")
                    assert grid.evaluate("(el) => getComputedStyle(el).display") == "grid"
                    card = grid.locator("button").first
                    assert card.evaluate("(el) => getComputedStyle(el).borderRadius") != "0px"
                    assert page.locator("#kpis").get_by_text("из 67 активно").count() == 1
                if view == "posts":
                    assert page.get_by_role("heading", name="Очередь пока пуста").is_visible()
                    page.get_by_role("button", name="Открыть планирование").click()
                    assert page.locator('.view[data-view="plan"].is-active').is_visible()
                if view == "connect":
                    assert page.locator(".connect-card").count() == 4
                    assert page.get_by_text("Подключено · @tgfarm_demo").count() == 1
                    for title in page.locator(".connect-card-title").all():
                        assert title.evaluate("(el) => el.getBoundingClientRect().width >= 120")
                        assert title.locator("strong").evaluate("(el) => el.getBoundingClientRect().width >= 70")

        browser.close()

    assert not failed_static_requests, f"static UI assets failed: {failed_static_requests}"
    assert not browser_errors, f"uncaught browser errors: {browser_errors}"
