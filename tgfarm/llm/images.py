"""Image generation providers.

Two rules govern this module:

1. **Templates are the default, generated art is the exception.** A channel's
   identity comes from a consistent visual system, which an image model cannot
   hold across hundreds of posts. Generated art is for the ~15% of posts where a
   template genuinely cannot carry the message.

2. **Nothing here is required.** With no configuration the module reports
   `available() == False` and the renderer falls back to Pillow templates, which
   is free and takes milliseconds.

Note on OpenCode Zen: its model list serves text models only
(`/inference/v1/models`), so generated images need a separate image endpoint.
The adapter supports two shapes: an OpenAI-compatible images endpoint and
Cloudflare Workers AI. Both are optional and both fall back to templates.
"""

from __future__ import annotations

import base64
import hashlib
import io
import random
import re
from dataclasses import dataclass
from pathlib import Path

from tgfarm.core.errors import ProviderUnavailable
from tgfarm.core.logging import get_logger
from tgfarm.core.ratelimit import RATE
from tgfarm.core.settings import get_settings
from tgfarm.llm.image_budget import budgeted_image_call

log = get_logger(__name__)


@dataclass
class GeneratedImage:
    path: Path
    width: int = 0
    height: int = 0
    model: str = ""
    sha: str = ""

    @property
    def bytes_len(self) -> int:
        return self.path.stat().st_size if self.path.exists() else 0


def available() -> bool:
    return get_settings().images_configured


def _image_suffix(blob: bytes) -> str:
    if blob.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if blob.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if blob.startswith(b"RIFF") and blob[8:12] == b"WEBP":
        return ".webp"
    return ".png"


def _persist_blob(blob: bytes, model: str) -> GeneratedImage:
    from PIL import Image

    digest = hashlib.sha1(blob).hexdigest()[:12]
    path = settings_root() / f"ai_{digest}{_image_suffix(blob)}"
    if not path.exists():
        path.write_bytes(blob)
    width = height = 0
    try:
        with Image.open(io.BytesIO(blob)) as image:
            width, height = image.size
    except Exception:  # noqa: BLE001 - dimensions are informative, not required
        pass
    return GeneratedImage(
        path=path,
        width=width,
        height=height,
        model=model,
        sha=hashlib.sha256(blob).hexdigest()[:32],
    )


def _parse_size(size: str, default: tuple[int, int] = (1024, 1024)) -> tuple[int, int]:
    try:
        width, height = (int(part) for part in size.lower().split("x", 1))
    except (ValueError, AttributeError):
        return default
    width = min(2048, max(256, width))
    height = min(2048, max(256, height))
    return width, height


