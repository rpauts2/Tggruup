"""Destination intelligence for community-based publishing.

Reddit uses its official OAuth Data API and machine-readable subreddit rules.
Pikabu and vc.ru use read-only public pages for topic/community discovery; their
browser workflows remain draft-only until a supported publisher is implemented.
No community is subscribed to, joined, voted in, or posted to by this module.
"""
from __future__ import annotations

import asyncio
import re
import time
from collections import Counter
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from tgfarm.core.text import content_tokens, normalize, token_jaccard
from tgfarm.db.models import Post
from tgfarm.db.session import session_scope

USER_AGENT = (
    "Mozilla/5.0 (compatible; TgFarmCommunityScout/1.0; "
    "+https://github.com/rpauts2/Tggruup)"
)
CACHE_TTL_S = 6 * 60 * 60
MAX_CANDIDATES = 12
MIN_TARGET_SCORE = 30

_VC_EXCLUDED_PATHS = {
    "/", "/rules", "/support", "/search", "/editor", "/settings",
    "/login", "/register", "/profile", "/feed", "/popular", "/fresh",
}
_PIKABU_EXCLUDED_SLUGS = {
    "create", "support_community", "moderator", "admin", "ads",
}
_VC_FALLBACK_TOPICS = (
    ("AI", "https://vc.ru/ai"),
    ("Сервисы", "https://vc.ru/services"),
    ("Маркетинг", "https://vc.ru/marketing"),
    ("Личный опыт", "https://vc.ru/life"),
    ("Деньги", "https://vc.ru/money"),
    ("Маркетплейсы", "https://vc.ru/marketplaces"),
    ("Разработка", "https://vc.ru/dev"),
    ("Техника", "https://vc.ru/tech"),
    ("Карьера", "https://vc.ru/career"),
)
_PIKABU_FALLBACK_COMMUNITIES = (
    ("technology", "https://pikabu.ru/community/technology"),
    ("web", "https://pikabu.ru/community/web"),
)

_cache_lock = asyncio.Lock()
_catalog_cache: dict[str, tuple[float, list[dict[str, str]]]] = {}


class CommunityTargetError(RuntimeError):
    """A safe, operator-facing community scouting error."""


def _load_post(post_public_id: str) -> dict[str, Any]:
    with session_scope() as session:
        post = session.query(Post).filter_by(public_id=post_public_id).one_or_none()
        if post is None:
            raise CommunityTargetError("Пост не найден в архиве TgFarm.")
        return {
            "public_id": post.public_id,
            "status": str(post.status or ""),
            "title": str(post.hook or "").strip(),
            "body": str(post.body or "").strip(),
            "sources": list(post.sources or []),
            "hashtags": list(post.hashtags or []),
            "angle": str(post.angle or ""),
        }


def _terms(text: str, limit: int = 18) -> list[str]:
    counts = Counter(content_tokens(text))
    return [term for term, _count in counts.most_common(limit)]


def _score_target(title: str, body: str, description: str, tags: list[str] | None = None) -> int:
    source = set(_terms(f"{title} {body[:5000]}", 32))
    target = set(_terms(" ".join([description, *(tags or [])]), 180))
    if not source or not target:
        return 0
    overlap = len(source & target) / max(1, min(len(source), 18))
    # Give the title additional importance; target name/description is still the
    # decisive signal. The score is a ranking aid, not a promise of acceptance.
    title_terms = set(_terms(title, 18))
    title_fit = len(title_terms & target) / max(1, len(title_terms))
    return max(0, min(100, round(72 * overlap + 28 * title_fit)))


def _community_slug(url: str, platform: str) -> str:
    parsed = urlparse(url)
    allowed = {"pikabu": {"pikabu.ru", "www.pikabu.ru"}, "vc_ru": {"vc.ru", "www.vc.ru"}}
    if parsed.scheme != "https" or parsed.hostname not in allowed[platform]:
        return ""
    match = re.fullmatch(r"/community/([A-Za-z0-9_-]{2,80})/?", parsed.path) if platform == "pikabu" else re.fullmatch(r"/([A-Za-z0-9_-]{1,50})/?", parsed.path)
    return match.group(1) if match else ""


