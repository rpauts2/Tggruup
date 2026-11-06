"""Read-only target selection for Pikabu communities and vc.ru topics."""
from __future__ import annotations

import asyncio
import re
from datetime import datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

from tgfarm.community_policy import (
    DEFAULT_MAX_AGE_HOURS,
    FRESHNESS_FRESH,
    FRESHNESS_MISSING,
    FRESHNESS_STALE,
    FRESHNESS_UNREAD,
    assess_policy,
    inert_external_text,
    policy_from_candidate,
    rules_freshness,
)
from tgfarm.core.text import content_tokens, normalize

PIKABU_DIRECTORY = "https://pikabu.ru/communities/all"
PIKABU_RULES = "https://pikabu.ru/information/rules"
VC_DIRECTORY = "https://vc.ru/support"
VC_RULES = "https://vc.ru/rules"
USER_AGENT = "Mozilla/5.0 (compatible; TgFarmEditorial/1.0)"
MIN_SCORE = 28
MAX_PIKABU = 24
MAX_VC = 10

#: How much rule text one candidate keeps for the panel and the editorial context.
#: It is page content, so it is stored only in the inert form (see CommunityPolicy).
RULES_PAGE_LIMIT = 2200


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


async def revalidate_pikabu_target(
    post: dict[str, Any],
    target: dict[str, Any],
) -> dict[str, Any]:
    """Read the Code and the selected community again, right before publishing.

    The selector read the rules minutes ago; the native editor then takes a while
    (fill, upload, tags, schedule) before the save button exists. Rules that were
    fine at selection may have changed by then, and rules that cannot be read now
    are not a licence to publish anyway — every unreadable outcome here becomes a
    blocker rather than a silent pass.

    The verdict is recorded in the rules history like any other check, so the
    operator can see after the fact what the publisher saw at the last moment.
    """
    target_url = str(target.get("url") or "").strip()
    parsed = urlparse(target_url)
    match = re.fullmatch(r"/community/([A-Za-z0-9_-]+)", parsed.path.rstrip("/"))
    slug = str(target.get("slug") or "").strip()
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"pikabu.ru", "www.pikabu.ru"}
        or match is None
        or (slug and slug != match.group(1))
    ):
        raise ValueError("Selected Pikabu community URL is not a canonical community path.")

    expected_slug = match.group(1)
    global_page, community_page = await asyncio.gather(
        _fetch(PIKABU_RULES), _fetch(target_url), return_exceptions=True
    )
    if isinstance(global_page, BaseException):
        global_rules = ""
    else:
        global_rules = str(global_page.get("text") or "").strip()
    if isinstance(community_page, BaseException):
        community_rules, page_description = "", ""
    else:
        community_rules = _rules(
            list(community_page.get("chunks") or []),
            str(community_page.get("text") or ""),
        )
        page_description = str(community_page.get("description") or "")

    warnings, blockers = _pikabu_risks(community_rules, post, global_rules)
    if isinstance(global_page, BaseException):
        blockers.append(f"Общий Кодекс Пикабу не перечитался ({type(global_page).__name__}).")
    elif not global_rules:
        blockers.append("Не удалось повторно прочитать общий Кодекс Пикабу.")
    if isinstance(community_page, BaseException):
        blockers.append(f"Правила сообщества не перечитались ({type(community_page).__name__}).")
    elif not community_rules:
        blockers.append("Не удалось повторно прочитать правила выбранного сообщества.")

    try:
        score = max(0, min(100, int(target.get("score") or 0)))
    except (TypeError, ValueError):
        score = 0
    candidate = {
        "target_type": "community",
        "name": str(target.get("name") or target.get("title") or expected_slug),
        "slug": expected_slug,
        "url": target_url,
        "description": inert_external_text(page_description or target.get("description") or "", 900),
        "rules": inert_external_text(community_rules, RULES_PAGE_LIMIT),
        "global_rules": inert_external_text(global_rules, RULES_PAGE_LIMIT),
        # The Code and the community rules are judged as one policy: with the
        # Code unread there is no claim that the destination's rules are known.
        "rules_available": bool(community_rules and global_rules),
        "global_rules_available": bool(global_rules),
        "rules_source": PIKABU_RULES,
        "auto_publish_supported": True,
        "score": score,
        "matches": target.get("matches") or [],
        "suggested_tags": target.get("suggested_tags") or [],
        "editorial_brief": target.get("editorial_brief") or [],
        "risks": warnings,
        "blockers": list(dict.fromkeys(blockers)),
        "recommended": bool(target.get("recommended")) and score >= MIN_SCORE and not blockers,
    }

    from tgfarm.community_policy import assess_policy, policy_from_candidate, record_policy_snapshot

    policy = policy_from_candidate("pikabu", candidate, rules_source=PIKABU_RULES)
    assessment = assess_policy(policy)
    record_policy_snapshot(policy, assessment)
    return {
        "target": candidate,
        "community_policy": policy.to_dict(),
        "policy_assessment": assessment,
        "warnings": warnings,
        "blockers": list(dict.fromkeys(blockers)),
    }


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
            "description": inert_external_text(description, 900),
            "rules": inert_external_text(rule_text, RULES_PAGE_LIMIT),
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
            "description": inert_external_text(description, 1000),
            "rules": inert_external_text(topic_rules, RULES_PAGE_LIMIT),
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


