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
            "can_publish": False,
            "cycle_health": {"state": "idle", "stale": False, "started_at": None, "finished_at": None},
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
                    overview = page.locator('.view[data-view="overview"]')
                    assert overview.locator('[aria-label="Операционная картина"]').count() == 1
                    assert overview.locator('[aria-label="Публикации и доставка"]').count() == 1
                    positions = overview.evaluate("""(root) => [
                        "pipeline-visual", "report-chart", "publication-decision",
                        "backlog-list", "portfolio-insights"
                    ].map((id) => [...root.querySelectorAll("[id]")].indexOf(root.querySelector(`#${id}`)))
                    """)
                    assert positions == sorted(positions)
                    assert overview.get_by_role("heading", name="Состояние конвейера").is_visible()
                    operations = overview.locator("#command-status")
                    assert operations.is_visible()
                    assert operations.get_by_role("heading", name="Состояние фермы").is_visible()
                    assert operations.locator(".command-status-item").count() == 4
                    assert operations.get_by_text("OFF", exact=True).is_visible()
                    assert operations.get_by_text("Остановлен", exact=True).is_visible()
                    assert operations.get_by_text("Заблокирована", exact=True).is_visible()
                    assert operations.get_by_text("Не запускался", exact=True).is_visible()
                    next_action = operations.locator("#command-next-action")
                    assert next_action.get_attribute("data-goto") == "platforms"
                    assert next_action.get_by_text("Проверить настройки публикации").is_visible()
                    assert overview.get_by_role("heading", name="Результаты редакции").is_visible()
                    grid = page.locator(".command-shortcuts-grid")
                    assert grid.evaluate("(el) => getComputedStyle(el).display") == "grid"
                    card = grid.locator("button").first
                    assert card.evaluate("(el) => getComputedStyle(el).borderRadius") != "0px"
                    assert page.locator("#kpis").get_by_text("из 67 активно").count() == 1
                    attention_shortcut = page.locator('.command-shortcuts-grid [data-goto="review"]')
                    assert attention_shortcut.get_by_text("Требует внимания").is_visible()
                    attention_shortcut.click()
                    assert page.locator('.view[data-view="review"].is-active').is_visible()
                    page.locator('.tab[data-view="overview"]').click()
                    assert page.locator('.view[data-view="overview"].is-active').is_visible()
                if view == "posts":
                    failed_filter = page.locator('#post-filters button[data-status="failed"]')
                    draft_filter = page.locator('#post-filters button[data-status="draft"]')
                    assert failed_filter.is_visible()
                    assert draft_filter.is_visible()
                    failed_filter.click()
                    assert "is-active" in (failed_filter.get_attribute("class") or "")
                    draft_filter.click()
                    assert "is-active" in (draft_filter.get_attribute("class") or "")
                    page.locator('#post-filters button[data-status=""]').click()
                    assert page.get_by_role("heading", name="Очередь пока пуста").is_visible()
                    page.get_by_role("button", name="Открыть планирование").click()
                    assert page.locator('.view[data-view="plan"].is-active').is_visible()
                if view == "platforms":
                    assert page.locator(".platform-card").count() == 2
                    assert page.locator(".platform-card").first.evaluate("(el) => el.getBoundingClientRect().width >= 360")
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
        assert page.locator("#command-status-grid").evaluate("(el) => getComputedStyle(el).gridTemplateColumns.split(' ').length") == 2
        assert page.locator("#command-status").is_visible()
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


def test_command_center_attention_actions_route_by_alert_code(dashboard_url):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        page.route("**/api/**", _mock_api)
        page.route("**/api/status", lambda route: route.fulfill(json={
            "mode": "live",
            "scheduler_running": True,
            "can_publish": True,
            "cycle_health": {
                "state": "failed", "stale": False,
                "started_at": "2026-11-12T10:00:00+00:00",
                "finished_at": "2026-11-12T10:01:00+00:00",
            },
            "alerts": [
                {"level": "error", "code": "dzen_auth", "message": "Требуется повторный вход"},
                {"level": "error", "code": "cycle_failed", "message": "Последний цикл завершился ошибкой"},
            ],
            "kpis": {
                "sources": 1, "sources_total": 1, "ready_clusters": 0, "clusters": 0,
                "scheduled": 0, "gated": 0, "published": 0, "posts": 0, "rejected": 0,
                "reject_rate": None, "avg_er": None, "covers": 0, "videos": 0, "crossposts": 0,
            },
        }))
        response = page.goto(dashboard_url, wait_until="networkidle")
        assert response is not None and response.status == 200
        page.locator("#command-status-grid .command-status-item").nth(3).get_by_text(
            "Сбой", exact=True
        ).wait_for()
        assert page.locator("#command-next-action").get_attribute("data-goto") == "connect"
        attention = page.locator("#alert-banner")
        assert attention.is_visible()
        buttons = attention.locator(".attention-row .attention-open")
        assert buttons.count() == 2
        assert buttons.nth(0).get_attribute("data-goto") == "connect"
        assert buttons.nth(1).get_attribute("data-goto") == "runs"
        buttons.nth(0).click()
        assert page.locator('.view[data-view="connect"].is-active').is_visible()
        page.locator('.tab[data-view="overview"]').click()
        attention.locator(".attention-open").nth(1).click()
        assert page.locator('.view[data-view="runs"].is-active').is_visible()
        browser.close()


