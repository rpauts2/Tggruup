"""Text utilities: normalization, tokenisation, near-duplicate fingerprints.

Pure-python only, no downloads, deterministic — these run on every item in the
pipeline so they must be cheap.
"""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from collections import Counter

WORD_RE = re.compile(r"[a-zа-яё0-9]+", re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
URL_RE = re.compile(r"https?://\S+")

STOPWORDS: frozenset[str] = frozenset(
    """
    a an the and or but if then than that this these those of in on at to for from by with
    is are was were be been being do does did doing have has had having will would can could
    should may might must not no nor so as it its it's you your we our they their he she
    и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по только
    ее мне было вот от меня еще нет о из ему теперь когда даже ну вдруг ли если уже или ни
    быть был него до вас нибудь опять уж вам ведь там потом себя ничего ей может они тут где
    есть надо ней для мы тебя их чем была сам чтоб без будто чего раз тоже себе под будет ж
    тогда кто этот того потому этого какой совсем ним здесь этом один почти мой тем чтобы
    нее сейчас были куда зачем всех никогда можно при наконец два об другой хоть после над
    больше тот через эти нас про всего них какая много разве три эту моя впрочем хорошо
    свою этой перед иногда лучше чуть том нельзя такой им более всегда конечно всю между
    """.split()
)

# stripped headline noise that adds nothing to semantic distance
_BOILERPLATE_RE = re.compile(
    r"\b("
    r"источник|источники|read more|подробнее|читать далее|фото|фото:|video|видео|"
    r"telegram|телеграм|канал|подписывайтесь|подписывайся|жми|читай|полностью|"
    r"breaking|exclusive|opinion|analysis|watch|listen"
    r")\b",
    re.IGNORECASE,
)


def strip_html(text: str) -> str:
    """Remove tags *and* unescape entities.

    Feeds routinely emit `&#8216;Pure insanity&#8217;` in a <title>. Dropping tags
    without unescaping leaves the numeric entity sitting in the headline, and it
    ends up on the rendered cover.
    """
    return WS_RE.sub(" ", html.unescape(TAG_RE.sub(" ", text or ""))).strip()


def normalize(text: str) -> str:
    """Lowercase, de-accent-free, collapse whitespace. Stable across sources."""
    text = strip_html(text or "")
    text = unicodedata.normalize("NFKC", text)
    return WS_RE.sub(" ", text).strip().lower()


def tokens(text: str, drop_stopwords: bool = True) -> list[str]:
    toks = WORD_RE.findall(normalize(text))
    if drop_stopwords:
        toks = [t for t in toks if len(t) > 2 and t not in STOPWORDS]
    return toks


def content_tokens(text: str) -> list[str]:
    """Tokens with boilerplate removed — used for topic-level similarity."""
    return tokens(_BOILERPLATE_RE.sub(" ", strip_html(text or "")))


def char_ngrams(text: str, n: int = 4) -> list[str]:
    """Padded character n-grams. Robust to morphology and typos, which matters a
    lot for Russian headlines."""
    norm = " " + WS_RE.sub(" ", normalize(text)).strip() + " "
    if len(norm) < n:
        return [norm] if norm.strip() else []
    return [norm[i : i + n] for i in range(len(norm) - n + 1)]


def simhash(text: str, bits: int = 62) -> int:
    """Charikar simhash over char n-grams. Cheap near-duplicate detector.

    62 bits (not 64) so the value always fits a signed 64-bit column — SQLite
    INTEGER is signed, and an unsigned 64-bit fingerprint overflows on insert.

    Counted per distinct n-gram, not per occurrence. The fingerprint is a sign
    vote over the bits of each gram's hash, so `w` identical grams cast the same
    vote `w` times: weighting the unique grams by their multiplicity is the same
    number. That equivalence is what makes this linear in distinct grams instead
    of linear in characters times 62 bits — the old loop spent 62 Python
    iterations on every one of 77k grams (0.94 s for a 77 KB article), and the
    pipeline calls this on every collected item.
    """
    vector = [0] * bits
    weights = Counter(char_ngrams(text, 4))
    if not weights:
        return 0
    for gram, weight in weights.items():
        h = int.from_bytes(hashlib.blake2b(gram.encode(), digest_size=8).digest(), "big")
        for i in range(bits):
            vector[i] += weight if (h >> i) & 1 else -weight
    out = 0
    for i in range(bits):
        if vector[i] > 0:
            out |= 1 << i
    return out


def hamming(a: int, b: int, bits: int = 62) -> int:
    return bin((a ^ b) & ((1 << bits) - 1)).count("1")


# Calibrated on real headlines: two spellings of the same story land at 7-13
# bits apart, unrelated stories at 17-30. 12 is the midpoint with margin, and it
# is only a pre-filter — the embedding cosine check (>=0.95) makes the final call.
SIMHASH_MAX_DISTANCE = 12


def is_near_duplicate(a_fp: int, b_fp: int, max_distance: int = SIMHASH_MAX_DISTANCE) -> bool:
    return hamming(a_fp, b_fp) <= max_distance


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


def token_jaccard(a: str, b: str) -> float:
    return jaccard(set(content_tokens(a)), set(content_tokens(b)))


def keyword_set(text: str, limit: int = 12) -> set[str]:
    """Distinctive tokens, longest first — a stable topic fingerprint."""
    seen: set[str] = set()
    for tok in sorted(set(content_tokens(text)), key=lambda t: (-len(t), t)):
        if len(tok) >= 4:
            seen.add(tok)
        if len(seen) >= limit:
            break
    return seen


#: Tokens a cut must not split: a markdown link, a bare URL, a hashtag or a
#: mention. Cutting inside one publishes a dead target or a dangling bracket.
_PROTECTED_TOKEN = re.compile(
    r"\[([^\]\n]*)\]\(([^)\s]*)\)"  # [label](target)
    r"|https?://\S+"  # bare URL
    r"|(?<![\w#@])[#@][\w-]+",  # hashtag or mention
    re.IGNORECASE,
)


def truncate(text: str, limit: int, ellipsis: str = "…") -> str:
    """Cut text to `limit` characters without breaking a link or a markdown pair.

    Every platform path ends here (`distribution`, adapters, manual packages), so
    a cut that lands inside `https://…` ships a dead link, and a cut inside
    `[label](url)` ships a dangling bracket. A markdown link that straddles the
    cut keeps its label and loses its target; a bare URL that straddles it is
    dropped whole, because half a link is worse than none.
    """
    text = (text or "").strip()
    if limit <= 0:
        return ""
    dropped = False
    while len(text) > limit:
        room = max(0, limit - len(ellipsis))
        straddling = next(
            (
                match
                for match in _PROTECTED_TOKEN.finditer(text)
                if match.start() < room < match.end()
            ),
            None,
        )
        if straddling is None:
            break
        label = straddling.group(1)
        if label is None:
            # A bare URL or a tag: nothing readable survives, so stop before it.
            head = text[: straddling.start()]
            if head.strip():
                return _finish_cut(head, limit, ellipsis, force_ellipsis=True)
            # Nothing but the link: an empty post helps nobody, so keep a plain cut.
            break
        collapsed = text[: straddling.start()] + label + text[straddling.end() :]
        if len(collapsed) >= len(text):
            head = text[: straddling.start()]
            if head.strip():
                return _finish_cut(head, limit, ellipsis, force_ellipsis=True)
            break
        text = collapsed
        dropped = True
    return _finish_cut(text, limit, ellipsis, force_ellipsis=dropped)


def _finish_cut(text: str, limit: int, ellipsis: str, *, force_ellipsis: bool = False) -> str:
    """Trim to the limit on a word boundary and mark the omission."""
    if len(text) <= limit:
        return text + ellipsis if force_ellipsis and text else text
    room = max(0, limit - len(ellipsis))
    cut = text[:room]
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip(" ,.;:—-") + ellipsis


def first_sentence(text: str, limit: int = 120) -> str:
    cleaned = strip_html(text or "")
    match = re.search(r"(?<=[.!?…])\s+", cleaned)
    candidate = cleaned[: match.start()] if match else cleaned
    if not candidate.strip():
        candidate = cleaned
    return truncate(candidate, limit)


def title_key(title: str) -> str:
    """Order-independent fingerprint of a headline's content words."""
    toks = sorted(set(content_tokens(title)))
    return hashlib.blake2b(" ".join(toks).encode(), digest_size=16).hexdigest()


def detect_lang(text: str) -> str:
    """Cheap ru/en detector. Cyrillic ratio is a good enough discriminator for
    our source mix and needs zero dependencies."""
    cyr = sum(1 for ch in text if "а" <= ch.lower() <= "я" or ch.lower() == "ё")
    lat = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    total = cyr + lat
    if total == 0:
        return "unknown"
    return "ru" if cyr / total > 0.25 else "en"


def word_count(text: str) -> int:
    return len(WORD_RE.findall(text or ""))