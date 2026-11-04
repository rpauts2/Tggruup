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
    expected_hosts = (
        {"pikabu.ru", "www.pikabu.ru"}
        if parsed.hostname in {"pikabu.ru", "www.pikabu.ru"}
        else {"vc.ru", "www.vc.ru"}
    )
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
        if final.scheme != "https" or final.hostname not in expected_hosts:
            raise ValueError("Public page redirected outside the selected platform.")
        return result


def _target_editorial_brief(
    platform_id: str,
    post: dict[str, Any],
    target: dict[str, Any],
) -> dict[str, Any]:
    """Build an evidence-backed community brief; never treat warnings as permission."""
    rules = str(target.get("rules") or "").strip()
    blockers = list(dict.fromkeys(str(x) for x in target.get("blockers", []) if str(x).strip()))
    warnings = list(dict.fromkeys(str(x) for x in target.get("risks", []) if str(x).strip()))
    matches = list(dict.fromkeys(str(x) for x in target.get("matches", []) if str(x).strip()))
    tags = list(dict.fromkeys(str(x) for x in target.get("suggested_tags", []) if str(x).strip()))
    platform_name = "Пикабу" if platform_id == "pikabu" else "vc.ru"
    destination = str(target.get("name") or target.get("title") or platform_name)
    constraints = [line.strip() for line in re.split(r"[\n.!?]+", rules) if line.strip()][:8]
    return {
        "role": "community_editor",
        "platform": platform_id,
        "destination": destination,
        "topic_fit": int(target.get("score") or 0),
        "matched_terms": matches[:8],
        "suggested_tags": tags[:8],
        "editorial_angle": (
            f"Раскрыть тему «{post.get('title', '').strip()}» через интерес аудитории «{destination}»"
            if not blockers else
            "Не назначать публикацию: сначала устранить блокирующие условия"
        ),
        "rules_read": bool(rules),
        "constraints": constraints,
        "warnings": warnings,
        "blockers": blockers,
        "decision": "eligible_for_automatic_pipeline" if target.get("recommended") and not blockers else "hold",
        "publication_authorized": False,
        "decision_note": (
            "Тема и правила прошли проверку конвейера; перед отправкой они проверяются повторно."
            if target.get("recommended") and not blockers else
            "Не публиковать: цель не прошла проверку правил или тематического соответствия."
        ),
    }


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
    global_policy = normalize(global_rules or "")
    combined_policy = f"{global_policy} {joined}".strip()
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
    explicit_promotion_ban = (
        "самопродвижение" in combined_policy
        and any(word in combined_policy for word in ("запрещ", "не допуска", "не разреш"))
    )
    explicit_advertising_ban = (
        "реклама" in combined_policy
        and any(word in combined_policy for word in ("запрещ", "не допуска", "не разреш"))
    )
    if any(phrase in combined_policy for phrase in advertising_bans) or explicit_promotion_ban or explicit_advertising_ban:
        blockers.append("Общий Кодекс Пикабу или правила сообщества содержат запрет рекламы и самопродвижения.")
    elif any(word in combined_policy for word in ("самореклам", "реклама", "продвижение", "промо")):
        warnings.append("В правилах есть требования к рекламе или самопродвижению; проверь соответствие.")
    ai_bans = (
        "нейросетевый контент запрещ", "контент нейросетей запрещ",
        "генерация нейросетью запрещ", "запрещены нейросети",
        "запрещен ai-контент", "ai-контент запрещен",
    )
    if any(phrase in combined_policy for phrase in ai_bans):
        blockers.append("Общий Кодекс Пикабу или правила сообщества явно запрещают AI-контент.")
    elif any(word in combined_policy for word in ("нейросет", "искусственн", "генерац", "ai-контент")):
        warnings.append("Правила упоминают AI-контент; проверь допустимость и специальную маркировку.")
    if any(word in content for word in ("жесть", "смерт", "кров", "увеч", "насили")):
        warnings.append("Проверь специальный тег «Жесть» и требования к небезопасному контенту.")
    if any(word in content for word in ("политик", "выбор", "государств", "президент", "войн")):
        warnings.append("Проверь обязательные специальные теги и правила для политических публикаций.")
    if any(word in content for word in ("нейросет", "сгенерирован", "chatgpt", "искусственн")):
        warnings.append("Проверь специальный тег «Контент нейросетей».")
    if re.search(r"https?://", content):
        warnings.append(
            "В материале найдена внешняя ссылка или ссылки: Кодекс Пикабу запрещает самостоятельные "
            "посты-ссылки и перенос продолжения на внешний ресурс. Источники должны быть вспомогательными."
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
        if not global_rules:
            blockers.append("Не удалось прочитать общие правила и Кодекс Пикабу.")
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
            "editorial_brief": _target_editorial_brief("pikabu", post, {
                "name": label, "score": max(0, min(100, round(score * .8 + initial * .2))),
                "matches": matches, "suggested_tags": suggested_tags, "rules": rule_text,
                "risks": warnings, "blockers": blockers,
                "recommended": bool(rule_text) and not blockers and score >= MIN_SCORE,
            }),
        }

    candidates = [
        item for item in await asyncio.gather(*(
            read_community(initial, slug, label)
            for initial, slug, label in first
        ))
        if item is not None
    ]
    candidates.sort(key=lambda x: (x["recommended"], x["score"], x["name"]), reverse=True)
    best = next((
        item for item in candidates
        if item["recommended"] and _valid_pikabu_community_tags(item.get("suggested_tags") or [])
    ), None)
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
        "auto_publish_supported": best is not None,
        "detail": (
            f"Автоматическая публикация возможна: {best['name']} ({best['score']}/100)."
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


def _vc_rule_assessment(
    post: dict[str, Any],
    global_rules: str,
    topic_rules: str,
) -> tuple[list[str], list[str]]:
    """Separate review warnings from explicit topic-policy blockers."""
    warnings = _vc_risks(post, global_rules)
    blockers: list[str] = []
    if not topic_rules:
        blockers.append(
            "Не удалось прочитать правила выбранной темы vc.ru; автоматический выбор этой темы запрещён."
        )

    policy_text = normalize(f"{global_rules}\n{topic_rules}")
    content = normalize(f"{post['title']}\n{post['body']}")
    promotion_bans = (
        "реклама запрещена",
        "запрещена реклама",
        "самореклама запрещена",
        "запрещено самопродвижение",
        "самопродвижение запрещено",
        "не допускается реклама",
        "реклама не допускается",
        "рекламные публикации запрещены",
        "коммерческая реклама запрещена",
    )
    promotional_signals = (
        "купи",
        "купить",
        "покупайте",
        "закажи",
        "заказать",
        "оформить заказ",
        "промокод",
        "скидка",
        "оставьте заявку",
        "запишитесь",
        "регистрируйтесь",
        "переходите на сайт",
        "наш сервис доступен",
        "наш продукт доступен",
    )
    has_promotion = any(signal in content for signal in promotional_signals)
    has_promotion_ban = any(rule in policy_text for rule in promotion_bans)
    if has_promotion and has_promotion_ban:
        blockers.append(
            "В общих правилах или правилах этой темы найден прямой запрет рекламы/самопродвижения, "
            "а текст содержит призыв к покупке или заказу."
        )
    return list(dict.fromkeys(warnings)), list(dict.fromkeys(blockers))


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
        topic_rules = _rules(page["chunks"], page["text"])
        score, matches = _score(post_text, f"{label}\n{page['title']}\n{description}\n{topic_rules}")
        risks, blockers = _vc_rule_assessment(post, rules_text, topic_rules)
        if not rules_text:
            risks.append("Не удалось прочитать общие правила vc.ru. Автоматическая рекомендация заблокирована.")
        return {
            "target_type": "topic",
            "name": label,
            "slug": path.lstrip("/"),
            "title": page["title"] or label,
            "url": url,
            "description": description[:1000],
            "rules": topic_rules[:2200],
            "topic_rules_available": bool(topic_rules),
            "global_rules_available": bool(rules_text),
            "score": max(0, min(100, round(score * .82 + initial * .18))),
            "matches": matches,
            "risks": list(dict.fromkeys(risks)),
            "blockers": blockers,
            "rules_available": bool(rules_text),
            "recommended": bool(rules_text) and bool(topic_rules) and not blockers and score >= MIN_SCORE,
            "editorial_brief": _target_editorial_brief("vc_ru", post, {
                "name": label, "score": max(0, min(100, round(score * .82 + initial * .18))),
                "matches": matches, "rules": topic_rules, "risks": risks, "blockers": blockers,
                "recommended": bool(rules_text) and bool(topic_rules) and not blockers and score >= MIN_SCORE,
            }),
        }

    candidates = [
        item for item in await asyncio.gather(*(
            read_topic(initial, path, label)
            for initial, path, label in first
        ))
        if item is not None
    ]
    candidates.sort(
        key=lambda x: (x["recommended"], x["score"], x["name"]),
        reverse=True,
    )
    best = next((item for item in candidates if item["recommended"]), None)
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
    from tgfarm.community_policy import gate_plan

    platform_id = str(platform_id or "").strip().lower()
    if platform_id not in {"vc_ru", "pikabu"}:
        raise CommunityDraftError("Подбор места доступен только для vc.ru и Пикабу.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,40}", str(post_public_id or "")):
        raise CommunityDraftError("Некорректный идентификатор поста.")
    post = await asyncio.to_thread(_load_post, post_public_id)
    if not post.get("title") or not post.get("body"):
        raise CommunityDraftError("У поста нет заголовка или текста для тематического подбора.")
    plan = await (_vc(post) if platform_id == "vc_ru" else _pikabu(post))
    # One auditable contract for every destination: the panel and the publish
    # gate read the same verdict instead of re-deriving freshness themselves.
    plan["policy_verdict"] = gate_plan(platform_id, plan)
    return plan


def _valid_pikabu_community_tags(values: list[str]) -> list[str]:
    selected: list[str] = []
    seen: set[str] = set()
    for value in values:
        tag = re.sub(r"\s+", " ", str(value or "").strip().lstrip("#")).strip()
        key = tag.casefold()
        if not tag or key in seen or len(tag) > 50:
            continue
        if key in {"моё", "мое", "18+", "длиннопост", "текст", "видео", "гифка"}:
            continue
        seen.add(key)
        selected.append(tag)
    total = sum(len(tag) for tag in selected) + max(0, len(selected) - 1)
    return selected[:8] if len(selected) >= 2 and total <= 250 else []


async def recommend_automatic_pikabu_target(
    post_public_id: str,
    *,
    post_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Choose one live-rule-checked community for unattended native publishing."""
    if post_override is None:
        result = await recommend_community_target("pikabu", post_public_id)
    else:
        from tgfarm.community_drafts import CommunityDraftError
        post = dict(post_override)
        post["public_id"] = str(post_public_id)
        if not post.get("title") or not post.get("body"):
            raise CommunityDraftError("Для подбора сообщества Пикабу нужен заголовок и текст.")
        result = await _pikabu(post)
    for candidate in result.get("candidates", []):
        if not isinstance(candidate, dict) or not candidate.get("recommended"):
            continue
        if candidate.get("blockers") or not candidate.get("rules_available"):
            continue
        tags = _valid_pikabu_community_tags(candidate.get("suggested_tags") or [])
        if tags:
            return {
                "ok": True,
                "platform": "pikabu",
                "selected": candidate,
                "tags": tags,
                # Provenance travels with the selection so the publish gate can
                # rebuild the same CommunityPolicy contract the selector used.
                "rules_source": result.get("rules_source") or "",
                "auto_publish_supported": bool(result.get("auto_publish_supported")),
            }
    return {
        "ok": False, "platform": "pikabu", "selected": None, "tags": [],
        "reason": (
            "Не найдено сообщества с прочитанными правилами и минимум двумя существующими "
            "тематическими тегами. Публикация пропущена; конвейер продолжает другие площадки."
        ),
        "candidates": result.get("candidates", []),
    }