def test_command_status_marks_retained_snapshot_stale_and_first_failure_unavailable(dashboard_url):
    from playwright.sync_api import sync_playwright

    status_failed = False
    payload = {
        "mode": "live",
        "scheduler_running": True,
        "can_publish": True,
        "cycle_health": {
            "state": "ok", "stale": False,
            "started_at": "2026-11-12T10:00:00+00:00",
            "finished_at": "2026-11-12T10:01:00+00:00",
        },
        "alerts": [],
        "kpis": {
            "sources": 1, "sources_total": 1, "ready_clusters": 0, "clusters": 0,
            "scheduled": 0, "gated": 0, "published": 0, "posts": 0, "rejected": 0,
            "reject_rate": None, "avg_er": None, "covers": 0, "videos": 0, "crossposts": 0,
        },
    }

    def status_route(route):
        if status_failed:
            route.fulfill(status=503, json={"detail": "status service unavailable"})
        else:
            route.fulfill(json=payload)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        page.route("**/api/**", _mock_api)
        page.route("**/api/status", status_route)
        page.goto(dashboard_url, wait_until="networkidle")
        page.wait_for_function("document.querySelector('#command-status')?.dataset.state === 'fresh'")
        assert page.locator("#command-status").get_by_text("LIVE", exact=True).is_visible()
        status_failed = True
        page.evaluate("window.tgfarmRefresh()")
        page.wait_for_function(
            "document.querySelector('#command-status-freshness')?.dataset.state === 'stale'"
        )
        assert page.locator("#command-status").get_by_text("LIVE", exact=True).is_visible()
        assert page.locator("#command-status-freshness").get_by_text(
            "Показан последний успешный снимок", exact=False
        ).is_visible()
        first_failure = browser.new_page(viewport={"width": 1440, "height": 960})
        first_failure.route("**/api/**", _mock_api)
        first_failure.route(
            "**/api/status",
            lambda route: route.fulfill(status=503, json={"detail": "status service unavailable"}),
        )
        first_failure.goto(dashboard_url, wait_until="networkidle")
        first_failure.wait_for_function(
            "document.querySelector('#command-status')?.dataset.state === 'error'"
        )
        assert first_failure.locator("#command-status").get_by_text(
            "Состояние фермы неизвестно", exact=False
        ).is_visible()
        assert first_failure.locator("#command-next-action").is_hidden()
        browser.close()


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
    """The material drawer loads and refreshes its lifecycle trace automatically."""
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

        drawer = page.locator("#drawer")
        drawer.wait_for(state="visible")
        timeline = drawer.locator("#trace-slot .trace-block")
        timeline.locator(".trace-event").first.wait_for(state="visible", timeout=5000)
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

