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
        "/api/platforms": {
            "summary": {
                "total": 2, "supported": 2, "enabled": 1,
                "configured": 1, "planned": 0, "needs_setup": 0,
            },
            "note": "Тестовый каталог возможностей.",
            "platforms": [
                {
                    "id": "telegram", "name": "Telegram", "symbol": "TG",
                    "category": "Мессенджеры", "market": "Россия и мир",
                    "priority": "core", "description": "Основная очередь через Bot API.",
                    "requirements": "Токен бота и целевой канал.",
                    "docs_url": "https://core.telegram.org/bots/api",
                    "supported": True, "enabled": True, "planned": False,
                    "configured": True, "connection_detail": "Доступ настроен.",
                    "status": "configured", "status_label": "Настройки найдены",
                    "channel_count": 2, "roles_label": "Основная публикация",
                    "automation_mode": "primary",
                    "automation_note": "Основная очередь площадки.",
                    "capability": "publish", "capability_label": "Публикует автоматически",
                    "capability_note": "Материал доставляется адаптером без участия человека.",
                    "auto_publish": True, "supports_draft": True,
                    "supports_manual_package": True,
                    "auth": {
                        "method": "token", "status": "authorized",
                        "status_label": "Доступ подтверждён", "detail": "Bot API доступен.",
                        "fields": [], "browser_session": {"available": False, "profile_saved": False, "verified": False},
                    },
                    "editorial": None,
                },
                {
                    "id": "tiktok", "name": "TikTok", "symbol": "TT",
                    "category": "Видео", "market": "Мировые",
                    "priority": "P1", "description": "Подготовка ролика с подтверждением владельца.",
                    "requirements": "OAuth и явное согласие для каждой публикации.",
                    "docs_url": "https://developers.tiktok.com/",
                    "supported": True, "enabled": False, "planned": False,
                    "configured": False, "connection_detail": "Подключение ещё не настроено.",
                    "status": "paused", "status_label": "Выключена",
                    "channel_count": 0, "roles_label": "Видео",
                    "automation_mode": "manual",
                    "automation_note": "Каждое видео требует отдельного согласия автора.",
                    "capability": "manual_upload", "capability_label": "Ручная загрузка пакета",
                    "capability_note": "Публикация требует участия человека.",
                    "auto_publish": False, "supports_draft": True,
                    "supports_manual_package": True,
                    "auth": {
                        "method": "oauth", "status": "needs_setup",
                        "status_label": "Не настроено", "detail": "Нужен вход через OAuth.",
                        "fields": [], "browser_session": {"available": False, "profile_saved": False, "verified": False},
                    },
                    "editorial": None,
                },
            ],
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
    tab = page.locator(f'.tab[data-view="{view}"]')
    tab.evaluate("(el) => el.click()")
    active_view = page.locator(f'.view[data-view="{view}"].is-active')
    active_view.wait_for(state="visible", timeout=5000)
    page.locator(f'.tab[data-view="{view}"][aria-selected="true"]').wait_for(state="visible")
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

        # The sidebar is a real tablist: every tab names one panel, and the
        # selected state is mirrored by panel visibility for assistive tech.
        tablist = page.locator(".tabs")
        assert tablist.get_attribute("role") == "tablist"
        assert tablist.get_attribute("aria-label") == "Разделы TgFarm"
        tabs = page.locator(".tabs [role='tab']")
        assert tabs.count() == page.locator(".view[role='tabpanel']").count()
        for tab in tabs.all():
            view = tab.get_attribute("data-view")
            panel_id = tab.get_attribute("aria-controls")
            assert panel_id == f"panel-{view}"
            panel = page.locator(f"#{panel_id}")
            assert panel.get_attribute("role") == "tabpanel"
            assert panel.get_attribute("aria-labelledby") == tab.get_attribute("id")
            selected = tab.get_attribute("aria-selected") == "true"
            assert (panel.get_attribute("aria-hidden") == "false") == selected

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
                if view == "platforms":
                    assert page.locator(".platform-card").count() == 2
                    assert page.get_by_text("2 канала", exact=True).is_visible()
                    assert page.get_by_text("0 каналов", exact=True).is_visible()
                    assert page.get_by_text("Публикует автоматически", exact=True).is_visible()
                    assert page.get_by_text("Ручная загрузка пакета", exact=True).is_visible()
                    assert page.get_by_text("Каждое видео требует отдельного согласия автора.", exact=True).is_visible()
                    for auth_copy in page.locator(".platform-auth-copy").all():
                        assert auth_copy.evaluate("(el) => el.getBoundingClientRect().width >= 180")
                    for auth_block in page.locator(".platform-auth-block").all():
                        assert auth_block.evaluate("""(el) => {
                            const bounds = el.getBoundingClientRect();
                            return [...el.querySelectorAll(".platform-auth-actions .btn")]
                                .every((button) => {
                                    const rect = button.getBoundingClientRect();
                                    return rect.left >= bounds.left - 1 && rect.right <= bounds.right + 1;
                                });
                        }""")
                    capability_filter = page.locator("#platform-capability-filter")
                    capability_filter.select_option("publish")
                    assert page.locator(".platform-card").count() == 1
                    assert page.locator(".platform-card h3").inner_text() == "Telegram"
                    capability_filter.select_option("manual_upload")
                    assert page.locator(".platform-card").count() == 1
                    assert page.locator(".platform-card h3").inner_text() == "TikTok"
                    capability_filter.select_option("unsupported")
                    assert page.locator(".platform-card").count() == 0
                    assert page.locator(".platform-empty").is_visible()
                    page.get_by_role("button", name="Сбросить фильтры").click()
                    assert capability_filter.input_value() == "all"
                    assert page.locator(".platform-card").count() == 2
                if view == "connect":
                    assert page.locator(".connect-card").count() == 4
                    assert page.get_by_text("Подключено · @tgfarm_demo").count() == 1
                    for title in page.locator(".connect-card-title").all():
                        assert title.evaluate("(el) => el.getBoundingClientRect().width >= 120")
                        assert title.locator("strong").evaluate("(el) => el.getBoundingClientRect().width >= 70")

        # Narrow webview: navigation stays in one horizontally scrollable row,
        # and the page itself must not overflow the viewport horizontally.
        page.set_viewport_size({"width": 390, "height": 844})
        page.locator('.tab[data-view="overview"]').evaluate("(el) => el.click()")
        assert page.locator(".tabs").evaluate("(el) => getComputedStyle(el).flexWrap") == "nowrap"
        assert page.locator('.tab[data-view="overview"]').get_attribute("aria-selected") == "true"
        assert page.locator('.tab[data-view="team"]').get_attribute("aria-selected") == "false"
        assert page.locator(".tabs").evaluate("(el) => el.scrollWidth > el.clientWidth")
        assert page.locator("#data-sync").is_visible()
        assert page.locator("#refresh-data").is_visible()
        overflow = page.locator("body").evaluate("""(body) => ({
            bodyWidth: body.scrollWidth,
            viewportWidth: window.innerWidth,
            offenders: [...document.querySelectorAll("body *")]
                .map((node) => {
                    const rect = node.getBoundingClientRect();
                    const style = getComputedStyle(node);
                    return {
                        tag: node.tagName,
                        id: node.id,
                        className: typeof node.className === "string" ? node.className : "",
                        left: Math.round(rect.left),
                        right: Math.round(rect.right),
                        width: Math.round(rect.width),
                        overflowX: style.overflowX,
                    };
                })
                .filter((item) => item.width > 0 && item.right > window.innerWidth + 1)
                .slice(0, 20),
        })""")
        assert overflow["bodyWidth"] <= overflow["viewportWidth"], overflow
        page.locator('.tab[data-view="team"]').evaluate("(el) => el.click()")
        assert page.locator('.view[data-view="team"].is-active').is_visible()

        # Tablist keyboard contract: arrows wrap through the ordered tabs;
        # Home/End jump to boundaries, and focus always follows selection.
        team_tab = page.locator('.tab[data-view="team"]')
        team_tab.focus()
        team_tab.press("ArrowRight")
        assert page.locator("#tab-plan").get_attribute("aria-selected") == "true"
        assert page.evaluate("document.activeElement.id") == "tab-plan"
        assert page.locator("#panel-plan").is_visible()
        page.locator("#tab-plan").press("ArrowDown")
        assert page.locator("#tab-posts").get_attribute("aria-selected") == "true"
        assert page.evaluate("document.activeElement.id") == "tab-posts"
        page.locator("#tab-posts").press("ArrowLeft")
        assert page.locator("#tab-plan").get_attribute("aria-selected") == "true"
        page.locator("#tab-plan").press("ArrowUp")
        assert page.locator("#tab-team").get_attribute("aria-selected") == "true"
        page.locator("#tab-team").press("End")
        assert page.locator("#tab-runs").get_attribute("aria-selected") == "true"
        assert page.locator("#panel-runs").is_visible()
        assert page.evaluate("document.activeElement.id") == "tab-runs"
        page.locator("#tab-runs").press("Home")
        assert page.locator("#tab-overview").get_attribute("aria-selected") == "true"
        assert page.locator("#panel-overview").is_visible()
        assert page.evaluate("document.activeElement.id") == "tab-overview"

        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        page.screenshot(
            path=str(ARTIFACT_DIR / "390x844-team-mobile.png"),
            full_page=True,
            animations="disabled",
        )
        page.locator('#tab-platforms').click()
        page.locator("#panel-platforms").wait_for(state="visible")
        assert page.locator(".platform-card").count() == 2
        assert page.locator("body").evaluate("(el) => el.scrollWidth <= window.innerWidth")

        browser.close()

    assert not failed_static_requests, f"static UI assets failed: {failed_static_requests}"
    assert not browser_errors, f"uncaught browser errors: {browser_errors}"


def test_publish_unknown_post_drawer_requires_reconciliation(dashboard_url):
    """An uncertain delivery must open an evidence-first drawer, never a blind retry."""
    from playwright.sync_api import sync_playwright

    post = {
        "id": "post-unknown-001",
        "channel": "telegram",
        "channel_id": "telegram",
        "channel_title": "Основной канал",
        "platform": "telegram",
        "angle": "Проверка безопасной сверки",
        "post_format": "news",
        "hook": "Пост с неопределённым результатом",
        "excerpt": "Проверяем, не отправлен ли материал повторно.",
        "chars": 420,
        "quality": {},
        "status": "publish_unknown",
        "created_at": "2026-11-11T10:00:00Z",
        "scheduled_at": None,
        "published_at": None,
        "reject_reason": None,
        "cover": None,
        "video": None,
        "er": None,
        "ctr": None,
        "impressions": None,
        "opens": None,
        "views": None,
        "tg_message_id": None,
    }
    detail = {
        **post,
        "text": "Полный текст тестовой публикации.",
        "body": "Полный текст тестовой публикации.",
        "cta": None,
        "sources": [],
        "claims": [],
        "hashtags": [],
        "format_reason": None,
        "external_url": None,
        "remote_id": None,
        "external_platform": None,
        "media": [],
    }

    reconcile_requests = []

    def mock_unknown_api(route):
        path = urlsplit(route.request.url).path
        if route.request.method == "POST" and path == "/api/posts/post-unknown-001/reconcile":
            reconcile_requests.append(json.loads(route.request.post_data or "{}"))
            payload = {"status": "scheduled"}
        elif path == "/api/posts":
            payload = {"posts": [post]}
        elif path == "/api/posts/post-unknown-001":
            payload = detail
        else:
            payload = {}
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(payload),
        )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        page.route("**/api/**", mock_unknown_api)
        page.goto(dashboard_url, wait_until="networkidle")
        posts_tab = page.locator('.tab[data-view="posts"]')
        posts_tab.click()
        page.locator('.view[data-view="posts"].is-active').wait_for(state="visible")
        page.locator('.tab[data-view="posts"][aria-selected="true"]').wait_for(state="visible")
        card = page.locator('.pcard[data-post="post-unknown-001"]')
        card.wait_for(state="visible", timeout=5000)
        card.click()

        drawer = page.locator("#drawer")
        drawer.locator("text=Результат не подтверждён.").wait_for()
        assert drawer.get_by_role("button", name="Да, пост уже есть").is_visible()
        assert drawer.get_by_role("button", name="Проверил: поста нет").is_visible()
        assert drawer.get_by_text(
            "Не нажимайте повтор, пока не проверите, появился ли пост."
        ).is_visible()
        assert drawer.get_by_role("button", name="Опубликовать сейчас").count() == 0
        assert drawer.get_by_role("button", name="Повторить после исправления").count() == 0

        page.once("dialog", lambda dialog: dialog.accept())
        with page.expect_request(
            lambda request: request.method == "POST"
            and urlsplit(request.url).path == "/api/posts/post-unknown-001/reconcile"
        ):
            drawer.get_by_role("button", name="Проверил: поста нет").click()
        assert reconcile_requests == [{"action": "retry_after_check", "confirm_not_published": True}]
        browser.close()