async def _fetch_html(client: httpx.AsyncClient, url: str, *, platform: str) -> str:
    parsed = urlparse(url)
    hosts = {"pikabu": {"pikabu.ru", "www.pikabu.ru"}, "vc_ru": {"vc.ru", "www.vc.ru"}}
    if parsed.scheme != "https" or parsed.hostname not in hosts[platform]:
        raise CommunityTargetError("Запрос к неизвестному домену отклонён.")
    try:
        response = await client.get(url, follow_redirects=True)
        final = urlparse(str(response.url))
        if final.scheme != "https" or final.hostname not in hosts[platform]:
            raise CommunityTargetError("Площадка перенаправила запрос за пределы своего домена.")
        if response.status_code == 429:
            raise CommunityTargetError(f"{platform}: площадка ограничила частоту чтения. Повтори позже.")
        response.raise_for_status()
        return response.text
    except CommunityTargetError:
        raise
    except httpx.HTTPStatusError as exc:
        raise CommunityTargetError(
            f"{platform}: публичная страница вернула HTTP {exc.response.status_code}."
        ) from exc
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        raise CommunityTargetError(f"{platform}: не удалось загрузить публичную страницу.") from exc


def _page_summary(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    title = ""
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(" ", strip=True)
    if not title:
        title = str(
            (soup.find("meta", property="og:title") or {}).get("content")
            or (soup.find("title") or {}).get_text(" ", strip=True)
            or ""
        ).strip()
    description = str(
        (soup.find("meta", attrs={"name": "description"}) or {}).get("content")
        or (soup.find("meta", property="og:description") or {}).get("content")
        or ""
    ).strip()
    text = soup.get_text(" ", strip=True)
    subscribers = 0
    posts = 0
    for pattern, key in (
        (r"([\d\s\u00a0]+)\s+подписчик", "subscribers"),
        (r"([\d\s\u00a0]+)\s+пост(?:а|ов)?\b", "posts"),
    ):
        match = re.search(pattern, text, re.I)
        if match:
            try:
                number = int(re.sub(r"\D", "", match.group(1)))
            except ValueError:
                number = 0
            if key == "subscribers":
                subscribers = number
            else:
                posts = number
    return {
        "title": title[:180],
        "description": description[:1200],
        "text": text[:12_000],
        "subscribers": subscribers,
        "posts": posts,
        "soup": soup,
    }


def _extract_pikabu_links(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, dict[str, str]] = {}
    for anchor in soup.select('a[href^="/community/"]'):
        href = str(anchor.get("href") or "")
        url = urljoin("https://pikabu.ru", href.split("?")[0])
        slug = _community_slug(url, "pikabu")
        if not slug or slug.lower() in _PIKABU_EXCLUDED_SLUGS:
            continue
        name = anchor.get_text(" ", strip=True)
        if len(name) < 2:
            continue
        found.setdefault(slug.lower(), {"name": name[:180], "url": url})
    return list(found.values())[:MAX_CANDIDATES * 3]


def _extract_vc_topics(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, dict[str, str]] = {}
    for anchor in soup.select("a[href]"):
        href = str(anchor.get("href") or "")
        url = urljoin("https://vc.ru", href)
        parsed = urlparse(url)
        if parsed.hostname not in {"vc.ru", "www.vc.ru"}:
            continue
        path = parsed.path.rstrip("/") or "/"
        if path in _VC_EXCLUDED_PATHS or not re.fullmatch(r"/[A-Za-z0-9_-]{1,50}", path):
            continue
        name = anchor.get_text(" ", strip=True)
        if len(name) < 2 or len(name) > 80:
            continue
        found.setdefault(path.lower(), {"name": name[:80], "url": f"https://vc.ru{path}"})
    if not found:
        return [{"name": name, "url": url} for name, url in _VC_FALLBACK_TOPICS]
    return list(found.values())[:MAX_CANDIDATES]


def _pikabu_rules_flags(post: dict[str, Any], community: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    text = normalize(f"{community.get('description', '')} {community.get('rules', '')}")
    content = f"{post['title']}\n{post['body']}"
    body_without_urls = re.sub(r"https?://\S+", "", post["body"]).strip()
    if len(body_without_urls) < 120 and re.search(r"https?://", post["body"]):
        flags.append("Пикабу запрещает посты, состоящие только из ссылки или тизера. Нужен самостоятельный текст.")
    if community.get("closed"):
        flags.append("Сообщество закрытое/частное; право публикации не подтверждено.")
    if community.get("premoderation"):
        flags.append("В сообществе может действовать премодерация. Пост может ждать решения адмодеров.")
    if any(word in text for word in ("не по теме", "только по теме", "строго по тематике")):
        flags.append("В правилах есть строгое ограничение по тематике.")
    if any(word in text for word in ("реклама запрещ", "самореклам", "продвижение услуг", "не рекламировать")):
        flags.append("Правила ограничивают рекламу и самопродвижение.")
    if any(word in normalize(content) for word in ("политика", "президент", "выборы", "государство")):
        sources = post.get("sources") or []
        if not sources:
            flags.append("Для политической/новостной темы не найден источник подтверждения.")
    if not post.get("hashtags"):
        flags.append("Нужно выбрать не менее двух существующих тегов Пикабу.")
    flags.append("TgFarm создаёт AI-материалы: проверь специальный тег «Контент нейросетей»; «Моё» без проверки авторства не ставить.")
    return list(dict.fromkeys(flags))


def _vc_rules_flags(post: dict[str, Any], topic: dict[str, Any], rules_text: str) -> list[str]:
    flags: list[str] = []
    combined = normalize(f"{post['title']} {post['body']}")
    topic_text = normalize(f"{topic.get('title', '')} {topic.get('description', '')}")
    flags_global = normalize(rules_text)
    if len(set(_terms(combined)) & set(_terms(topic_text))) < 2:
        flags.append("Слабое совпадение с описанием выбранной темы; лучше не публиковать в неё.")
    promotional = ("купить", "заказать", "промокод", "реферальн", "переходите по ссылке", "наш сервис", "мы предлагаем")
    if any(term in combined for term in promotional):
        flags.append("В тексте обнаружены рекламные/продающие формулировки; правила vc.ru ограничивают промо.")
    if re.search(r"https?://", post["body"]) and any(
        phrase in flags_global for phrase in ("агрессивное размещение ссылок", "внешних ресурсов")
    ):
        flags.append("Проверь каждую внешнюю ссылку: массовое и агрессивное размещение запрещено.")
    if any(term in combined for term in ("президент", "выборы", "политика", "правительство")):
        flags.append("Политическая тема может быть неуместна для основной повестки vc.ru.")
    if re.search(r"\b\d+(?:[.,]\d+)?\s?(?:%|млн|млрд|руб|доллар)", combined) and not post.get("sources"):
        flags.append("В материале есть проверяемые цифры, но в архиве не сохранены источники.")
    flags.append("Перед размещением проверь правила выбранной темы и отсутствие повторной копии материала.")
    return list(dict.fromkeys(flags))


async def _cached_catalog(platform: str, loader) -> list[dict[str, str]]:
    now = time.monotonic()
    cached = _catalog_cache.get(platform)
    if cached and now - cached[0] < CACHE_TTL_S:
        return list(cached[1])
    async with _cache_lock:
        cached = _catalog_cache.get(platform)
        if cached and time.monotonic() - cached[0] < CACHE_TTL_S:
            return list(cached[1])
        data = await loader()
        _catalog_cache[platform] = (time.monotonic(), list(data))
        return list(data)


async def _pikabu_catalog() -> list[dict[str, str]]:
    async def load() -> list[dict[str, str]]:
        timeout = httpx.Timeout(18.0, connect=8.0)
        async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": USER_AGENT}) as client:
            html = await _fetch_html(client, "https://pikabu.ru/communities/all", platform="pikabu")
            links = _extract_pikabu_links(html)
            if not links:
                # The directory is public but may be hydrated by client JS.
                # Search results expose the same official /community/{slug} links.
                query = quote("технологии программирование наука")
                search_html = await _fetch_html(
                    client, f"https://pikabu.ru/search?q={query}", platform="pikabu"
                )
                links = _extract_pikabu_links(search_html)
            if not links:
                links = [
                    {"name": name, "url": url}
                    for name, url in _PIKABU_FALLBACK_COMMUNITIES
                ]
            return links[:MAX_CANDIDATES]
    return await _cached_catalog("pikabu", load)


async def _vc_catalog() -> tuple[list[dict[str, str]], str]:
    async def load() -> list[dict[str, str]]:
        timeout = httpx.Timeout(18.0, connect=8.0)
        async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": USER_AGENT}) as client:
            html = await _fetch_html(client, "https://vc.ru/rules", platform="vc_ru")
            topics = _extract_vc_topics(html)
            if not topics:
                topics = [{"name": name, "url": url} for name, url in _VC_FALLBACK_TOPICS]
            return topics[:MAX_CANDIDATES]
    topics = await _cached_catalog("vc_ru", load)
    timeout = httpx.Timeout(18.0, connect=8.0)
    async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": USER_AGENT}) as client:
        rules_html = await _fetch_html(client, "https://vc.ru/rules", platform="vc_ru")
    return topics, BeautifulSoup(rules_html, "html.parser").get_text(" ", strip=True)[:14_000]


async def _inspect_pikabu_candidate(
    client: httpx.AsyncClient,
    item: dict[str, str],
    post: dict[str, Any],
) -> dict[str, Any] | None:
    try:
        html = await _fetch_html(client, item["url"], platform="pikabu")
    except CommunityTargetError:
        return None
    page = _page_summary(html)
    text = page["text"]
    lower = normalize(text)
    rules_text = ""
    rules_match = re.search(r"правила сообщества(.{0,3000})", lower, re.I)
    if rules_match:
        rules_text = rules_match.group(1)[:2400]
    description = page["description"]
    if not description:
        community_match = re.search(r"о сообществе(.{0,1200})", text, re.I)
        description = community_match.group(1)[:900] if community_match else ""
    tags: list[str] = []
    tags_match = re.search(r"популярные теги(.{0,900})", text, re.I)
    if tags_match:
        tags = _terms(tags_match.group(1), 20)
    closed = "закрытое сообщество" in lower or "частное сообщество" in lower
    premoderation = "премодерац" in lower
    name = page["title"] or item["name"]
    score = _score_target(post["title"], post["body"], f"{name} {description}", tags)
    community = {
        "name": name,
        "slug": urlparse(item["url"]).path.rsplit("/", 1)[-1],
        "url": item["url"],
        "description": description[:900],
        "rules": rules_text,
        "tags": tags[:12],
        "posts": page["posts"],
        "subscribers": page["subscribers"],
        "closed": closed,
        "premoderation": premoderation,
    }
    flags = _pikabu_rules_flags(post, community)
    blocked = closed or score < MIN_TARGET_SCORE or any(
        "строгое ограничение по тематике" in flag for flag in flags
    )
    return {
        **community,
        "score": score,
        "fit_label": "Высокое совпадение" if score >= 65 else "Возможный вариант" if score >= MIN_TARGET_SCORE else "Слабое совпадение",
        "risk_flags": flags,
        "eligible": not blocked,
        "review_required": True,
        "automation": "draft_only",
    }


async def _inspect_vc_candidate(
    client: httpx.AsyncClient,
    item: dict[str, str],
    post: dict[str, Any],
    global_rules: str,
) -> dict[str, Any] | None:
    try:
        html = await _fetch_html(client, item["url"], platform="vc_ru")
    except CommunityTargetError:
        return None
    page = _page_summary(html)
    name = page["title"] or item["name"]
    description = page["description"]
    score = _score_target(post["title"], post["body"], f"{name} {description}")
    community = {
        "name": name,
        "slug": urlparse(item["url"]).path.strip("/"),
        "url": item["url"],
        "description": description[:900],
        "subscribers": page["subscribers"],
        "posts": page["posts"],
    }
    flags = _vc_rules_flags(post, community, global_rules)
    blocked = score < MIN_TARGET_SCORE
    return {
        **community,
        "score": score,
        "fit_label": "Высокое совпадение" if score >= 65 else "Возможный вариант" if score >= MIN_TARGET_SCORE else "Слабое совпадение",
        "risk_flags": flags,
        "rules_url": "https://vc.ru/rules",
        "eligible": not blocked,
        "review_required": True,
        "automation": "draft_only",
    }


async def recommend_places_for_post(post_public_id: str) -> dict[str, Any]:
    """Find and rank Reddit subreddits, Pikabu communities and vc.ru topics."""
    post = _load_post(str(post_public_id or "").strip())
    if post["status"] not in {"published", "gated", "scheduled"}:
        raise CommunityTargetError("Подбирать место можно только для сохранённого поста, прошедшего редактуру.")
    results: list[dict[str, Any]] = []

    # Reddit already has the richer official-API scout: it fetches community
    # descriptions, rules, post requirements and historical farm performance.
    try:
        from tgfarm.s9_growth.reddit_scout import search_communities

        query = " ".join(_terms(f"{post['title']} {post['body'][:1500]}", 7)[:6]) or post["title"][:100]
        reddit = await search_communities(query, post_public_id=post_public_id, limit=6)
        candidates = reddit.get("candidates") or []
        for item in candidates:
            item["eligible"] = bool(
                int(item.get("score") or 0) >= 65
                and not item.get("risk_flags")
                and not item.get("over18")
                and item.get("subreddit_type") == "public"
                and item.get("submission_type") in {"any", "self"}
            )
            item["automation"] = "automatic" if item["eligible"] else "manual_review"
        results.append({
            "platform": "reddit",
            "status": "ok",
            "candidates": candidates[:4],
            "recommended": next((item for item in candidates if item.get("eligible")), None),
            "publisher": "official_api",
            "note": "Reddit API: описание, правила модераторов и ограничения отправки проверены. Перед автоотправкой S9 повторяет проверку.",
        })
    except Exception as exc:  # noqa: BLE001 - other platform scans remain useful
        results.append({
            "platform": "reddit",
            "status": "needs_setup" if "OAuth" in str(exc) else "unavailable",
            "candidates": [],
            "recommended": None,
            "note": str(exc)[:260],
        })

    try:
        pikabu_links = await _pikabu_catalog()
        timeout = httpx.Timeout(18.0, connect=8.0)
        async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": USER_AGENT}) as client:
            semaphore = asyncio.Semaphore(5)

            async def inspect_pikabu(item):
                async with semaphore:
                    return await _inspect_pikabu_candidate(client, item, post)

            inspected = await asyncio.gather(
                *(inspect_pikabu(item) for item in pikabu_links[:MAX_CANDIDATES]),
                return_exceptions=True,
            )
        pikabu = [
            item for item in inspected
            if isinstance(item, dict)
        ]
        pikabu.sort(key=lambda item: (item["eligible"], item["score"], item["subscribers"]), reverse=True)
        eligible = [item for item in pikabu if item["eligible"]]
        results.append({
            "platform": "pikabu",
            "status": "ok" if pikabu else "unavailable",
            "candidates": pikabu[:4],
            "recommended": eligible[0] if eligible else None,
            "publisher": "browser_draft_only",
            "note": (
                "Сообщество выбирается по описанию, тегам и правилам. TgFarm подготовит черновик в браузере; "
                "выбор сообщества и отправку нужно подтвердить в редакторе."
                if pikabu else "Не удалось получить список сообществ Пикабу."
            ),
        })
    except Exception as exc:  # noqa: BLE001
        results.append({
            "platform": "pikabu",
            "status": "unavailable",
            "candidates": [],
            "recommended": None,
            "publisher": "browser_draft_only",
            "note": str(exc)[:260],
        })

    try:
        vc_topics, global_rules = await _vc_catalog()
        timeout = httpx.Timeout(18.0, connect=8.0)
        async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": USER_AGENT}) as client:
            semaphore = asyncio.Semaphore(5)

            async def inspect_vc(item):
                async with semaphore:
                    return await _inspect_vc_candidate(client, item, post, global_rules)

            inspected = await asyncio.gather(
                *(inspect_vc(item) for item in vc_topics[:MAX_CANDIDATES]),
                return_exceptions=True,
            )
        vc = [item for item in inspected if isinstance(item, dict)]
        vc.sort(key=lambda item: (item["eligible"], item["score"], item["subscribers"]), reverse=True)
        eligible = [item for item in vc if item["eligible"]]
        results.append({
            "platform": "vc_ru",
            "status": "ok" if vc else "unavailable",
            "candidates": vc[:4],
            "recommended": eligible[0] if eligible else None,
            "publisher": "browser_draft_only",
            "note": (
                "Выбрана тема по её публичному описанию; глобальные правила vc.ru проверены. "
                "TgFarm подготовит редакторский черновик, а ты должен подтвердить тему и отправку."
                if vc else "Не удалось получить каталог тем vc.ru."
            ),
        })
    except Exception as exc:  # noqa: BLE001
        results.append({
            "platform": "vc_ru",
            "status": "unavailable",
            "candidates": [],
            "recommended": None,
            "publisher": "browser_draft_only",
            "note": str(exc)[:260],
        })

    return {
        "post_public_id": post_public_id,
        "title": post["title"],
        "targets": results,
        "rules_checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "all_destinations_verified": all(item["status"] == "ok" for item in results),
        "disclaimer": "Публичные правила и карточки сообществ могут измениться. TgFarm показывает проверенные на момент запроса данные; публикация в Pikabu/vc.ru остаётся черновиком до появления надёжного автоматического адаптера.",
    }