def test_review_board_shows_unknown_measurements_and_cancels_safe_retry(dashboard_url):
    """The review card must not invent zeroes or retry after a dismissed confirmation."""
    from playwright.sync_api import sync_playwright

    retry_requests: list[str] = []
    dialog_messages: list[str] = []

    def mock_review_api(route):
        path = urlsplit(route.request.url).path
        if path == "/api/posts/review-unknown-001":
            payload = {
                "id": "review-unknown-001",
                "public_id": "review-unknown-001",
                "hook": "Материал с неподтверждённой доставкой",
                "body": "Полный текст материала для сверки.",
                "text": "Полный текст материала для сверки.",
                "status": "publish_unknown",
                "platform": "vk",
                "channel_id": "vk",
                "channel_title": "Основной канал VK",
                "post_format": "news",
                "chars": 420,
                "quality": {},
                "sources": [],
                "claims": [],
                "hashtags": [],
                "media": [],
                "cover": None,
                "video": None,
                "reject_reason": None,
                "created_at": "2026-11-11T10:00:00Z",
            }
            route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))
            return
        if path == "/api/posts/review-unknown-001/trace":
            payload = {
                "status": "publish_unknown",
                "media": 0,
                "events": [{
                    "kind": "draft",
                    "at": "2026-11-11T09:55:00Z",
                    "detail": "Материал создан перед передачей в очередь.",
                }],
                "stats": None,
            }
            route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))
            return
        if path == "/api/review/queue":
            payload = {
                "total": 1,
                "counts": {"unknown": 1, "terminal": 0, "held": 0},
                "titles": {
                    "unknown": "Неизвестный результат",
                    "terminal": "Финальные отказы",
                    "held": "Без слота",
                },
                "buckets": {
                    "unknown": [{
                        "public_id": "review-unknown-001",
                        "platform": "vk",
                        "hook": "Материал с неподтверждённой доставкой",
                        "reason": "Площадка не вернула подтверждение",
                        "age_hours": None,
                        "attempts": None,
                        "actions": ["retry_after_check"],
                    }],
                    "terminal": [],
                    "held": [],
                },
            }
            route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))
            return
        if route.request.method == "POST" and path.endswith("/reconcile"):
            retry_requests.append(path)
            route.fulfill(status=200, content_type="application/json", body="{}")
            return
        _mock_api(route)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        page.route("**/api/**", mock_review_api)
        page.on("dialog", lambda dialog: (dialog_messages.append(dialog.message), dialog.dismiss()))
        page.goto(dashboard_url, wait_until="networkidle")

        page.locator('.tab[data-view="review"]').click()
        card = page.locator(".review-card")
        card.wait_for(state="visible", timeout=5000)
        assert card.get_by_text("возраст не измерен", exact=False).is_visible()
        assert card.get_by_text("попыток: нет отчёта", exact=False).is_visible()
        assert card.get_by_text("0 ч", exact=False).count() == 0
        assert card.get_by_text("попыток 0", exact=False).count() == 0

        open_button = card.get_by_role("button", name="Открыть материал")
        open_button.click()
        drawer = page.locator("#drawer")
        drawer.wait_for(state="visible")
        assert drawer.get_attribute("role") == "dialog"
        assert drawer.get_attribute("aria-modal") == "true"
        material_heading = drawer.get_by_role("heading", name="Материал с неподтверждённой доставкой")
        material_heading.wait_for(state="visible", timeout=5000)
        assert drawer.get_by_text("Результат не подтверждён.", exact=False).is_visible()
        drawer.locator("#trace-slot .trace-event").wait_for(state="visible", timeout=5000)
        assert drawer.get_by_text("Материал создан перед передачей в очередь.", exact=True).is_visible()
        drawer.locator(".drawer-close").click()
        drawer.wait_for(state="hidden")
        assert open_button.evaluate("(el) => document.activeElement === el")

        open_button.click()
        drawer.wait_for(state="visible")
        page.keyboard.press("Escape")
        drawer.wait_for(state="hidden")
        assert open_button.evaluate("(el) => document.activeElement === el")

        card.get_by_role("button", name="Проверил, поста нет — в очередь").click()
        assert len(dialog_messages) == 1
        assert "лично проверили" in dialog_messages[0]
        assert retry_requests == []
        browser.close()