#: The one shape a stored vc.ru topic id is allowed to have: the canonical section
#: path the selector wrote (`_vc` stores `path.lstrip("/")`, e.g. `finance`). The
#: rules watch rebuilds the page URL from this and from nothing else, so a stored
#: id can never turn into an arbitrary request (§45: the id is page-authored data).
VC_TOPIC_SLUG_RE = re.compile(r"[a-z0-9_-]{2,32}")


async def read_vc_topic_rules(destination: dict[str, Any]) -> dict[str, Any]:
    """Re-read one vc.ru topic's rules and the site-wide rules; needs no post.

    The destination-only twin of `read_topic` inside `_vc`. That one scores a
    *material* against a live directory and cannot answer "which rules does this
    topic publish under today", which is why the maintenance rules watch had no
    vc.ru reader at all. Everything else is reused verbatim: the same `_fetch`,
    the same `_rules` extractor, the same two pages the selector already opens
    (`VC_RULES` and the topic page), the same `inert_external_text(...,
    RULES_PAGE_LIMIT)` treatment and the same `policy_from_candidate("vc_ru", ...)`
    contract — so the `rules_hash` this writes is directly comparable with the one
    a selection wrote, and no new page or request shape is introduced.

    What it deliberately does not do:

    * fetch a URL the stored id *implies* rather than builds. A `target_id` that
      is not a canonical section path raises before any request is made;
    * retry, authorise, or knock twice (§47). One GET per page per destination: a
      refusal, a CAPTCHA wall or a 403 stays a refusal and becomes an unreadable
      destination, never a circumvented one;
    * write a snapshot it cannot stand behind. When the topic rules cannot be
      read this raises instead of recording an empty `rules_hash`, because the
      change detection reads an empty hash as «the rules changed» — a statement
      about the destination that was never measured;
    * publish, comment or schedule anything. `auto_publish_supported` stays False:
      on vc.ru a person presses the final button.

    A rules page is untrusted data (§45). Its text is bounded, control-flattened
    and credential-redacted on the way in, hashed, and quoted back only as data;
    nothing read from it can change a flag, a verdict status or a URL.
    """
    from tgfarm.community_drafts import CommunityDraftError
    from tgfarm.community_policy import assess_policy, policy_from_candidate, record_policy_snapshot

    slug = str(destination.get("target_id") or destination.get("slug") or "").strip().lstrip("/")
    if not VC_TOPIC_SLUG_RE.fullmatch(slug):
        raise ValueError("stored vc.ru destination is not a canonical topic path")
    topic_url = urljoin("https://vc.ru", f"/{slug}")

    rules_result, topic_result = await asyncio.gather(
        _fetch(VC_RULES), _fetch(topic_url), return_exceptions=True
    )
    if isinstance(topic_result, BaseException):
        raise CommunityDraftError(
            f"Правила темы vc.ru не перечитались ({type(topic_result).__name__}); снимок не записан."
        )
    topic_rules = _rules(
        list(topic_result.get("chunks") or []),
        str(topic_result.get("text") or ""),
    )
    if not topic_rules:
        raise CommunityDraftError(
            "Не удалось прочитать правила выбранной темы vc.ru; снимок не записан."
        )
    global_rules = (
        ""
        if isinstance(rules_result, BaseException)
        else str(rules_result.get("text") or "").strip()
    )

    warnings: list[str] = []
    blockers: list[str] = []
    if isinstance(rules_result, BaseException):
        blockers.append(
            f"Общие правила vc.ru не перечитались ({type(rules_result).__name__})."
        )
    elif not global_rules:
        blockers.append("Не удалось повторно прочитать общие правила vc.ru.")
    # The rules-only half of `_vc_risks`: with no post there is no material to
    # judge, only what the destination itself currently demands.
    published_rules = normalize(global_rules or "") + " " + normalize(topic_rules)
    if any(word in published_rules for word in ("коммерческ", "реклам", "ограничен")):
        warnings.append(
            "В текущих правилах есть отдельные ограничения для коммерческих/рекламных публикаций."
        )

    candidate = {
        "target_type": "topic",
        "name": str(destination.get("target_name") or destination.get("name") or slug),
        "slug": slug,
        "url": topic_url,
        "description": inert_external_text(topic_result.get("description") or "", 1000),
        "rules": inert_external_text(topic_rules, RULES_PAGE_LIMIT),
        # Same meaning the selector gives these keys: the topic text is in hand,
        # the site-wide page is a separate read that can have failed.
        "topic_rules_available": True,
        "global_rules_available": bool(global_rules),
        "rules_available": bool(global_rules),
        "rules_source": VC_RULES,
        "auto_publish_supported": False,
        "score": 0,
        "matches": [],
        "suggested_tags": [],
        "editorial_brief": [],
        "risks": warnings,
        "blockers": list(dict.fromkeys(blockers)),
    }
    policy = policy_from_candidate("vc_ru", candidate, rules_source=VC_RULES)
    assessment = assess_policy(policy)
    snapshot = record_policy_snapshot(policy, assessment)
    return {
        "target": candidate,
        "community_policy": policy.to_dict(),
        "policy_assessment": assessment,
        "snapshot": snapshot,
        "warnings": warnings,
        "blockers": list(dict.fromkeys(blockers)),
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
    # ``record=True`` puts the check in ``community_policy_snapshots``: this is the
    # only rules read an operator actually triggers, and an unrecorded check is an
    # unauditable one — the history card would otherwise always be empty.
    plan["policy_verdict"] = await asyncio.to_thread(gate_plan, platform_id, plan, record=True)
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


# --------------------------------------------------------------------------
# Destination selection with stored rules proof (W29-B)
# --------------------------------------------------------------------------
#
# Why this exists: `recommend_automatic_pikabu_target` chooses the first
# candidate whose *in-memory* flags say `recommended` / `rules_available` /
# "no blockers", and `publish_pikabu_scheduled` re-assesses a policy that
# :func:`tgfarm.community_policy.policy_from_candidate` has just stamped with
# ``checked_at = now`` — an in-memory re-assessment can therefore never be
# stale by construction, and neither path ever asks the
# ``community_policy_snapshots`` table what was actually proven and when.
# A rules verdict that was never stored cannot justify a publish, so this
# module refuses, by measurement, the three states the live farm allowed:
# no snapshot ever stored, a snapshot older than the freshness window, and a
# verdict that does not permit publishing.  Every refusal is returned in
# ``rejected`` with a reason code from the closed vocabulary below plus a
# short human reason; a refusal is never a silent skip.

#: Closed vocabulary of rejection reason codes.  A code outside this tuple
#: must never be invented from page text or from a stored row: the reason
#: describes *our* gate outcome, not the destination's opinion about itself.
REJECT_UNIDENTIFIABLE = "unidentifiable_destination"
REJECT_NO_SNAPSHOT = "no_rules_snapshot"
REJECT_SNAPSHOT_UNREAD = "rules_snapshot_unread"
REJECT_SNAPSHOT_STALE = "rules_snapshot_stale"
REJECT_VERDICT_DENIES = "verdict_denies_publish"
TARGET_REJECTION_CODES = (
    REJECT_UNIDENTIFIABLE,
    REJECT_NO_SNAPSHOT,
    REJECT_SNAPSHOT_UNREAD,
    REJECT_SNAPSHOT_STALE,
    REJECT_VERDICT_DENIES,
)


def _destination_proof_id(candidate: dict[str, Any]) -> str:
    """Stored destination id of one selector candidate.

    Same fallback chain :func:`tgfarm.community_policy.policy_from_candidate`
    uses for ``target_id`` (slug, then path, then name), so the key asked of
    the rules history is the key the snapshot writer wrote.
    """
    return str(
        candidate.get("slug") or candidate.get("path") or candidate.get("target_id") or ""
    ).strip()


def check_destination_rules_proof(
    platform: str,
    candidate: dict[str, Any] | None,
    *,
    rules_source: str = "",
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Ask the two questions a publish must answer about one destination.

    1. does the destination's *live* policy verdict allow publishing —
       :func:`tgfarm.community_policy.assess_policy` on the candidate's own
       structured fields; page text is data and never a permission;
    2. does the stored rules history prove that check —
       :func:`tgfarm.community_policy.rules_freshness` must answer ``fresh``
       *and* carry a stored ``verdict_status`` of ``ok``.

    Returns ``{"publishable": bool, "proof": freshness dict, "verdict": dict,
    "rejection": None or a rejected-list entry}``.  A broken or empty store is
    a refusal in the return value, never an exception the caller might
    mistake for a skip.
    """
    candidate = candidate if isinstance(candidate, dict) else {}
    target_id = _destination_proof_id(candidate)
    if not target_id:
        return {
            "publishable": False,
            "proof": {},
            "verdict": {},
            "rejection": {
                "destination_id": "",
                "platform": platform,
                "reason_code": REJECT_UNIDENTIFIABLE,
                "reason": (
                    "the candidate carries no slug/path/target_id, so no stored "
                    "rules check can even be looked up for it"
                ),
            },
        }

    policy = policy_from_candidate(platform, candidate, rules_source=rules_source)
    verdict = assess_policy(policy, max_age_hours=max_age_hours, now=now)
    if not verdict.get("allow_publish"):
        blockers = ", ".join(str(item) for item in (verdict.get("blockers") or ()))
        return {
            "publishable": False,
            "proof": {},
            "verdict": verdict,
            "rejection": {
                "destination_id": target_id,
                "platform": platform,
                "reason_code": REJECT_VERDICT_DENIES,
                "reason": (
                    "the destination's own rules verdict does not allow "
                    f"publishing: {verdict.get('reason') or blockers or 'no reason given'}"
                ),
                "blockers": list(verdict.get("blockers") or ()),
            },
        }

    proof = rules_freshness(platform, target_id, max_age_hours=max_age_hours, now=now)
    state = str(proof.get("state") or "")
    if state == FRESHNESS_MISSING:
        code = REJECT_NO_SNAPSHOT
        text = (
            "no rules snapshot has ever been stored for this destination; the "
            "check exists only in memory and cannot justify a publish"
        )
    elif state == FRESHNESS_STALE:
        code = REJECT_SNAPSHOT_STALE
        text = (
            "the stored rules snapshot is older than the freshness window: "
            f"{proof.get('reason') or 're-read the rules before publishing'}"
        )
    elif state == FRESHNESS_FRESH:
        stored_verdict = str(proof.get("verdict_status") or "")
        if stored_verdict == "ok":
            return {"publishable": True, "proof": proof, "verdict": verdict, "rejection": None}
        code = REJECT_VERDICT_DENIES
        text = (
            f"the stored snapshot's verdict was {stored_verdict or 'unknown'!r}; "
            "only a fresh snapshot whose verdict allowed publishing justifies a publish"
        )
    elif state == FRESHNESS_UNREAD:
        code = REJECT_SNAPSHOT_UNREAD
        text = (
            "a rules snapshot row exists but proves nothing: "
            + str(proof.get('reason') or 'unreadable rules history')
        )
    else:
        code = REJECT_SNAPSHOT_UNREAD
        text = (
            "the rules store answered with an unknown state "
            + repr(state or 'empty')
            + "; treated as unproven"
        )
    return {
        "publishable": False,
        "proof": proof,
        "verdict": verdict,
        "rejection": {
            "destination_id": target_id,
            "platform": platform,
            "reason_code": code,
            "reason": text,
        },
    }


def select_destination_with_rules_proof(
    platform: str,
    candidates: list[dict[str, Any]] | None,
    *,
    rules_source: str = "",
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Choose the first candidate that can prove its rules were checked.

    Input order is the selection priority (the live selectors already sort by
    ``recommended`` then score).  Returns ``{"selected": candidate | None,
    "rejected": [entry, ...], "proof": freshness-of-selected, "detail": str}``.
    Every candidate examined before the winner — or every candidate, when
    none wins — appears in ``rejected`` with ``destination_id``, a
    ``reason_code`` from :data:`TARGET_REJECTION_CODES` and a short human
    ``reason``.  An empty candidate list selects nothing and refuses nothing:
    there was no destination to evaluate, which is a different answer from
    refusing every one of them.
    """
    rejected: list[dict[str, Any]] = []
    selected: dict[str, Any] | None = None
    selected_proof: dict[str, Any] = {}
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        outcome = check_destination_rules_proof(
            platform,
            candidate,
            rules_source=rules_source,
            max_age_hours=max_age_hours,
            now=now,
        )
        if outcome["publishable"]:
            selected = candidate
            selected_proof = outcome["proof"]
            break
        rejected.append(outcome["rejection"])
    if selected is not None:
        detail = (
            f"selected {_destination_proof_id(selected)}: its rules snapshot is "
            "stored, fresh, and its verdict allowed publishing"
        )
    elif rejected:
        detail = (
            f"no destination passed the stored-rules gate "
            f"({len(rejected)} refused, each with its reason)"
        )
    else:
        detail = "no candidates were offered, so nothing was selected or refused"
    return {"selected": selected, "rejected": rejected, "proof": selected_proof, "detail": detail}
