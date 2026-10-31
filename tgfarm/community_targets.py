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
MAX_PIKABU = 8
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
    score = max(0, min(100, round(100 * len(hits) / max(1, min(12, len(a))) * 1.8)))
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


def _pikabu_risks(rules: str, post: dict[str, Any], global_rules: str) -> list[str]:
    joined = normalize(f"{rules}\n{global_rules}")
    content = normalize(f"{post['title']}\n{post['body']}")
    risks: list[str] = []
    if not rules:
        risks.append("Не удалось прочитать правила этого сообщества.")
    if any(word in joined for word in ("самореклам", "реклама", "продвижение", "промо")):
        risks.append("Правила сообщества могут ограничивать рекламу или самопродвижение.")
    if any(word in joined for word in ("нейросет", "искусственн", "генерац", "ai-контент")):
        risks.append("Правила упоминают AI-контент; требуется ручная проверка допуска.")
    if any(word in content for word in ("жесть", "смерт", "кров", "увеч", "насили")):
        risks.append("Проверь специальный тег «Жесть» и требования к небезопасному контенту.")
    if any(word in content for word in ("политик", "выбор", "государств", "президент", "войн")):
        risks.append("Проверь обязательные специальные теги и правила для политических публикаций.")
    if any(word in content for word in ("нейросет", "сгенерирован", "chatgpt", "искусственн")):
        risks.append("Проверь специальный тег «Контент нейросетей».")
    if re.search(r"https?://", content):
        risks.append("В материале есть внешняя ссылка: Кодекс Пикабу запрещает посты-ссылки и перенос продолжения на сторонний ресурс.")
    return list(dict.fromkeys(risks))


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
    candidates: list[dict[str, Any]] = []
    for initial, slug, label in first:
        url = f"https://pikabu.ru/community/{slug}"
        try:
            page = await _fetch(url)
        except Exception:
            continue
        rule_text = _rules(page["chunks"], page["text"])
        description = page["description"] or page["text"][:900]
        score, matches = _score(post_text, f"{label}\n{page['title']}\n{description}\n{rule_text}")
        risks = _pikabu_risks(rule_text, post, global_rules)
        candidates.append({
            "target_type": "community",
            "name": label,
            "slug": slug,
            "title": page["title"] or label,
            "url": url,
            "description": description[:900],
            "rules": rule_text[:2200],
            "rules_available": bool(rule_text),
            "score": max(0, min(100, round(score * .8 + initial * .2))),
            "matches": matches,
            "risks": risks,
            "recommended": bool(rule_text) and not risks and score >= MIN_SCORE,
        })
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


def _vc_risks(post: dict[str, Any]) -> list[str]:
    content = normalize(f"{post['title']}\n{post['body']}")
    risks = []
    if any(word in content for word in ("промокод", "купи", "закажи", "скидка", "наш продукт", "наш сервис")):
        risks.append("Материал может быть воспринят как продвижение; проверь правила и тип аккаунта.")
    if any(word in content for word in ("политик", "выбор", "президент", "партия", "войн")):
        risks.append("Для vc.ru политическая тема должна быть раскрыта в контексте бизнеса/экономики.")
    return risks


async def _vc(post: dict[str, Any]) -> dict[str, Any]:
    from tgfarm.community_drafts import CommunityDraftError

    directory = await _fetch(VC_DIRECTORY)
    topics = _vc_links(directory)
    if not topics:
        raise CommunityDraftError("Не удалось прочитать актуальный каталог тем vc.ru. Ничего не угадываю.")
    post_text = f"{post['title']}\n{post['body']}"
    first = sorted(
        ((_score(post_text, label)[0], path, label) for path, label in topics),
        reverse=True,
    )[:MAX_VC]
    candidates = []
    for initial, path, label in first:
        url = urljoin("https://vc.ru", path)
        try:
            page = await _fetch(url)
        except Exception:
            continue
        description = page["description"] or page["text"][:1000]
        score, matches = _score(post_text, f"{label}\n{page['title']}\n{description}")
        candidates.append({
            "target_type": "topic",
            "name": label,
            "slug": path.lstrip("/"),
            "title": page["title"] or label,
            "url": url,
            "description": description[:1000],
            "score": max(0, min(100, round(score * .82 + initial * .18))),
            "matches": matches,
            "risks": _vc_risks(post),
            "recommended": score >= MIN_SCORE,
        })
    candidates.sort(key=lambda x: (x["score"], x["name"]), reverse=True)
    best = candidates[0] if candidates and candidates[0]["recommended"] else None
    return {
        "platform": "vc_ru",
        "post_public_id": post["public_id"],
        "selected": best,
        "candidates": candidates[:5],
        "general_notes": [
            "Тема выбрана по актуальному списку разделов и описанию самой темы.",
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