def test_publication_queue_opens_material_and_restores_focus_and_filter(dashboard_url):
    """The queue's keyboard entry point opens the same detail/trace lifecycle and returns to context."""
    from playwright.sync_api import sync_playwright

    post = {
        "id": "queue-item-001",
        "public_id": "queue-item-001",
        "hook": "Очередной материал для проверки",
        "excerpt": "Краткий анонс материала для карточки очереди.",
        "text": "Полный текст материала для проверки.",
        "body": "Полный текст материала для проверки.",
        "status": "scheduled",
        "platform": "vk",
        "channel_id": "vk-main",
        "channel_title": "Основной канал VK",
        "post_format": "news",
        "chars": 420,
        "quality": {},
        "sources": [],
        "claims": [],
        "hashtags": [],
        "media": [],
        "cover": None,
        "video": None,
        "reject_reason": None,
        "created_at": "2026-11-11T10:00:00Z",
        "scheduled_at": "2026-11-12T14:00:00Z",
        "published_at": None,
        "impressions": None,
        "opens": None,
        "views": None,
        "er": None,
        "ctr": None,
        "plan_entry_id": None,
        "plan_slot_at": None,
        "plan_public_id": None,
    }

    def mock_queue_api(route):
        path = urlsplit(route.request.url).path
        if path == "/api/posts":
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"posts": [post]}))
            return
        if path == "/api/posts/queue-item-001":
            route.fulfill(status=200, content_type="application/json", body=json.dumps(post))
            return
        if path == "/api/posts/queue-item-001/trace":
            payload = {
                "status": "scheduled",
                "media": 0,
                "events": [{
                    "kind": "scheduled",
                    "at": "2026-11-11T10:30:00Z",
                    "detail": "Материал поставлен в очередь публикации.",
                }],
                "stats": None,
            }
            route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))
            return
        _mock_api(route)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        page.route("**/api/**", mock_queue_api)
        page.goto(dashboard_url, wait_until="networkidle")

        page.locator('.tab[data-view="posts"]').click()
        scheduled_filter = page.locator('#post-filters button[data-status="scheduled"]')
        scheduled_filter.click()
        card = page.locator('.pcard[data-post="queue-item-001"]')
        card.wait_for(state="visible", timeout=5000)
        assert card.get_attribute("role") == "button"
        assert card.get_attribute("tabindex") == "0"

        card.focus()
        card.press("Enter")
        drawer = page.locator("#drawer")
        drawer.wait_for(state="visible")
        drawer.get_by_role("heading", name="Очередной материал для проверки").wait_for(timeout=5000)
        drawer.locator("#trace-slot .trace-event").wait_for(state="visible", timeout=5000)
        assert drawer.get_by_text("Материал поставлен в очередь публикации.", exact=True).is_visible()

        page.keyboard.press("Escape")
        drawer.wait_for(state="hidden")
        assert card.evaluate("(el) => document.activeElement === el")
        assert scheduled_filter.get_attribute("class").find("is-active") >= 0
        browser.close()

def test_material_drawer_ignores_detail_response_from_previous_open(dashboard_url):
    """Closing A and opening B must make a late detail response for A harmless."""
    from playwright.sync_api import sync_playwright

    posts = [
        {
            "id": "drawer-race-a", "channel": "vk", "channel_id": "vk",
            "channel_title": "VK", "platform": "vk", "post_format": "news",
            "hook": "Материал A", "excerpt": "Первый материал", "chars": 100,
            "quality": {}, "status": "scheduled", "created_at": "2026-11-12T08:00:00Z",
            "scheduled_at": "2026-11-12T10:00:00Z", "published_at": None,
            "reject_reason": None, "cover": None, "video": None, "er": None,
            "ctr": None, "impressions": None, "opens": None, "views": None,
            "tg_message_id": None,
        },
        {
            "id": "drawer-race-b", "channel": "telegram", "channel_id": "telegram",
            "channel_title": "Telegram", "platform": "telegram", "post_format": "news",
            "hook": "Материал B", "excerpt": "Второй материал", "chars": 120,
            "quality": {}, "status": "scheduled", "created_at": "2026-11-12T08:01:00Z",
            "scheduled_at": "2026-11-12T10:01:00Z", "published_at": None,
            "reject_reason": None, "cover": None, "video": None, "er": None,
            "ctr": None, "impressions": None, "opens": None, "views": None,
            "tg_message_id": None,
        },
    ]

    def detail(post):
        return {
            **post, "text": f"Полный текст: {post['hook']}", "body": f"Полный текст: {post['hook']}",
            "cta": None, "sources": [], "claims": [], "hashtags": [],
            "format_reason": "Тестовый материал.", "external_url": None,
            "remote_id": None, "external_platform": None, "media": [],
        }

    def mock_api(route):
        path = urlsplit(route.request.url).path
        if path == "/api/posts":
            payload = {"posts": posts}
        else:
            return _mock_api(route)
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    hold_detail_fetches = """
    (() => {
      const originalFetch = window.fetch.bind(window);
      window.__tgfarmPendingDetails = [];
      window.fetch = (input, init) => {
        const url = typeof input === "string" ? input : (input && input.url) || "";
        const match = String(url).match(/\\/api\\/posts\\/([^/?]+)$/);
        if (!match) return originalFetch(input, init);
        const id = decodeURIComponent(match[1]);
        return new Promise((resolve) => {
          window.__tgfarmPendingDetails.push({ id, resolve });
        });
      };
      window.__tgfarmResolveDetail = (id, payload) => {
        const pending = window.__tgfarmPendingDetails.find((item) => item.id === id);
        if (!pending) throw new Error("No pending detail request for " + id);
        window.__tgfarmPendingDetails = window.__tgfarmPendingDetails.filter((item) => item !== pending);
        pending.resolve(new Response(JSON.stringify(payload), {
          status: 200, headers: { "Content-Type": "application/json" }
        }));
      };
    })();
    """

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        page.add_init_script(hold_detail_fetches)
        page.route("**/api/**", mock_api)
        page.goto(dashboard_url, wait_until="networkidle")

        drawer = page.locator("#drawer")
        page.locator("#overview-queue [data-post='drawer-race-a']").click()
        page.wait_for_function(
            "() => window.__tgfarmPendingDetails.some((item) => item.id === 'drawer-race-a')"
        )
        page.keyboard.press("Escape")
        drawer.wait_for(state="hidden")

        page.locator("#overview-queue [data-post='drawer-race-b']").click()
        page.wait_for_function(
            "() => window.__tgfarmPendingDetails.some((item) => item.id === 'drawer-race-b')"
        )
        page.evaluate(
            "payload => window.__tgfarmResolveDetail('drawer-race-b', payload)",
            detail(posts[1]),
        )
        drawer.get_by_role("heading", name="Материал B").wait_for(state="visible")
        assert drawer.get_by_text("Полный текст: Материал B").is_visible()

        # The stale A response arrives last. It must not replace B or reopen the drawer.
        page.evaluate(
            "payload => window.__tgfarmResolveDetail('drawer-race-a', payload)",
            detail(posts[0]),
        )
        page.wait_for_timeout(100)
        assert drawer.get_by_role("heading", name="Материал B").is_visible()
        assert drawer.get_by_text("Полный текст: Материал B").is_visible()
        assert drawer.get_by_role("heading", name="Материал A").count() == 0

        browser.close()