def test_dashboard_refresh_indicator_reports_partial_failure_and_recovers(dashboard_url):
    """A failed data source is visible, recoverable, and never mistaken for fresh."""
    from playwright.sync_api import sync_playwright

    trend_requests = 0
    trend_available = {"value": False}

    def mock_api_with_one_outage(route):
        nonlocal trend_requests
        path = urlsplit(route.request.url).path
        if path == "/api/trends":
            trend_requests += 1
            if not trend_available["value"]:
                route.fulfill(
                    status=503,
                    content_type="application/json",
                    body=json.dumps({"detail": "temporary test outage"}),
                )
                return
        _mock_api(route)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        page.route("**/api/**", mock_api_with_one_outage)
        page.goto(dashboard_url, wait_until="networkidle")

        sync_status = page.locator("#data-sync")
        sync_status.wait_for(state="visible")
        page.locator('#data-sync[data-state="partial"]').wait_for(timeout=10000)
        assert page.locator("#data-sync-label").inner_text() == "Часть данных устарела"
        assert "тренды" in page.locator("#data-sync-detail").inner_text().lower()
        page.wait_for_function("document.querySelector('#refresh-data')?.disabled === false")
        assert page.locator("#refresh-data").is_enabled()
        assert page.locator("#run-cycle").is_enabled()

        # Restore the source and use the visible control, not an artificial
        # page reload. All endpoints must be fetched again and the warning clear.
        trend_available["value"] = True
        page.locator("#refresh-data").click()
        page.locator('#data-sync[data-state="ok"]').wait_for(timeout=10000)
        assert page.locator("#data-sync-label").inner_text() == "Данные обновлены"
        assert "все" in page.locator("#data-sync-detail").inner_text().lower()
        assert trend_requests >= 2
        assert "Обновление панели восстановлено" in page.locator("#data-sync-announcement").inner_text()

        browser.close()


