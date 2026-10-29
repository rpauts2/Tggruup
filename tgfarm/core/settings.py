"""Application settings. Every field has a working default so the farm boots
with an empty .env; capabilities are probed, not assumed."""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# A frozen build unpacks Python under `_internal/`, so paths derived from
# `__file__` would land in a directory the user cannot write to and cannot see.
# In a packaged app the executable's own folder is the root: config ships next
# to it, data is created next to it, and `.env` is read from there.
FROZEN = getattr(sys, "frozen", False)
ROOT = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent.parent.parent
PACKAGE_DIR = ROOT / "tgfarm"
DATA_DIR = ROOT / "data"
MEDIA_DIR = DATA_DIR / "media"

_MARKER = "personas.yaml"


def _config_candidates() -> list[Path]:
    """Where a config may live, most specific first."""
    if not FROZEN:
        return [ROOT / "config"]

    bundle = getattr(sys, "_MEIPASS", None)
    return [
        ROOT / "config",  # an operator-edited copy beside the .exe
        Path(bundle) / "config" if bundle else None,  # the shipped default
        ROOT / "config",
    ]


def _resolve_config_dir() -> Path:
    candidates = [c for c in _config_candidates() if c is not None]
    for candidate in candidates:
        if (candidate / _MARKER).exists():
            return candidate
    return candidates[0]


CONFIG_DIR = _resolve_config_dir()


def config_dir() -> Path:
    """The config directory, resolved at call time.

    In a packaged app the editable copy beside the executable always wins, and it
    is created on first run — otherwise the operator has no file to edit and no
    way to tell that config is being read from inside `_internal/`.
    """
    if FROZEN:
        return ensure_config_template() or _resolve_config_dir()
    return CONFIG_DIR


def ensure_config_template() -> Path | None:
    """Copy the bundled config next to the executable on first run.

    A packaged app that silently starts with zero channels looks broken, and the
    operator has no way to know which files are missing. Seeding the editable
    copy means `config/personas.yaml` is always there to look at.
    """
    if not FROZEN:
        return None
    target = ROOT / "config"
    if (target / _MARKER).exists():
        return target

    bundle = getattr(sys, "_MEIPASS", None)
    source = Path(bundle) / "config" if bundle else None
    if source is None or not (source / _MARKER).exists():
        return None

    import shutil

    target.mkdir(parents=True, exist_ok=True)
    for item in source.glob("*.yaml"):
        shutil.copy2(item, target / item.name)
    (target / "README.txt").write_text(
        "Эти файлы — копия настроек, поставляемых с TgFarm.\n"
        "Правьте их здесь: изменения применяются при следующем запуске.\n",
        encoding="utf-8",
    )
    globals()["CONFIG_DIR"] = target
    return target