def test_specialist_workspaces_mark_retained_snapshots_stale_and_recover(dashboard_url):
    """Trends and sources distinguish an outage from a real empty response."""
    from playwright.sync_api import sync_playwright

    available = {"trends": True, "sources": True}
    trends = [{
        "id": "trend-stale-001", "label": "Сохранившийся тестовый тренд",
        "niche": "технологии", "items": 4, "outlets": ["Издание"],
        "status": "ready", "heat": 0.8, "trend_phase": "rising", "acceleration": 0.2,
    }]
    sources = [{
        "id": "source-stale-001", "name": "Сохранившийся тестовый источник",
        "kind": "rss", "weight": 1, "last_fetched_at": "2026-11-12T08:00:00Z",
        "cooldown_active": False, "cooldown_until": None, "failures": 0,
        "last_status": "ok", "last_error": None,
    }]

    def mock_api(route):
        path = urlsplit(route.request.url).path
        if path in ("/api/trends", "/api/sources"):
            key = path.removeprefix("/api/")
            if not available[key]:
                route.fulfill(
                    status=503,
                    content_type="application/json",
                    body=json.dumps({"detail": f"{key} test outage"}),
                )
                return
            payload = {"trends": trends} if key == "trends" else {"sources": sources}
            route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))
            return
        _mock_api(route)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        page.route("**/api/**", mock_api)
        page.goto(dashboard_url, wait_until="networkidle")

        trends_notice = page.locator("#trends-freshness")
        sources_notice = page.locator("#sources-freshness")
        page.locator('.tab[data-view="trends"]').click()
        page.locator("#trends-list").get_by_text("Сохранившийся тестовый тренд").wait_for()
        assert trends_notice.is_hidden()
        page.locator('.tab[data-view="sources"]').click()
        page.locator("#sources-list").get_by_text("Сохранившийся тестовый источник").wait_for()
        assert sources_notice.is_hidden()

        available["trends"] = False
        available["sources"] = False
        page.locator("#refresh-data").click()
        page.wait_for_function(
            "() => document.querySelector('#trends-freshness')?.dataset.state === 'stale' "
            "&& document.querySelector('#sources-freshness')?.dataset.state === 'stale'",
            timeout=10000,
        )

        # Last good content remains available for inspection, but is labelled stale.
        page.locator('.tab[data-view="trends"]').click()
        assert trends_notice.is_visible()
        assert trends_notice.get_by_text("Данные устарели").is_visible()
        assert "503" in trends_notice.inner_text()
        assert page.locator("#trends-list").get_by_text("Сохранившийся тестовый тренд").is_visible()

        page.locator('.tab[data-view="sources"]').click()
        assert sources_notice.is_visible()
        assert sources_notice.get_by_text("Данные устарели").is_visible()
        assert "503" in sources_notice.inner_text()
        assert page.locator("#sources-list").get_by_text("Сохранившийся тестовый источник").is_visible()

        available["trends"] = True
        available["sources"] = True
        page.locator("#refresh-data").click()
        page.wait_for_function(
            "() => document.querySelector('#trends-freshness')?.hidden "
            "&& document.querySelector('#sources-freshness')?.hidden"
        )
        page.locator('.tab[data-view="trends"]').click()
        assert trends_notice.is_hidden()
        assert page.locator("#trends-list").get_by_text("Сохранившийся тестовый тренд").is_visible()
        page.locator('.tab[data-view="sources"]').click()
        assert sources_notice.is_hidden()
        assert page.locator("#sources-list").get_by_text("Сохранившийся тестовый источник").is_visible()

        # A successful empty response is not an outage and must not inherit a stale badge.
        trends.clear()
        sources.clear()
        page.locator("#refresh-data").click()
        page.locator("#sources-list").get_by_text("Источники не заданы").wait_for()
        assert sources_notice.is_hidden()
        assert page.locator("#sources-list").get_by_text("Сохранившийся тестовый источник").count() == 0
        page.locator('.tab[data-view="trends"]').click()
        page.locator("#trends-list").get_by_text("Трендов нет — тепло ниже порога").wait_for()
        assert trends_notice.is_hidden()
        assert page.locator("#trends-list").get_by_text("Сохранившийся тестовый тренд").count() == 0

        # A first-read failure has no old snapshot to preserve; report unavailable, not stale.
        available["trends"] = False
        available["sources"] = False
        fresh_page = browser.new_page(viewport={"width": 1440, "height": 960})
        fresh_page.route("**/api/**", mock_api)
        fresh_page.goto(dashboard_url, wait_until="networkidle")
        fresh_page.wait_for_function(
            "() => document.querySelector('#trends-freshness')?.dataset.state === 'error' "
            "&& document.querySelector('#sources-freshness')?.dataset.state === 'error'",
            timeout=10000,
        )
        fresh_page.locator('.tab[data-view="trends"]').click()
        assert fresh_page.locator("#trends-freshness").get_by_text("Данные раздела недоступны").is_visible()
        assert fresh_page.locator("#trends-freshness").get_by_text("Данные устарели").count() == 0
        fresh_page.locator('.tab[data-view="sources"]').click()
        assert fresh_page.locator("#sources-freshness").get_by_text("Данные раздела недоступны").is_visible()

        browser.close()

