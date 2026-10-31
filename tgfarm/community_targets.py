"""Read-only target selection for Pikabu communities and vc.ru topics."""
from __future__ import annotations

import asyncio
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

from tgfarm.core.text import content_tokens, normalize

PIKABU_DIRECTORY = "https://pikabu.ru/communities/all"
PIKABU_RULES = "https://pikabu.ru/information/rules"
VC_DIRECTORY = "https://vc.ru/support"
VC_RULES = "https://vc.ru/rules"
USER_AGENT = "Mozilla/5.0 (compatible; TgFarmEditorial/1.0)"
MIN_SCORE = 28
MAX_PIKABU = 24
MAX_VC = 10


class _Reader(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_on = False
        self.title_parts: list[str] = []
        self.description = ""
        self.links: list[tuple[str, str]] = []
        self._link: tuple[str, list[str]] | None = None
        self._heading = False
        self._buffer: list[str] = []
        self.chunks: list[str] = []
        self._title = ""

    def handle_starttag(self, tag: str, attrs) -> None:
        attrs = {str(k).lower(): str(v or "") for k, v in attrs}
        tag = tag.lower()
        if tag == "title":
            self.title_on = True
        if tag == "meta" and attrs.get("name", "").lower() == "description":
            self.description = attrs.get("content", "")[:1200]
        if tag == "meta" and attrs.get("property", "").lower() == "og:description":
            self.description = self.description or attrs.get("content", "")[:1200]
        if tag == "a" and attrs.get("href"):
            self._link = (attrs["href"], [])
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li"}:
            self._flush()
            self._heading = tag.startswith("h")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title":
            self.title_on = False
        if tag == "a" and self._link:
            href, pieces = self._link
            label = " ".join(" ".join(pieces).split())
            if href and label:
                self.links.append((href, label[:180]))
            self._link = None
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li"}:
            self._flush()

    def handle_data(self, data: str) -> None:
        value = " ".join(str(data or "").split())
        if not value:
            return
        if self.title_on:
            self.title_parts.append(value)
        if self._link:
            self._link[1].append(value)
        self._buffer.append(value)

    def _flush(self) -> None:
        line = " ".join(" ".join(self._buffer).split())
        if line:
            self.chunks.append(line)
        self._buffer.clear()

    def result(self, url: str) -> dict[str, Any]:
        self._flush()
        return {
            "url": url,
            "title": " ".join(self.title_parts).strip()[:240],
            "description": self.description,
            "text": "\n".join(self.chunks)[:24000],
            "chunks": self.chunks[:3000],
            "links": self.links[:2000],
        }


async def _fetch(url: str) -> dict[str, Any]:
    import httpx

    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in {"pikabu.ru", "www.pikabu.ru", "vc.ru", "www.vc.ru"}:
        raise ValueError("Unexpected destination host.")
    async with httpx.AsyncClient(
        timeout=18.0,
        follow_redirects=True,
        max_redirects=4,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
    ) as client:
        response = await client.get(url)
        response.raise_for_status()
        if "html" not in str(response.headers.get("content-type") or "").lower():
            raise ValueError("Target directory did not return HTML.")
        reader = _Reader()
        reader.feed(response.text[:2_500_000])
        result = reader.result(str(response.url))
        final = urlparse(result["url"])
        if final.scheme != "https" or final.hostname not in {"pikabu.ru", "www.pikabu.ru", "vc.ru", "www.vc.ru"}:
            raise ValueError("Public page redirected outside the platform.")
        return result


def _score(post_text: str, candidate_text: str) -> tuple[int, list[str]]:
    a, b = set(content_tokens(post_text)), set(content_tokens(candidate_text))
    if not a or not b:
        return 0, []
    hits = sorted(a & b, key=lambda x: (-len(x), x))
    score = max(0, min(100, round(100 * len(hits) / max(1, min(6, len(a))) * 1.8)))
    return score, hits[:8]


def _rules(chunks: list[str], full_text: str) -> str:
    for index, line in enumerate(chunks):
        if any(word in normalize(line) for word in ("правила сообщества", "правила публикации", "правила")):
            selected = []
            for item in chunks[index + 1:]:
                if any(normalize(item).startswith(x) for x in (
                    "похожие посты", "подписчики", "популярные посты", "все посты",
                    "описание", "темы", "публикации", "комментарии",
                )):
                    break
                selected.append(item)
                if sum(map(len, selected)) > 3000:
                    break
            if selected:
                return "\n".join(selected)[:3000]
    match = re.search(
        r"(?:правила сообщества|правила публикации|правила)\s*[:\-]?\s*(.{40,3000})",
        full_text or "",
        flags=re.IGNORECASE | re.DOTALL,
    )
    return re.sub(r"\s+", " ", match.group(1)).strip()[:3000] if match else ""


def _pikabu_risks(
    rules: str,
    post: dict[str, Any],
    global_rules: str,
) -> tuple[list[str], list[str]]:
    """Return warnings separately from hard policy/fit blockers."""
    joined = normalize(rules)
    content = normalize(f"{post['title']}\n{post['body']}")
    warnings: list[str] = []
    blockers: list[str] = []
    if not rules:
        blockers.append("Не удалось прочитать правила этого сообщества.")
    advertising_bans = (
        "самореклама запрещ", "запрещена самореклама", "реклама запрещена",
        "запрещена реклама", "рекламировать запрещено", "не рекламируйте",
        "реклама не допускается", "продвижение запрещено", "запрещено продвижение",
    )
    if any(phrase in joined for phrase in advertising_bans):
        blockers.append("Правила сообщества явно запрещают рекламу или самопродвижение.")
    elif any(word in joined for word in ("самореклам", "реклама", "продвижение", "промо")):
        warnings.append("В правилах есть требования к рекламе или самопродвижению; проверь соответствие.")
    ai_bans = (
        "нейросетевый контент запрещ", "контент нейросетей запрещ",
        "генерация нейросетью запрещ", "запрещены нейросети",
        "запрещен ai-контент", "ai-контент запрещен",
    )
    if any(phrase in joined for phrase in ai_bans):
        blockers.append("Правила сообщества явно запрещают AI-контент.")
    elif any(word in joined for word in ("нейросет", "искусственн", "генерац", "ai-контент")):
        warnings.append("Правила упоминают AI-контент; проверь допустимость и специальную маркировку.")
    if any(word in content for word in ("жесть", "смерт", "кров", "увеч", "насили")):
        warnings.append("Проверь специальный тег «Жесть» и требования к небезопасному контенту.")
    if any(word in content for word in ("политик", "выбор", "государств", "президент", "войн")):
        warnings.append("Проверь обязательные специальные теги и правила для политических публикаций.")
    if any(word in content for word in ("нейросет", "сгенерирован", "chatgpt", "искусственн")):
        warnings.append("Проверь специальный тег «Контент нейросетей».")
    if re.search(r"https?://", content):
        warnings.append(
            "В материале есть внешние ссылки: Кодекс Пикабу запрещает самостоятельные посты-ссылки и "
            "перенос продолжения на внешний ресурс. Источники должны быть вспомогательными."
        )
    return list(dict.fromkeys(warnings)), list(dict.fromkeys(blockers))

async def _pikabu(post: dict[str, Any]) -> dict[str, Any]:
    from tgfarm.community_drafts import CommunityDraftError

    directory, global_page = await asyncio.gather(
        _fetch(PIKABU_DIRECTORY), _fetch(PIKABU_RULES), return_exceptions=True
    )
    if isinstance(directory, Exception):
        raise CommunityDraftError(f"Не удалось прочитать каталог Пикабу ({type(directory).__name__}).")
    global_rules = global_page.get("text", "") if isinstance(global_page, dict) else ""
    links: dict[str, str] = {}
    for href, label in directory["links"]:
        absolute = urljoin(directory["url"], href)
        parsed = urlparse(absolute)
        match = re.fullmatch(r"/community/([A-Za-z0-9_-]+)(?:/.*)?", parsed.path)
        if parsed.scheme != "https" or parsed.hostname not in {"pikabu.ru", "www.pikabu.ru"} or not match:
            continue
        slug = match.group(1)
        if slug.lower() not in {"all", "create", "my", "new", "popular", "rules"}:
            links.setdefault(slug, label)
    if not links:
        raise CommunityDraftError("Каталог Пикабу не вернул ссылки на сообщества. Цель не выбрана.")

    post_text = f"{post['title']}\n{post['body']}"
    first = sorted(
        ((_score(post_text, label)[0], slug, label) for slug, label in links.items()),
        reverse=True,
    )[:MAX_PIKABU]
    post_words = set(content_tokens(post_text))
    semaphore = asyncio.Semaphore(4)

    async def read_community(initial: int, slug: str, label: str) -> dict[str, Any] | None:
        url = f"https://pikabu.ru/community/{slug}"
        async with semaphore:
            try:
                page = await _fetch(url)
            except Exception:
                return None
        rule_text = _rules(page["chunks"], page["text"])
        description = page["description"] or page["text"][:900]
        score, matches = _score(post_text, f"{label}\n{page['title']}\n{description}\n{rule_text}")
        warnings, blockers = _pikabu_risks(rule_text, post, global_rules)
        visible_tags = []
        for href, tag_label in page["links"]:
            tag_url = urlparse(urljoin(page["url"], href))
            if tag_url.scheme == "https" and tag_url.hostname in {"pikabu.ru", "www.pikabu.ru"} and (
                "/tag/" in tag_url.path or "/tags/" in tag_url.path
            ):
                visible_tags.append(tag_label)
        visible_tags = list(dict.fromkeys(visible_tags))[:20]
        suggested_tags = [
            tag for tag in visible_tags
            if set(content_tokens(tag)) & post_words
        ][:8]
        return {
            "target_type": "community",
            "name": label,
            "slug": slug,
            "title": page["title"] or label,
            "url": url,
            "description": description[:900],
            "rules": rule_text[:2200],
            "rules_available": bool(rule_text),
            "tags": visible_tags,
            "suggested_tags": suggested_tags,
            "score": max(0, min(100, round(score * .8 + initial * .2))),
            "matches": matches,
            "risks": warnings,
            "blockers": blockers,
            "recommended": bool(rule_text) and not blockers and score >= MIN_SCORE,
        }

    candidates = [
        item for item in await asyncio.gather(*(
            read_community(initial, slug, label)
            for initial, slug, label in first
        ))
        if item is not None
    ]
    candidates.sort(key=lambda x: (x["recommended"], x["score"], x["name"]), reverse=True)
    best = next((item for item in candidates if item["recommended"]), None)
    return {
        "platform": "pikabu",
        "post_public_id": post["public_id"],
        "selected": best,
        "candidates": candidates[:5],
        "general_notes": [
            "Выбор сделан по названию, описанию, правилам и совпадению темы.",
            "Если правила не прочитаны или найден риск, сообщество не назначается.",
            "Кодекс Пикабу запрещает самостоятельные посты-ссылки и перенос продолжения на внешний ресурс.",
            "Проверь специальные теги «Моё», «Политика», «18+», «Жесть» и «Контент нейросетей», когда они применимы.",
        ],
        "rules_source": PIKABU_RULES,
        "auto_publish_supported": False,
        "detail": (
            f"Рекомендуемое сообщество: {best['name']} ({best['score']}/100)."
            if best else
            "Нет сообщества с прочитанными правилами и достаточным совпадением. Не назначаю цель автоматически."
        ),
    }


def _vc_links(page: dict[str, Any]) -> list[tuple[str, str]]:
    result: dict[str, str] = {}
    skip = {"/", "/support", "/rules", "/about", "/advertising", "/editor", "/login", "/registration"}
    for href, label in page["links"]:
        absolute = urljoin(page["url"], href)
        parsed = urlparse(absolute)
        path = parsed.path.rstrip("/") or "/"
        if parsed.scheme != "https" or parsed.hostname not in {"vc.ru", "www.vc.ru"}:
            continue
        if path in skip or path.count("/") != 1 or len(label.strip()) < 2:
            continue
        if re.fullmatch(r"/[a-z0-9_-]{2,32}", path):
            result.setdefault(path, label.strip())
    return list(result.items())


def _vc_risks(post: dict[str, Any], rules_text: str = "") -> list[str]:
    content = normalize(f"{post['title']}\n{post['body']}")
    published_rules = normalize(rules_text or "")
    risks = []
    if any(word in content for word in ("промокод", "купи", "закажи", "скидка", "наш продукт", "наш сервис")):
        risks.append("Материал может быть воспринят как продвижение; проверь правила и тип аккаунта.")
    if any(word in content for word in ("политик", "выбор", "президент", "партия", "войн")):
        risks.append("Для vc.ru политическая тема должна быть раскрыта в контексте бизнеса/экономики.")
    if any(word in published_rules for word in ("коммерческ", "реклам", "ограничен")):
        risks.append("В текущих правилах есть отдельные ограничения для коммерческих/рекламных публикаций.")
    return list(dict.fromkeys(risks))


async def _vc(post: dict[str, Any]) -> dict[str, Any]:
    from tgfarm.community_drafts import CommunityDraftError

    directory_result, rules_result = await asyncio.gather(
        _fetch(VC_DIRECTORY), _fetch(VC_RULES), return_exceptions=True
    )
    if isinstance(directory_result, Exception):
        raise CommunityDraftError(
            f"Не удалось получить список тем vc.ru ({type(directory_result).__name__})."
        )
    directory = directory_result
    rules_text = rules_result.get("text", "") if isinstance(rules_result, dict) else ""
    topics = _vc_links(directory)
    if not topics:
        raise CommunityDraftError("Не удалось прочитать актуальный каталог тем vc.ru. Ничего не угадываю.")
    post_text = f"{post['title']}\n{post['body']}"
    first = sorted(
        ((_score(post_text, label)[0], path, label) for path, label in topics),
        reverse=True,
    )[:MAX_VC]
    semaphore = asyncio.Semaphore(4)

    async def read_topic(initial: int, path: str, label: str) -> dict[str, Any] | None:
        url = urljoin("https://vc.ru", path)
        async with semaphore:
            try:
                page = await _fetch(url)
            except Exception:
                return None
        description = page["description"] or page["text"][:1000]
        score, matches = _score(post_text, f"{label}\n{page['title']}\n{description}")
        risks = _vc_risks(post, rules_text)
        return {
            "target_type": "topic",
            "name": label,
            "slug": path.lstrip("/"),
            "title": page["title"] or label,
            "url": url,
            "description": description[:1000],
            "score": max(0, min(100, round(score * .82 + initial * .18))),
            "matches": matches,
            "risks": risks,
            "rules_available": bool(rules_text),
            "recommended": score >= MIN_SCORE,
        }

    candidates = [
        item for item in await asyncio.gather(*(
            read_topic(initial, path, label)
            for initial, path, label in first
        ))
        if item is not None
    ]
    candidates.sort(key=lambda x: (x["score"], x["name"]), reverse=True)
    best = candidates[0] if candidates and candidates[0]["recommended"] else None
    return {
        "platform": "vc_ru",
        "post_public_id": post["public_id"],
        "selected": best,
        "candidates": candidates[:5],
        "general_notes": [
            "Тема выбрана по актуальному списку разделов и описанию самой темы.",
            "Общие правила vc.ru перечитаны перед подбором; на тематических страницах могут действовать дополнительные требования модерации.",
            "Если материал не подходит разделам, справка vc.ru рекомендует личный блог или «Офтоп».",
            "Неформатные, рекламные и плохо оформленные публикации могут быть скрыты или возвращены в блог.",
            "Для коммерческих аккаунтов действует отдельный месячный лимит; TgFarm не может определить тип аккаунта по одной сессии.",
        ],
        "rules_source": VC_RULES,
        "auto_publish_supported": False,
        "detail": (
            f"Рекомендуемая тема: {best['name']} ({best['score']}/100)."
            if best else
            "Ни одна актуальная тема не набрала достаточного совпадения. Не назначаю раздел автоматически."
        ),
    }


async def recommend_community_target(platform_id: str, post_public_id: str) -> dict[str, Any]:
    from tgfarm.community_drafts import CommunityDraftError, _load_post

    platform_id = str(platform_id or "").strip().lower()
    if platform_id not in {"vc_ru", "pikabu"}:
        raise CommunityDraftError("Подбор места доступен только для vc.ru и Пикабу.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,40}", str(post_public_id or "")):
        raise CommunityDraftError("Некорректный идентификатор поста.")
    post = await asyncio.to_thread(_load_post, post_public_id)
    if not post.get("title") or not post.get("body"):
        raise CommunityDraftError("У поста нет заголовка или текста для тематического подбора.")
    return await (_vc(post) if platform_id == "vc_ru" else _pikabu(post))