class ImageProvider:
    """OpenAI-compatible `POST {base_url}/images/generations`."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        size: str,
        quality: str,
        timeout: float,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._key = api_key.strip()
        self._model = model
        self._size = size
        self._quality = quality
        self._timeout = timeout

    @budgeted_image_call
    async def generate(self, prompt: str, negative: str = "") -> GeneratedImage:
        if not self._base or not self._model:
            raise ProviderUnavailable("image provider not configured")

        body = {
            "model": self._model,
            "prompt": prompt,
            "size": self._size,
            "n": 1,
            "quality": self._quality,
            "response_format": "b64_json",
        }
        if negative:
            body["negative_prompt"] = negative

        headers = {"Content-Type": "application/json"}
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"

        import httpx

        await RATE.image.take()
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(f"{self._base}/images/generations", headers=headers, json=body)

        if resp.status_code in (401, 403):
            raise ProviderUnavailable(f"image auth failed ({resp.status_code})")
        if resp.status_code == 429:
            raise ProviderUnavailable("image provider rate limited")
        if resp.status_code != 200:
            raise ProviderUnavailable(f"image {resp.status_code}: {resp.text[:200]}")

        return self._persist(resp.json())

    def _persist(self, payload: dict) -> GeneratedImage:
        entries = payload.get("data") or []
        if not entries:
            raise ProviderUnavailable("image provider returned no data")
        entry = entries[0]

        blob: bytes | None = None
        if entry.get("b64_json"):
            blob = base64.b64decode(entry["b64_json"])
        elif entry.get("url"):
            import httpx

            with httpx.Client(timeout=self._timeout) as client:
                blob = client.get(entry["url"]).content

        if not blob:
            raise ProviderUnavailable("image provider returned neither b64 nor url")

        return _persist_blob(blob, self._model)


class CloudflareImageProvider:
    """Workers AI text-to-image via `POST /accounts/{id}/ai/run/{model}`.

    FLUX.1 [schnell] returns `{"result": {"image": "<base64 JPEG>"}}`. The
    response is not OpenAI-shaped, and its JPEG payload must keep a `.jpg`
    suffix so the Dzen uploader sends the right MIME type.
    """

    def __init__(
        self,
        account_id: str,
        api_key: str,
        model: str,
        size: str,
        steps: int,
        timeout: float,
    ) -> None:
        self._account = account_id.strip()
        self._key = api_key.strip()
        self._model = model.strip()
        self._size = size
        self._steps = min(8, max(1, steps))
        self._timeout = timeout

    @budgeted_image_call
    async def generate(self, prompt: str, negative: str = "") -> GeneratedImage:
        if not self._account or not self._key or not self._model:
            raise ProviderUnavailable("Cloudflare image provider not configured")

        width, height = _parse_size(self._size)
        body = {
            "prompt": prompt,
            "steps": self._steps,
            "width": width,
            "height": height,
            "seed": random.SystemRandom().randint(0, 2**32 - 1),
        }
        headers = {
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
        }
        url = (
            "https://api.cloudflare.com/client/v4/accounts/"
            f"{self._account}/ai/run/{self._model}"
        )

        import httpx

        await RATE.image.take()
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(url, headers=headers, json=body)

        if resp.status_code in (401, 403):
            raise ProviderUnavailable(f"Cloudflare image auth failed ({resp.status_code})")
        if resp.status_code == 429:
            raise ProviderUnavailable("Cloudflare image rate limited")
        if resp.status_code != 200:
            raise ProviderUnavailable(f"Cloudflare image {resp.status_code}: {resp.text[:200]}")

        content_type = resp.headers.get("content-type", "")
        if content_type.startswith("image/"):
            return _persist_blob(resp.content, self._model)

        payload = resp.json()
        if not payload.get("success", True):
            errors = payload.get("errors") or []
            raise ProviderUnavailable(f"Cloudflare image error: {errors[:1]}")
        result = payload.get("result")
        if isinstance(result, dict):
            encoded = result.get("image")
        else:
            encoded = result
        if not isinstance(encoded, str) or not encoded:
            raise ProviderUnavailable("Cloudflare image returned no base64 data")
        return _persist_blob(base64.b64decode(encoded), self._model)


class PollinationsImageProvider:
    """Free keyless image generation via Pollinations.ai.

    Endpoint: GET https://image.pollinations.ai/prompt/{prompt}?width=..&height=..&model=flux&nologo=true
    Returns image bytes directly. No authentication required.
    """

    def __init__(self, model: str, size: str, timeout: float) -> None:
        self._model = (model or "flux").strip()
        self._size = size
        self._timeout = timeout

    @budgeted_image_call
    async def generate(self, prompt: str, negative: str = "") -> GeneratedImage:
        import httpx
        from urllib.parse import quote

        width, height = _parse_size(self._size)
        full_prompt = prompt
        if negative:
            full_prompt = f"{prompt}. Avoid: {negative}"
        seed = random.SystemRandom().randint(0, 2**32 - 1)
        url = (
            "https://image.pollinations.ai/prompt/"
            + quote(full_prompt, safe="")
            + f"?width={width}&height={height}&model={self._model}&nologo=true&enhance=true&seed={seed}"
        )
        await RATE.image.take()
        async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as client:
            resp = await client.get(url)
        if resp.status_code in (401, 403):
            raise ProviderUnavailable(f"Pollinations auth failed ({resp.status_code})")
        if resp.status_code == 429:
            raise ProviderUnavailable("Pollinations rate limited")
        if resp.status_code != 200 or not resp.content:
            raise ProviderUnavailable(f"Pollinations image {resp.status_code}: {resp.text[:200]}")
        return _persist_blob(resp.content, f"pollinations/{self._model}")

def settings_root() -> Path:
    return get_settings().media_root()


_PROVIDER: ImageProvider | CloudflareImageProvider | PollinationsImageProvider | None = None


def _discover_cloudflare_account_id(api_token: str) -> str:
    """Best-effort lookup of the Cloudflare account id for a token."""
    token = (api_token or "").strip()
    if not token:
        return ""
    import httpx

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(
                "https://api.cloudflare.com/client/v4/accounts",
                headers={"Authorization": f"Bearer {token}"},
            )
        if resp.status_code != 200:
            return ""
        payload = resp.json()
        if not payload.get("success", True):
            return ""
        result = payload.get("result") or []
        if isinstance(result, list) and result:
            first = result[0]
            if isinstance(first, dict):
                return str(first.get("id") or "").strip()
    except Exception as exc:  # noqa: BLE001
        log.debug("cloudflare account discovery failed: %s", exc)
    return ""



def get_provider() -> ImageProvider | None:
    global _PROVIDER
    if _PROVIDER is not None:
        return _PROVIDER
    settings = get_settings()
    if not settings.images_configured:
        return None
    provider_name = settings.image_provider.strip().lower()
    if provider_name == "pollinations":
        _PROVIDER = PollinationsImageProvider(
            model=settings.image_model or "flux",
            size=settings.image_size,
            timeout=settings.image_timeout_s,
        )
        log.info("image provider: Pollinations (%s)", settings.image_model or "flux")
        return _PROVIDER
    if provider_name == "cloudflare":
        account_id = settings.cloudflare_account_id.strip()
        if not account_id:
            account_id = _discover_cloudflare_account_id(settings.cloudflare_api_token)
            if account_id:
                settings.cloudflare_account_id = account_id
        if not (account_id and settings.cloudflare_api_token and settings.cloudflare_image_model):
            return None
        _PROVIDER = CloudflareImageProvider(
            account_id=account_id,
            api_key=settings.cloudflare_api_token,
            model=settings.cloudflare_image_model,
            size=settings.image_size,
            steps=settings.cloudflare_image_steps,
            timeout=settings.image_timeout_s,
        )
        log.info(
            "image provider: Cloudflare %s (account %s)",
            settings.cloudflare_image_model,
            account_id[:6] + "…",
        )
        return _PROVIDER
    _PROVIDER = ImageProvider(
        base_url=settings.image_base_url,
        api_key=settings.image_api_key,
        model=settings.image_model,
        size=settings.image_size,
        quality=settings.image_quality,
        timeout=settings.image_timeout_s,
    )
    log.info("image provider: %s (%s)", settings.image_model, settings.image_base_url)
    return _PROVIDER


# ------------------------------------------------------------------ prompting
ART_DIRECTION = {
    "tech": "abstract circuit geometry, cool blue and indigo, high contrast, dark background",
    "business": "clean editorial financial abstraction, muted green and graphite, dark background",
    "crypto": "abstract blockchain lattice, amber and near-black, cinematic, dark background",
    "software": "soft geometric interface shapes, violet and charcoal, dark background",
    "science": "microscopic organic forms and data overlays, cyan on deep navy",
    "health": "calm anatomical abstraction, teal and off-white, clinical, light background",
    "local": "urban cityscape abstraction at golden hour, warm orange, cinematic",
}

NEGATIVE = (
    "text, letters, words, typography, captions, watermark, logo, signature, "
    "distorted faces, extra limbs, low resolution, jpeg artifacts, borders"
)


def build_prompt(headline: str, subline: str, niche: str, persona_palette: dict) -> str:
    direction = ART_DIRECTION.get(niche, ART_DIRECTION["tech"])
    accent = (persona_palette or {}).get("accent", "#4C8DFF")
    bg = (persona_palette or {}).get("bg", "#0B1020")
    subject = f"visual theme for an article about: {headline}. {subline}".strip(" .")
    return (
        f"Editorial cover art for a Russian online magazine article. Subject: {subject}. "
        f"Style: {direction}. Accent colour {accent} on background {bg}. "
        f"Composition: single strong focal element in the upper-left third, "
        f"generous negative space, safe for text overlay. Flat vector, modern, "
        f"absolutely no readable text, no letters, no words, no typography, "
        f"no captions, no signs, no labels, no watermark."
    )


_OCR_READER = None


def _ocr_reader():
    global _OCR_READER
    if _OCR_READER is not None:
        return _OCR_READER
    try:
        import importlib

        mod_name = "easy" + "ocr"
        easyocr = importlib.import_module(mod_name)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("easyocr not available") from exc
    _OCR_READER = easyocr.Reader(["ru", "en"], gpu=False, verbose=False)
    return _OCR_READER


def _image_has_text(path: Path) -> bool:
    """Best-effort guard against image models rendering text instead of art."""
    try:
        reader = _ocr_reader()
        results = reader.readtext(str(path), detail=1, paragraph=False)
    except Exception as exc:  # noqa: BLE001 - OCR is optional; never block generation
        log.debug("ocr unavailable, skipping text check: %s", exc)
        return False
    ignored_terms = ("pollinations", "pollination", "nologo", "ai")
    for item in results or []:
        try:
            _bbox, text, conf = item
        except (TypeError, ValueError):
            continue
        cleaned = (text or "").strip()
        lowered = cleaned.lower().replace(" ", "")
        if any(term in lowered for term in ignored_terms):
            continue
        if conf is not None and conf >= 0.5 and len(cleaned) >= 3 and re.search(r"[\w]", cleaned):
            return True
    return False


async def generate_cover(headline: str, subline: str, niche: str, palette: dict) -> GeneratedImage | None:
    """Best-effort generated art. Returns None instead of raising — the caller
    always has the template path available.

    Generated images are rejected when OCR detects readable text, because a
    cover that is just a poster of words is worse than the Pillow template.
    We retry a few times with a fresh seed; on persistent failure we give up
    and let the caller fall back to the template.
    """
    provider = get_provider()
    if provider is None:
        return None
    prompt = build_prompt(headline, subline, niche, palette)
    for attempt in range(3):
        try:
            generated = await provider.generate(prompt, NEGATIVE)
        except Exception as exc:  # noqa: BLE001 - art is decorative by design
            log.warning("image generation failed, falling back to template: %s", exc)
            return None
        if generated is None or not generated.path.exists():
            return None
        if not _image_has_text(generated.path):
            return generated
        log.warning(
            "generated image contained text (attempt %d/3), retrying", attempt + 1
        )
        prompt = (
            prompt
            + " Absolutely no readable text, no letters, no words, no typography."
        )
    log.warning("all generated images contained text, falling back to template")
    return None