def test_delivery_workspace_marks_journal_and_health_stale_on_refresh_failure(dashboard_url):
    """The delivery journal and health snapshot never masquerade as current."""
    from playwright.sync_api import sync_playwright

    available = {"crossposts": True, "health": True}
    entries = [{
        "post": "Материал для проверки доставки",
        "platform": "vk",
        "status": "posted",
        "at": "2026-11-12T08:00:00Z",
        "remote_id": "vk-123",
        "url": "https://vk.com/wall-1_123",
    }]
    health = {
        "state": "healthy",
        "generated_at": "2026-11-12T08:00:00Z",
        "totals": {"targets": 1, "posted": 1, "needs_attention": 0, "claims": 0, "stale_claims": 0},
        "platforms": [{
            "platform": "vk", "state": "healthy", "posted": 1,
            "scheduled": 0, "unknown": 0, "permanent": 0,
        }],
    }

    def mock_api(route):
        path = urlsplit(route.request.url).path
        if path in ("/api/crossposts", "/api/crossposts/health"):
            key = "crossposts" if path == "/api/crossposts" else "health"
            if not available[key]:
                route.fulfill(
                    status=503,
                    content_type="application/json",
                    body=json.dumps({"detail": f"{key} test outage"}),
                )
                return
            payload = {"crossposts": entries} if key == "crossposts" else health
            route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))
            return
        _mock_api(route)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        page.route("**/api/**", mock_api)
        page.goto(dashboard_url, wait_until="networkidle")
        page.locator('.view[data-view="overview"].is-active').wait_for(state="visible")

        journal_notice = page.locator("#crossposts-freshness")
        health_notice = page.locator("#crosspost-health-freshness")
        page.locator("#crossposts-list").get_by_text("Материал для проверки доставки").wait_for()
        page.locator("#crosspost-health").get_by_text("есть успешные доставки").first.wait_for()
        assert journal_notice.is_hidden()
        assert health_notice.is_hidden()

        available["crossposts"] = False
        available["health"] = False
        page.locator("#refresh-data").click()
        page.wait_for_function(
            "() => document.querySelector('#crossposts-freshness')?.dataset.state === 'stale' "
            "&& document.querySelector('#crosspost-health-freshness')?.dataset.state === 'stale'",
            timeout=10000,
        )
        assert journal_notice.is_visible()
        assert health_notice.is_visible()
        assert journal_notice.get_by_text("Данные устарели").is_visible()
        assert health_notice.get_by_text("Данные устарели").is_visible()
        assert "503" in journal_notice.inner_text()
        assert "503" in health_notice.inner_text()
        assert page.locator("#crossposts-list").get_by_text("Материал для проверки доставки").is_visible()
        assert page.locator("#crosspost-health").get_by_text("есть успешные доставки").first.is_visible()

        available["crossposts"] = True
        available["health"] = True
        page.locator("#refresh-data").click()
        page.wait_for_function(
            "() => document.querySelector('#crossposts-freshness')?.hidden "
            "&& document.querySelector('#crosspost-health-freshness')?.hidden",
            timeout=10000,
        )

        # A successful empty journal is a real empty state, not a stale one.
        entries.clear()
        page.locator("#refresh-data").click()
        page.locator("#crossposts-list").get_by_text("Кросспостов пока нет").wait_for()
        assert journal_notice.is_hidden()

        # First-read failures have no last-known-good snapshot to preserve.
        available["crossposts"] = False
        available["health"] = False
        fresh_page = browser.new_page(viewport={"width": 1440, "height": 960})
        fresh_page.route("**/api/**", mock_api)
        fresh_page.goto(dashboard_url, wait_until="networkidle")
        fresh_page.locator('.view[data-view="overview"].is-active').wait_for(state="visible")
        fresh_page.wait_for_function(
            "() => document.querySelector('#crossposts-freshness')?.dataset.state === 'error' "
            "&& document.querySelector('#crosspost-health-freshness')?.dataset.state === 'error'",
            timeout=10000,
        )
        assert fresh_page.locator("#crossposts-freshness").get_by_text("Данные раздела недоступны").is_visible()
        assert fresh_page.locator("#crosspost-health-freshness").get_by_text("Данные раздела недоступны").is_visible()

        browser.close()