def test_post_drawer_trace_is_rendered_as_a_lifecycle_timeline(dashboard_url):
    """The existing post trace API is readable in the material drawer."""
    from playwright.sync_api import sync_playwright

    post = {
        "id": "trace-post-001",
        "channel": "vk",
        "channel_id": "vk",
        "channel_title": "Основной канал VK",
        "platform": "vk",
        "angle": "Проверка истории материала",
        "post_format": "news",
        "hook": "Как материал прошёл конвейер",
        "excerpt": "Проверяем путь от черновика до доставки.",
        "chars": 420,
        "quality": {},
        "status": "published",
        "created_at": "2026-11-11T19:00:00Z",
        "scheduled_at": "2026-11-11T20:00:00Z",
        "published_at": "2026-11-11T19:10:00Z",
        "reject_reason": None,
        "cover": None,
        "video": None,
        "er": None,
        "ctr": None,
        "impressions": None,
        "opens": None,
        "views": None,
        "tg_message_id": None,
    }
    detail = {
        **post,
        "text": "Полный текст материала для теста.",
        "body": "Полный текст материала для теста.",
        "cta": None,
        "sources": [],
        "claims": [],
        "hashtags": [],
        "format_reason": "Новостной формат выбран по контексту.",
        "external_url": None,
        "remote_id": None,
        "external_platform": None,
        "media": [],
    }
    trace = {
        "post": post["id"],
        "status": "published",
        "media": 1,
        "stats": {"views": 0, "impressions": 0, "opens": 0},
        "events": [
            {"at": "2026-11-11T19:00:00", "kind": "draft", "detail": "формат=news · материал создан"},
            {"at": "2026-11-11T19:01:00", "kind": "quality", "detail": "gate=passed · проверки завершены"},
            {"at": "2026-11-11T19:05:00", "kind": "scheduled", "detail": "попыток: 0"},
            {"at": "2026-11-11T19:09:00", "kind": "crosspost:vk", "detail": "posted · внешний ID получен"},
            {"at": "2026-11-11T19:10:00", "kind": "published", "detail": "remote_message_id=post-remote-001"},
        ],
    }

    def mock_trace_api(route):
        path = urlsplit(route.request.url).path
        if path == "/api/posts":
            payload = {"posts": [post]}
        elif path == "/api/posts/trace-post-001":
            payload = detail
        elif path == "/api/posts/trace-post-001/trace":
            payload = trace
        else:
            return _mock_api(route)
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        page.route("**/api/**", mock_trace_api)
        page.goto(dashboard_url, wait_until="networkidle")
        page.locator('.tab[data-view="posts"]').click()
        card = page.locator('.pcard[data-post="trace-post-001"]')
        card.wait_for(state="visible", timeout=5000)
        card.click()

        page.locator("#drawer").wait_for(state="visible")
        page.get_by_role("button", name="История").click()
        timeline = page.locator("#preview-slot .trace-block")
        timeline.wait_for(state="visible")
        assert timeline.get_by_role("heading", name="История материала").is_visible()
        assert timeline.locator(".trace-event").count() == 5
        assert timeline.get_by_text("Черновик создан").is_visible()
        assert timeline.get_by_text("Проверка качества").is_visible()
        assert timeline.get_by_text("Добавлен в расписание").is_visible()
        assert timeline.get_by_text("Попытка доставки · VK").is_visible()
        assert timeline.get_by_text("Публикация подтверждена").is_visible()
        assert timeline.get_by_text("Медиа: 1 файл").is_visible()
        assert timeline.locator("time[datetime]").count() == 5
        assert timeline.get_by_text("часовой пояс не указан").count() == 5
        assert page.get_by_role("heading", name="Короткие видео · Shorts / TikTok / VK Клипы").count() == 1
        assert page.get_by_role("button", name="Создать вертикальное видео").count() == 1

        browser.close()