Mode = Literal["live", "dry", "off"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="",
        env_file=ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- runtime ------------------------------------------------------------
    mode: Mode = Field(default="dry", alias="TGFARM_MODE")
    database_url: str = Field(
        default="sqlite:///./tgfarm.db", alias="DATABASE_URL"
    )
    tz: str = Field(default="Europe/Moscow", alias="TZ")

    # --- admin API ----------------------------------------------------------
    # Localhost by default: the API can force-publish, so it must not be
    # reachable from the network unless the operator says so. `api_token` pins
    # the secret in .env instead of the generated file, which is what a packaged
    # desktop build does when it starts its own server.
    api_host: str = Field(default="127.0.0.1", alias="TGFARM_API_HOST")
    api_port: int = Field(default=8000, alias="TGFARM_API_PORT")
    api_token: str = Field(default="", alias="TGFARM_API_TOKEN")
    # Extra Host header values the dashboard answers on. The loopback names and
    # api_host are always allowed; this is for LAN names when the operator
    # binds 0.0.0.0 on purpose. The check blocks DNS-rebinding, where a public
    # site resolves to 127.0.0.1 and the browser forwards its cookies.
    allowed_hosts: str = Field(default="", alias="TGFARM_ALLOWED_HOSTS")
    require_api_token: bool = Field(default=True, alias="TGFARM_REQUIRE_TOKEN")
    # Dzen demotes fully automated feeds to "subscribers only" and its rules ban
    # auto-generated content outright. This flag parks quality-passed posts in
    # `gated` until a human presses "Опубликовать" in the panel: the farm keeps
    # producing, the channel keeps a human in the loop.
    approval_required: bool = Field(default=False, alias="TGFARM_APPROVAL_REQUIRED")

    # --- telegram -----------------------------------------------------------
    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_channels: str = Field(default="", alias="TELEGRAM_CHANNELS")

    # --- Telegram user session (read only) ----------------------------------
    # api_id / api_hash come from https://my.telegram.org → API development
    # tools. The resulting session file *is* the account: see `tgfarm.tguser`
    # for why this stays read-only while publishing keeps using the bot.
    telegram_api_id: str = Field(default="", alias="TELEGRAM_API_ID")
    telegram_api_hash: str = Field(default="", alias="TELEGRAM_API_HASH")
    telegram_phone: str = Field(default="", alias="TELEGRAM_PHONE")
    telegram_user_readonly: bool = Field(default=True, alias="TELEGRAM_USER_READONLY")
    telegram_top_groups_enabled: bool = Field(
        default=False, alias="TELEGRAM_TOP_GROUPS_ENABLED"
    )
    telegram_top_groups_limit: int = Field(default=10, alias="TELEGRAM_TOP_GROUPS_LIMIT")
    telegram_top_group_min_participants: int = Field(
        default=1000, alias="TELEGRAM_TOP_GROUP_MIN_PARTICIPANTS"
    )
    telegram_top_groups_interval_min: int = Field(
        default=180, alias="TELEGRAM_TOP_GROUPS_INTERVAL_MIN"
    )

    # --- llm ----------------------------------------------------------------
    # OpenCode Zen comes first so a single key covers the whole farm. Without a key
    # it is skipped without a request (Zen answers 403 unauthenticated), and the
    # chain falls through to the next provider, with `offline` last as the
    # guaranteed fallback.
    llm_providers: str = Field(
        default="opencode,anthropic,openai,ollama,offline", alias="LLM_PROVIDERS"
    )
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field(
        default="claude-sonnet-4-5", alias="ANTHROPIC_MODEL"
    )
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4.1-mini", alias="OPENAI_MODEL")

    # Any OpenAI-compatible endpoint: Gemini, Groq, OpenRouter, Together,
    # Fireworks, a local vLLM. Enabled by putting `compatible` in LLM_PROVIDERS.
    llm_base_url: str = Field(default="", alias="LLM_BASE_URL")
    llm_api_key: str = Field(default="", alias="LLM_API_KEY")
    llm_model: str = Field(default="", alias="LLM_MODEL")
    # Some gateways are reachable only through a local tunnel (OpenRouter denies
    # whole regions, for instance). Empty means a direct connection; anything
    # like http://127.0.0.1:7890 is handed to httpx as the proxy.
    llm_proxy: str = Field(default="", alias="LLM_PROXY")
    # Second echelon: an independent OpenAI-compatible gateway. It only gets
    # traffic when every model of the first chain is unavailable, which is
    # exactly what "fallback" means for a farm on free tiers.
    llm2_base_url: str = Field(default="", alias="LLM2_BASE_URL")
    llm2_api_key: str = Field(default="", alias="LLM2_API_KEY")
    llm2_model: str = Field(default="", alias="LLM2_MODEL")
    # Optional critic role: when set, the S5 tone/relevance/quality critics and
    # the rewriter use this model instead of the generation chain. A cheap model
    # can draft while a stronger one judges; empty means "same chain".
    llm_critic_model: str = Field(default="", alias="LLM_CRITIC_MODEL")

    # --- LongCat (OpenAI-compatible) ----------------------------------------
    # The platform currently exposes only paid LongCat models, despite its
    # "free" trial reputation. Keep the key separate from the generic
    # compatible endpoint so switching text providers stays explicit.
    longcat_api_key: str = Field(default="", alias="LONGCAT_API_KEY")
    longcat_model: str = Field(default="LongCat-2.5-Preview", alias="LONGCAT_MODEL")
    longcat_thinking: bool = Field(default=False, alias="LONGCAT_THINKING")

    # --- OpenCode Zen (OpenCode Console inference) ---------------------------
    # One key covers the whole farm. The key is mandatory: paid models answer 401
    # without it and the `-free` tier answers 403, being reserved for requests
    # made from inside OpenCode itself.
    opencode_zen_token: str = Field(default="", alias="OPENCODE_ZEN_TOKEN")
    opencode_zen_model: str = Field(
        default="claude-sonnet-4-6", alias="OPENCODE_ZEN_MODEL"
    )
    opencode_zen_free_only: bool = Field(
        default=False, alias="OPENCODE_ZEN_FREE_ONLY"
    )

    # --- image generation ---------------------------------------------------
    # `openai` speaks POST {base_url}/images/generations. `cloudflare` calls
    # Workers AI directly, which is not OpenAI-shaped and needs an account ID.
    image_provider: str = Field(default="openai", alias="IMAGE_PROVIDER")
    image_base_url: str = Field(default="", alias="IMAGE_BASE_URL")
    image_api_key: str = Field(default="", alias="IMAGE_API_KEY")
    image_model: str = Field(default="", alias="IMAGE_MODEL")
    image_size: str = Field(default="1280x720", alias="IMAGE_SIZE")
    image_enabled: bool = Field(default=False, alias="IMAGE_ENABLED")
    image_quality: str = Field(default="standard", alias="IMAGE_QUALITY")
    image_timeout_s: float = Field(default=90.0, alias="IMAGE_TIMEOUT_S")

    # Cloudflare Workers AI. The free plan includes 10k Neurons/day, enough for
    # roughly 150-170 FLUX.1 [schnell] covers at 1280x720 and four steps.
    cloudflare_account_id: str = Field(default="", alias="CLOUDFLARE_ACCOUNT_ID")
    cloudflare_api_token: str = Field(default="", alias="CLOUDFLARE_API_TOKEN")
    cloudflare_image_model: str = Field(
        default="@cf/black-forest-labs/flux-1-schnell",
        alias="CLOUDFLARE_IMAGE_MODEL",
    )
    cloudflare_image_steps: int = Field(default=4, alias="CLOUDFLARE_IMAGE_STEPS")

    # --- article lead photos ------------------------------------------------
    # Dzen articles carry their illustration inline, so a publisher's lead photo
    # is downloaded and pasted into the Draft.js body. The template cover remains
    # the fallback when no usable photo exists.
    article_image_enabled: bool = Field(default=True, alias="ARTICLE_IMAGE_ENABLED")
    article_image_max_mb: int = Field(default=12, alias="ARTICLE_IMAGE_MAX_MB")
    article_image_max_width: int = Field(default=1600, alias="ARTICLE_IMAGE_MAX_WIDTH")
    article_image_timeout_s: float = Field(default=20.0, alias="ARTICLE_IMAGE_TIMEOUT_S")

    # --- video -------------------------------------------------------------
    video_enabled: bool = Field(default=False, alias="VIDEO_ENABLED")
    video_max_mb: int = Field(default=45, alias="VIDEO_MAX_MB")
    video_max_duration_s: int = Field(default=180, alias="VIDEO_MAX_DURATION_S")
    video_download_dir: str = Field(default="", alias="VIDEO_DOWNLOAD_DIR")
    # Off by default: it disables the licensing gate in S11. Documented as the
    # legal risk it is; see the module docstring for what it stops checking.
    video_allow_unknown: bool = Field(default=False, alias="VIDEO_ALLOW_UNKNOWN")
    ollama_url: str = Field(default="http://localhost:11434", alias="OLLAMA_URL")
    ollama_model: str = Field(default="llama3.1", alias="OLLAMA_MODEL")

    # --- embeddings ---------------------------------------------------------
    embedding_provider: str = Field(default="local", alias="EMBEDDING_PROVIDER")
    embedding_dim: int = Field(default=512, alias="EMBEDDING_DIM")
    openai_embedding_model: str = Field(
        default="text-embedding-3-small", alias="OPENAI_EMBEDDING_MODEL"
    )

    # --- crossposting -------------------------------------------------------
    bluesky_handle: str = Field(default="", alias="BLUESKY_HANDLE")
    bluesky_app_password: str = Field(default="", alias="BLUESKY_APP_PASSWORD")
    # Mastodon OAuth user token; client credentials are registered per instance.
    mastodon_instance: str = Field(default="", alias="MASTODON_INSTANCE")
    mastodon_client_id: str = Field(default="", alias="MASTODON_CLIENT_ID")
    mastodon_client_secret: str = Field(default="", alias="MASTODON_CLIENT_SECRET")
    mastodon_access_token: str = Field(default="", alias="MASTODON_ACCESS_TOKEN")
    vk_access_token: str = Field(default="", alias="VK_ACCESS_TOKEN")
    vk_group_id: str = Field(default="", alias="VK_GROUP_ID")
    max_access_token: str = Field(default="", alias="MAX_ACCESS_TOKEN")
    max_channel_id: str = Field(default="", alias="MAX_CHANNEL_ID")
    # Dzen has no public publishing API. Publishing goes through its web editor
    # in a dedicated browser profile, logged in by the operator once.
    dzen_profile_dir: str = Field(default="", alias="DZEN_PROFILE_DIR")
    dzen_browser: str = Field(default="chrome", alias="DZEN_BROWSER")
    dzen_headless: bool = Field(default=False, alias="DZEN_HEADLESS")
    dzen_publish_timeout_s: int = Field(default=600, alias="DZEN_PUBLISH_TIMEOUT_S")
    dzen_login_timeout_s: int = Field(default=600, alias="DZEN_LOGIN_TIMEOUT_S")
    dzen_human_delay_ms: int = Field(default=70, alias="DZEN_HUMAN_DELAY_MS")
    # Dzen's editor is a browser session, not an API: publishing faster than one
    # post per this many seconds is the fastest way to get the profile flagged.
    dzen_min_gap_s: int = Field(default=600, alias="DZEN_MIN_GAP_S")
    # Dzen demotes template feeds: when every LLM is down, its posts must wait
    # in the queue instead of shipping deterministic composer text.
    dzen_requires_llm: bool = Field(default=True, alias="DZEN_REQUIRES_LLM")
    # When Dzen shows the "I'm not a robot" box, the headed window stays open
    # and the farm waits this long for the operator to click it.
    dzen_captcha_wait_s: int = Field(default=180, alias="DZEN_CAPTCHA_WAIT_S")
    # Numeric publisher id from dzen.ru/profile/editor/id/<id>/... . Optional:
    # when empty the collector discovers it from the authenticated main page.
    dzen_publisher_id: str = Field(default="", alias="DZEN_PUBLISHER_ID")
    dzen_stats_enabled: bool = Field(default=True, alias="DZEN_STATS_ENABLED")
    dzen_stats_interval_min: int = Field(default=30, alias="DZEN_STATS_INTERVAL_MIN")
    dzen_stats_timeout_s: int = Field(default=60, alias="DZEN_STATS_TIMEOUT_S")
    x_bearer_token: str = Field(default="", alias="X_BEARER_TOKEN")
    x_user_id: str = Field(default="", alias="X_USER_ID")
    x_oauth_client_id: str = Field(default="", alias="X_OAUTH_CLIENT_ID")
    x_oauth_client_secret: str = Field(default="", alias="X_OAUTH_CLIENT_SECRET")
    x_access_token: str = Field(default="", alias="X_ACCESS_TOKEN")
    x_refresh_token: str = Field(default="", alias="X_REFRESH_TOKEN")
    x_token_expires_at: int = Field(default=0, alias="X_TOKEN_EXPIRES_AT")
    # Reddit uses OAuth refresh tokens. Account passwords must never be stored.
    reddit_client_id: str = Field(default="", alias="REDDIT_CLIENT_ID")
    reddit_client_secret: str = Field(default="", alias="REDDIT_CLIENT_SECRET")
    reddit_refresh_token: str = Field(default="", alias="REDDIT_REFRESH_TOKEN")
    reddit_subreddit: str = Field(default="", alias="REDDIT_SUBREDDIT")
    reddit_user_agent: str = Field(default="", alias="REDDIT_USER_AGENT")
    # Threads user OAuth, refreshed locally before expiry.
    threads_app_id: str = Field(default="", alias="THREADS_APP_ID")
    threads_app_secret: str = Field(default="", alias="THREADS_APP_SECRET")
    threads_access_token: str = Field(default="", alias="THREADS_ACCESS_TOKEN")
    threads_token_expires_at: int = Field(default=0, alias="THREADS_TOKEN_EXPIRES_AT")
    threads_user_id: str = Field(default="", alias="THREADS_USER_ID")
    threads_username: str = Field(default="", alias="THREADS_USERNAME")
    # LinkedIn member OAuth and post publishing.
    linkedin_client_id: str = Field(default="", alias="LINKEDIN_CLIENT_ID")
    linkedin_client_secret: str = Field(default="", alias="LINKEDIN_CLIENT_SECRET")
    linkedin_access_token: str = Field(default="", alias="LINKEDIN_ACCESS_TOKEN")
    linkedin_refresh_token: str = Field(default="", alias="LINKEDIN_REFRESH_TOKEN")
    linkedin_token_expires_at: int = Field(default=0, alias="LINKEDIN_TOKEN_EXPIRES_AT")
    linkedin_refresh_token_expires_at: int = Field(default=0, alias="LINKEDIN_REFRESH_TOKEN_EXPIRES_AT")
    linkedin_member_id: str = Field(default="", alias="LINKEDIN_MEMBER_ID")
    linkedin_member_name: str = Field(default="", alias="LINKEDIN_MEMBER_NAME")

    # --- verification -------------------------------------------------------
    # Enables the S5 external verification gate, and the `websearch` source
    # collector when a source of that kind is configured.
    tavily_api_key: str = Field(default="", alias="TAVILY_API_KEY")
    serper_api_key: str = Field(default="", alias="SERPER_API_KEY")
    web_search_max_results: int = Field(default=8, alias="WEB_SEARCH_MAX_RESULTS")

    # --- AI policy ----------------------------------------------------------
    # `selective` spends online calls only where the deterministic composer is
    # weak (analysis/history, multi-source stories, short drafts). `always`
    # restores the old behaviour; `off` is a hard local-only mode.
    ai_policy: Literal["off", "selective", "always"] = Field(
        default="selective", alias="AI_POLICY"
    )
    ai_daily_budget: int = Field(default=30, alias="AI_DAILY_BUDGET")

    # AI thresholds for S5 gates
    ai_relevance_threshold: float = Field(default=7.0, alias="AI_RELEVANCE_THRESHOLD")
    ai_quality_threshold: float = Field(default=7.5, alias="AI_QUALITY_THRESHOLD")

    # --- pipeline -----------------------------------------------------------
    heat_threshold: float = Field(default=0.68, alias="HEAT_THRESHOLD")
    cluster_similarity: float = Field(default=0.82, alias="CLUSTER_SIMILARITY")
    duplicate_similarity: float = Field(default=0.88, alias="DUPLICATE_SIMILARITY")
    sameness_threshold: float = Field(default=0.87, alias="SAMENESS_THRESHOLD")
    collect_interval_min: int = Field(default=10, alias="COLLECT_INTERVAL_MIN")
    max_posts_per_channel_day: int = Field(
        default=1000, alias="MAX_POSTS_PER_CHANNEL_DAY"
    )
    source_adaptive_cadence: bool = Field(
        default=True, alias="SOURCE_ADAPTIVE_CADENCE"
    )
    source_idle_backoff_max: int = Field(default=6, alias="SOURCE_IDLE_BACKOFF_MAX")
    source_failure_backoff_max: int = Field(
        default=4, alias="SOURCE_FAILURE_BACKOFF_MAX"
    )
    source_auto_cooldown_after_failures: int = Field(
        default=5, alias="SOURCE_AUTO_COOLDOWN_AFTER_FAILURES"
    )
    source_auto_cooldown_hours: int = Field(
        default=24, alias="SOURCE_AUTO_COOLDOWN_HOURS"
    )

    # --- always-on supervision ---------------------------------------------
    cycle_timeout_min: int = Field(default=60, alias="CYCLE_TIMEOUT_MIN")
    # News drafts with known publication dates are rejected when every attached source is older than this.
    source_max_age_hours: int = Field(default=72, alias="SOURCE_MAX_AGE_HOURS")
    # Operational alerts: the farm must say out loud when it stops shipping.
    alert_no_publish_hours: int = Field(default=3, alias="ALERT_NO_PUBLISH_HOURS")
    # Warn when scheduled publication or editorial review queues grow beyond these limits.
    queue_backlog_alert_threshold: int = Field(default=100, alias="QUEUE_BACKLOG_ALERT_THRESHOLD")
    editorial_backlog_alert_threshold: int = Field(default=50, alias="EDITORIAL_BACKLOG_ALERT_THRESHOLD")
    # Where alerts go. Empty chat id = alerts stay in logs and /api/status.
    alert_chat_id: str = Field(default="", alias="ALERT_CHAT_ID")
    alert_cooldown_min: int = Field(default=30, alias="ALERT_COOLDOWN_MIN")
    # Share of posts generated without the few-shot examples, as a control
    # group: it is the only honest way to know the examples help.
    holdout_share: float = Field(default=0.1, alias="HOLDOUT_SHARE")
    # VK experiment: a share of posts snaps to the feed's prime hours instead
    # of pure interval spacing; variants let the farm compare honestly.
    vk_prime_hours: str = Field(default="", alias="VK_PRIME_HOURS")
    vk_prime_share: float = Field(default=0.3, alias="VK_PRIME_SHARE")
    # Silence is ambiguous: a daily "everything is fine" heartbeat removes it.
    alert_heartbeat_hours: int = Field(default=24, alias="ALERT_HEARTBEAT_HOURS")
    # Daily automatic backup destination; empty means ROOT/backup (same as the
    # Makefile). Old files beyond keep_backups are pruned.
    backup_dir: str = Field(default="", alias="BACKUP_DIR")
    # Optional mirror on a separate disk or mounted network share. Empty disables it.
    backup_mirror_dir: str = Field(default="", alias="BACKUP_MIRROR_DIR")
    keep_backups: int = Field(default=8, alias="KEEP_BACKUPS")
    # Warn when no successful verified snapshot exists within this window.
    backup_max_age_hours: int = Field(default=36, alias="BACKUP_MAX_AGE_HOURS")
    # Cluster and data hygiene: forming clusters age out, old raw rows leave.
    stale_cluster_hours: int = Field(default=72, alias="STALE_CLUSTER_HOURS")
    retention_raw_days: int = Field(default=90, alias="RETENTION_RAW_DAYS")
    retention_runs_days: int = Field(default=30, alias="RETENTION_RUNS_DAYS")
    cycle_stale_min: int = Field(default=45, alias="CYCLE_STALE_MIN")
    watchdog_interval_min: int = Field(default=5, alias="WATCHDOG_INTERVAL_MIN")
    # Alert if scheduled read-only auth checks stop running for this long.
    platform_auth_max_age_hours: int = Field(default=12, alias="PLATFORM_AUTH_MAX_AGE_HOURS")
    watchdog_recovery_base_min: int = Field(
        default=5, alias="WATCHDOG_RECOVERY_BASE_MIN"
    )
    watchdog_recovery_max_min: int = Field(
        default=60, alias="WATCHDOG_RECOVERY_MAX_MIN"
    )

    llm_timeout_s: float = 60.0
    llm_max_retries: int = 3
    http_timeout_s: float = 25.0
    user_agent: str = Field(
        default=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36 tgfarm/1.0"
        ),
        alias="USER_AGENT",
    )
    font_path: str = Field(default="", alias="FONT_PATH")

    @field_validator("database_url")
    @classmethod
    def _abs_sqlite(cls, v: str) -> str:
        if v.startswith("sqlite:///") and not v.startswith("sqlite:////"):
            return f"sqlite:///{(ROOT / v[10:]).as_posix()}"
        return v

    # --- derived helpers ----------------------------------------------------
    @property
    def provider_chain(self) -> list[str]:
        return [p.strip().lower() for p in self.llm_providers.split(",") if p.strip()]

    @property
    def zen_configured(self) -> bool:
        return bool(self.opencode_zen_token) or self.opencode_zen_free_only

    @property
    def images_configured(self) -> bool:
        if not self.image_enabled:
            return False
        provider = self.image_provider.strip().lower()
        if provider == "cloudflare":
            # Account ID can be discovered from the Cloudflare API when only the
            # token is configured, so do not hard-require it here.
            return bool(
                self.cloudflare_api_token
                and self.cloudflare_image_model
            )
        if provider == "pollinations":
            # Pollinations.ai is keyless; templates remain the fallback.
            return True
        return bool(self.image_base_url and self.image_model)

    @property
    def channel_ids(self) -> list[int]:
        out: list[int] = []
        for chunk in self.telegram_channels.replace(" ", "").split(","):
            if not chunk:
                continue
            try:
                out.append(int(chunk))
            except ValueError:
                continue
        return out

    @property
    def live(self) -> bool:
        return self.mode == "live"

    @property
    def online(self) -> bool:
        """False in `off` mode — collectors and LLM skip all network calls."""
        return self.mode != "off"

    def can_publish(self) -> bool:
        return self.live and bool(self.telegram_bot_token) and bool(self.channel_ids)

    def media_root(self) -> Path:
        MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        return MEDIA_DIR

    def data_root(self) -> Path:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        return DATA_DIR

    def dzen_profile_path(self) -> Path:
        root = Path(self.dzen_profile_dir) if self.dzen_profile_dir else DATA_DIR / "dzen_profile"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def video_root(self) -> Path:
        root = Path(self.video_download_dir) if self.video_download_dir else DATA_DIR / "video"
        root.mkdir(parents=True, exist_ok=True)
        return root


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