def test_editorial_plan_plan_history_and_queue_keep_independent_freshness(dashboard_url):
    """Plan, revision history and open-order queue fail and recover independently."""
    from playwright.sync_api import sync_playwright

    available = {"plan": True, "history": True, "queue": True}
    plan = {
        "status": "active", "public_id": "plan-current-001",
        "created_at": "2026-11-12T08:00:00Z", "horizon_days": 7,
        "summary": {"total": 1, "produce": 1, "schedule": 0, "hold": 0, "coverage_note": "Покрытие измерено."},
        "entries": [{"priority": 1, "action": "produce", "title": "Тестовый плановый материал",
                     "platform": "telegram", "channel_title": "Редакционный канал", "format": "post",
                     "reasons": ["Проверка актуальности"]}],
    }
    history = {"plans": [{"public_id": "history-revision-001", "created_at": "2026-11-11T08:00:00Z",
                          "summary": {"total": 3, "produce": 2, "hold": 1}}]}
    queue = {"queue": [{"label": "Тема ждёт бронирования", "cluster_public_id": "cluster-queue-001",
                        "niche": "технологии", "heat": 0.8, "score": 75,
                        "awaiting_booking": True, "queue_position": 1}]}
    apply_calls = []
    fail_plan_after_apply = False

    def mock_api(route):
        path = urlsplit(route.request.url).path
        mapping = {
            "/api/editorial/plan": ("plan", plan),
            "/api/editorial/plan/history": ("history", history),
            "/api/editorial": ("queue", queue),
        }
        if path in mapping:
            key, payload = mapping[path]
            if not available[key]:
                route.fulfill(status=503, content_type="application/json",
                              body=json.dumps({"detail": f"{key} test outage"}))
                return
            route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))
            return
        if path == "/api/editorial/plan/refresh" and route.request.method == "POST":
            apply_calls.append(path)
            route.fulfill(status=200, content_type="application/json", body=json.dumps(plan))
            if fail_plan_after_apply:
                available["plan"] = False
            return
        _mock_api(route)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        page.route("**/api/**", mock_api)
        page.goto(dashboard_url, wait_until="networkidle")
        page.locator('.tab[data-view="plan"]').click()
        page.locator("#plan-entries").get_by_text("Тестовый плановый материал").wait_for()
        page.locator("#plan-history").get_by_text("history-revision-001").wait_for()
        page.locator("#plan-open-orders").get_by_text("Тема ждёт бронирования").wait_for()
        notices = {
            "plan": page.locator("#plan-freshness"),
            "history": page.locator("#plan-history-freshness"),
            "queue": page.locator("#plan-open-orders-freshness"),
        }
        for notice in notices.values():
            assert notice.is_hidden()
        assert page.locator("#plan-apply").is_enabled()

        available["plan"] = False
        page.locator("#plan-refresh").click()
        page.wait_for_function("() => document.querySelector('#plan-freshness')?.dataset.state === 'stale'")
        assert notices["plan"].is_visible()
        assert "503" in notices["plan"].inner_text()
        assert page.locator("#plan-entries").get_by_text("Тестовый плановый материал").is_visible()
        assert page.locator("#plan-apply").is_disabled()
        assert notices["history"].is_hidden()
        assert notices["queue"].is_hidden()
        page.locator("#plan-apply").evaluate("(el) => el.click()")
        assert apply_calls == []

        available["plan"] = True
        available["history"] = False
        page.locator("#plan-refresh").click()
        page.wait_for_function("() => document.querySelector('#plan-history-freshness')?.dataset.state === 'stale'")
        assert notices["plan"].is_hidden()
        assert notices["history"].is_visible()
        assert "503" in notices["history"].inner_text()
        assert page.locator("#plan-history").get_by_text("history-revision-001").is_visible()
        assert page.locator("#plan-apply").is_enabled()
        assert notices["queue"].is_hidden()

        available["history"] = True
        available["queue"] = False
        page.locator("#plan-refresh").click()
        page.wait_for_function("() => document.querySelector('#plan-open-orders-freshness')?.dataset.state === 'stale'")
        assert notices["plan"].is_hidden()
        assert notices["history"].is_hidden()
        assert notices["queue"].is_visible()
        assert "503" in notices["queue"].inner_text()
        assert page.locator("#plan-open-orders").get_by_text("Тема ждёт бронирования").is_visible()

        available["queue"] = True
        page.locator("#plan-refresh").click()
        page.wait_for_function(
            "() => document.querySelector('#plan-freshness')?.hidden "
            "&& document.querySelector('#plan-history-freshness')?.hidden "
            "&& document.querySelector('#plan-open-orders-freshness')?.hidden"
        )
        assert page.locator("#plan-apply").is_enabled()

        # A successful POST is not enough to announce a verified current plan
        # when the immediate follow-up read fails. Keep the stale notice and
        # avoid the ordinary success toast.
        fail_plan_after_apply = True
        page.locator("#plan-apply").click()
        page.wait_for_function(
            "() => document.querySelector('#plan-freshness')?.dataset.state === 'stale'"
        )
        assert apply_calls == ["/api/editorial/plan/refresh"]
        assert page.locator("#toasts").get_by_text(
            "Сервер ответил успешно, но повторное чтение плана не подтвердило результат. "
            "Состояние помечено устаревшим; обнови его перед следующим действием.",
            exact=True,
        ).is_visible()
        assert page.locator("#toasts").get_by_text("План зафиксирован и повторно прочитан").count() == 0
        assert page.locator("#plan-apply").is_disabled()

        fresh_page = browser.new_page(viewport={"width": 1440, "height": 960})
        available.update({"plan": False, "history": False, "queue": False})
        fresh_page.route("**/api/**", mock_api)
        fresh_page.goto(dashboard_url, wait_until="networkidle")
        fresh_page.locator('.tab[data-view="plan"]').click()
        fresh_page.wait_for_function(
            "() => ['plan-freshness','plan-history-freshness','plan-open-orders-freshness']"
            ".every(id => document.querySelector('#' + id)?.dataset.state === 'error')"
        )
        assert fresh_page.locator("#plan-freshness").get_by_text("Данные раздела недоступны").is_visible()
        assert fresh_page.locator("#plan-history-freshness").get_by_text("Данные раздела недоступны").is_visible()
        assert fresh_page.locator("#plan-open-orders-freshness").get_by_text("Данные раздела недоступны").is_visible()
        assert fresh_page.locator("#plan-apply").is_disabled()
        assert fresh_page.locator("#plan-entries").get_by_text("Тестовый плановый материал").count() == 0

        browser.close()
