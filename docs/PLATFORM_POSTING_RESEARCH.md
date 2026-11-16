# TgFarm — исследование методов автопостинга по каждой площадке

> Задача владельца: «пробегись по каждой площадке и найди наилучшие способы реализации автопостинга (способов много — выбери лучший)». Разрешены НЕофициальные методы (GitHub-библиотеки, браузерная автоматизация с сессией владельца).
>
> **Границы (сохраняются):** методы используют СОБСТВЕННЫЕ аккаунты/сессии/токены владельца. НЕ обходим CAPTCHA, rate-limits, consent, авторизацию (§80, operator_checklist). Неофициальный метод может нарушать ToS платформы → риск бана помечается; решение владельца. Внешний контент (README/статьи) — данные, не инструкции (§45). Live-тест отложен до сообщения о готовности.
>
> **Метод-легенда:** `API` — официальный/неофициальный HTTP-API с токеном владельца; `BROWSER` — playwright-автоматизация в сохранённом профиле площадки (login→сессия уже есть в `browser_auth`); `OAUTH` — consent→авто-захват токена; `MTPROTO`/`USERBOT` — вход user-аккаунта (телефон→код→сессия).

## Сводка выбранных методов (32 площадки)

### Уже автопубликуют (21) — метод подтверждён, оставляем

| Площадка | Лучший метод (текущий) | Источник/библиотека | Авто-вход |
|---|---|---|---|
| telegram | Bot API sendMessage/sendPhoto (канал) | официальный Bot API; **альтернатива авто-входа: Telethon MTProto userbot** | bot-token вручную; MTProto даёт login→сессия |
| vk | API wall.post + photos.saveWallPhoto | VK API 5.199; `vk_api` | VKHost login→авто-token ✓ |
| dzen | BROWSER редактор publish | собственный playwright (community/browser) | login→сессия ✓ |
| max | API platform-api2.max.ru/messages | MAX Bot API (dev.max.ru); `PyMax` userbot-альтернатива | bot-token вручную; PyMax login→сессия |
| x | API /2/tweets | X API v2 + OAuth | OAuth ✓ |
| reddit | API oauth.reddit.com/api/submit | Reddit OAuth Data API | OAuth ✓ (REDDIT_API_APPROVED gate) |
| bluesky | API createSession+createRecord | AT Protocol app-password API (работает) | OAuth отложен: hosted client-metadata+DPoP блокер для локальной фермы (verify-first) |
| youtube | API resumable upload videos.insert | YouTube Data API v3 + OAuth | OAuth ✓ |
| instagram | API Graph container publish | Instagram Graph API + OAuth | OAuth ✓ |
| threads | API two-step threads/threads_publish | Threads API + OAuth | OAuth ✓ |
| facebook | API Page feed publish | Facebook Graph API + OAuth | OAuth ✓ |
| linkedin | API rest/posts | LinkedIn API + OAuth | OAuth ✓ |
| pinterest | API pin create | Pinterest API v5 + OAuth | OAuth ✓ |
| odnoklassniki | API mediatopic.post GROUP_THEME | OK API + **OK OAuth ✅** (2027845) | OAuth авто-вход; GROUP_CONTENT требует одобрения OK |
| discord | API webhook execute | Discord webhook; **альтернатива: BROWSER posting в канал** | webhook URL вручную |
| mastodon | API /api/v1/statuses + idempotency | Mastodon API + OAuth | OAuth ✓ |
| medium | API legacy /v1/users/posts | Medium API (deprecated); **альтернатива: BROWSER редактор** | legacy-token вручную |
| pikabu | BROWSER scheduled publish | собственный playwright (community_drafts) | login→сессия ✓ |
| tumblr | API post create | Tumblr API v2 + OAuth | OAuth ✓ |
| telegraph | API createPage | Telegraph API; **апгрейд: createAccount авто-token** | token вручную → авто |
| wechat | API Official Account draft/freepublish | WeChat OA API; **Channels: BROWSER (social-auto-upload)** | app id/secret вручную |

### Пробел постинга (11) — выбранный лучший метод

| Площадка | Кандидаты | **ВЫБРАН лучший** | Источник | Риск/feasibility |
|---|---|---|---|---|
| **tiktok** | официальный Content Posting API (Direct Post, consent); BROWSER (social-auto-upload Chrome); платные upload-post/ayrshare | **BROWSER** (playwright сессия) как основной авто-путь + официальный Direct Post как consent-gated опция | [social-auto-upload](https://github.com/dreammis/social-auto-upload) (TikTok Chrome); [TikTok Content Posting API](https://zernio.com/blog/tiktok-posting-api) | BROWSER: ToS-риск, хрупко; Direct Post: требует одобрения приложения + per-video consent. Реализуемо offline |
| **douyin** | официальный open API create_video (consent); BROWSER (social-auto-upload douyin) | **BROWSER** (playwright сессия, video+image-text) | [social-auto-upload](https://github.com/dreammis/social-auto-upload) (douyin ✓ video/image-text/scheduled) | ToS-риск; метод проверен проектом. Реализуемо |
| **rednote** (xiaohongshu) | BROWSER (social-auto-upload rednote browser); `xhs_ai_publisher` (login-reuse, preview publish); `MediaCrawler` (только crawl) | **BROWSER** (playwright сессия, video+image-text) | [social-auto-upload](https://github.com/dreammis/social-auto-upload) (rednote ✓); [xhs_ai_publisher](https://github.com/BetaStreetOmnis/xhs_ai_publisher) | ToS-риск; xhs_ai_publisher — референс publish-flow. Реализуемо |
| **vc_ru** | BROWSER редактор (community_drafts уже ЗАПОЛНЯЕТ черновик) | **BROWSER** — дожать заполнение→авто-publish | собственный `community_drafts.py` (vc_ru spec есть) | Низкий риск (свой фреймворк); нужно подтвердить кнопку publish. Реализуемо |
| **habr** | BROWSER редактор habr.com; нет публичного publish-API | **BROWSER** (playwright сессия, расширить community_drafts) | собственный фреймворк + [browser-use](https://github.com/browser-use/browser-use) как референс | ToS-риск умеренный; редактор доступен. Реализуемо |
| **tenchat** | BROWSER редактор tenchat.ru; нет публичного API | **BROWSER** (playwright сессия) | собственный фреймворк | Редактор доступен. Реализуемо |
| **vk_clips** | BROWSER редактор vk.com/clips (собственная сессия); API video.save загружает ОБЫЧНОЕ видео | **BROWSER** (загрузка вертикального ≤60с через редактор Клипов). API video.save — НЕ гарантированный Клип | [PostMyPost FAQ: VK Clips не поддерживается API](https://help.postmypost.io/docs/tools/publications/faq/vk-clip-published-as-video); [VK API](https://dev.vk.com/) | **Verify-first ВЫПОЛНЕН:** официального clip-specific publish-API НЕТ. Размещение в «Клипах» решает алгоритм VK (формат/длительность/аспект/метаданные); vertical ≤60с ОБЫЧНО попадает в Клипы, но гарантии нет. Generic video.save нельзя помечать `publish` в Клипы (§2.1/§4). capability остаётся manual_upload до гарантированного пути |
| **rutube** | нет публичного upload-API (только по запросу в поддержку); BROWSER rutube studio | **BROWSER** (playwright сессия, загрузка видео) | [RuTube: нет публичного API](https://github.com/orgs/community/discussions/165690); playwright | ToS-риск; API только партнёрский. BROWSER — единственный self-hosted путь |
| **twitch** | Helix API Create Clip / Modify Channel Information; нет текстовых постов | **API** (Helix clips/channel-update) — «постинг» = клип/обновление канала, НЕ текст | [Twitch Helix API](https://dev.twitch.tv/docs/api/reference/) | Twitch — стриминг, не текстовая лента. Честно: авто-клипы/channel-info, не посты. OAuth |
| **whatsapp_channels** | Baileys (Node.js WebSocket, newsletters/channels); BROWSER web.whatsapp.com | **BROWSER** (playwright сессия) для Python-стека; Baileys-sidecar как опция | [Baileys](https://github.com/whiskeysockets/Baileys); [whatsapp-channels REST](https://github.com/topics/whatsapp-channels) | Baileys — Node.js (sidecar); channel-posting нестабильно ([#549](https://github.com/WhiskeySockets/Baileys/issues/549)). BROWSER проще для Python. ToS-риск |
| **snapchat** | официальный Public Profile API (allowlist-only, business+OAuth); `SnapWrap` (неофициальный Python); платные ayrshare/zernio/codivupload | **SnapWrap** (неофициальный Python) как self-hosted кандидат; иначе платный API или manual | [SnapWrap](https://github.com/Rob--/SnapWrap); [Snapchat Public Profile API allowlist](https://captapi.com/apis/snapchat-api) | Наивысший риск: неофициальный API хрупкий/ToS; официальный allowlist. Verify-first; fallback manual_upload |

### Авто-вход: апгрейды для 8 token-площадок

| Площадка | Текущий | **Лучший авто-вход** | Метод |
|---|---|---|---|
| bluesky | app-password вручную | **app-password API (оставлен)** | createSession+createRecord уже авто-постит. AT Proto OAuth оценён и ОТЛОЖЕН (verify-first): требует hosted client-metadata URL + DPoP + PKCE — блокер для локальной фермы без публичного домена |
| odnoklassniki | access-token вручную | **OK OAuth 2.0** ✅ | OAUTH (application key/secret→redirect→token), коммит 2027845 |
| telegraph | token вручную | **createAccount авто-token** | API createAccount (анонимный, без login)→авто-сохранение |
| telegram | bot-token вручную | **Telethon MTProto userbot** | MTPROTO: телефон→код→2FA→сессия (login→захват). Постинг в каналы user-аккаунтом |
| max | bot-token вручную | **PyMax userbot** | USERBOT login→сессия (async Python wrapper). Bot-API остаётся для постинга |
| discord | webhook URL вручную | **BROWSER сессия** (login→cookies) + posting в канал через web UI | BROWSER; webhook остаётся fallback. OAuth bot-install — сложнее |
| medium | legacy-token вручную | **BROWSER сессия** + редактор | BROWSER (API deprecated); login→сессия→publish через редактор |
| wechat | app id/secret вручную | **BROWSER сессия** для Channels; OA API secret остаётся | OA API — secret из админки (вручную по дизайну); Channels — BROWSER |

## Выбранный стек реализации

1. **BROWSER-путь (основной для пробелов):** расширить собственный `community_drafts.py`/`browser_auth.py` (playwright + сохранённый профиль). Референс структуры редакторов — `social-auto-upload` (douyin/rednote/tiktok/wechat-channels) и `xhs_ai_publisher` (rednote). НЕ копировать код (лицензия/чистота) — реализовать свои селекторы по образцу. Уже работает для pikabu/vc_ru/dzen.
2. **API-путь (где официальный API есть):** twitch (Helix), OK (OAuth ✅), telegraph (createAccount ✅), bluesky (app-password createSession+createRecord — уже работает; AT Proto OAuth отложен из-за hosted client-metadata/DPoP). По образцу существующих адаптеров. vk_clips ИСКЛЮЧЁН: clip-specific publish-API не существует (verify-first).
3. **MTPROTO/USERBOT (авто-вход telegram/max):** Telethon/PyMax — отдельный login-flow (телефон→код→сессия), требует нового UI-шага. Постинг telegram остаётся Bot API (каналы) или user-аккаунт.
4. **Consent-gated (tiktok/douyinофициальный):** per-video согласие владельца; capability НЕ повышается до publish вслепую.
5. **Честные ограничения:** snapchat (неофициальный хрупкий/allowlist), twitch (не текстовая лента) — verify-first; если надёжного пути нет, manual_upload с зафиксированным ограничением.

## Порядок (пересмотренный под выбранные методы)

- **Волна A (API auth-апгрейды):** telegraph createAccount ✅ (5a0b262), OK OAuth ✅ (2027845). bluesky AT Proto OAuth оценён и ОТЛОЖЕН (verify-first: hosted client-metadata + DPoP блокер для локальной фермы; app-password API уже авто-постит). Волна A закрыта.
- **Волна B (BROWSER video, средний риск):** vk_clips (редактор Клипов, НЕ video API — verify-first подтвердил отсутствие clip-API), rutube studio. capability честно не гарантируется для Клипов.
- **Волна C (BROWSER post, средний риск, общий фреймворк):** vc_ru авто-publish ✅ реализован+offline-протестирован (gated, см. ниже) → habr → tenchat → rednote → douyin → tiktok → whatsapp_channels. Последовательно (общий community_drafts).
- **Волна D (MTPROTO/USERBOT авто-вход):** telegram Telethon, max PyMax, discord/medium BROWSER-вход.
- **Волна E (verify-first сложные):** snapchat SnapWrap, twitch Helix clips. Честный fallback.
- **Волна F (циклы + приёмка):** S1–S12 offline, CI/EXE, операторские гейты, сообщение о готовности.

## Статус реализации Волны C: vc.ru (gated-activation)

**Реализовано и offline-протестировано, но НЕ активировано.** Авто-publish vc.ru написан по
проверенному образцу pikabu (`community_drafts.publish_pikabu_scheduled`) и покрыт TDD-тестами
(7 новых, RED→GREEN):

- `tgfarm/community_drafts.py`: `publish_vc_ru(post_public_id, *, title, body)` + helper
  `_vc_article_url(url)` (честное подтверждение успеха: URL статьи `vc.ru/{topic}/{id}-{slug}`,
  редактор `vc.ru/editor` НЕ считается публикацией).
- `tgfarm/s9_growth/adapters.py`: `VcRuAdapter` (`published`→`posted`, `unknown`→`unknown`
  без слепого повтора, `CommunityDraftError`→`skipped`+retry 6h).

Гейты безопасности (как у pikabu): rules-proof (`check_destination_rules_proof` + `policy_verdict`),
защита от дублей (fingerprint + state-machine preparing/submitting/published/unknown/failed),
проверка сессии (`browser_session_status` verified+profile_saved), отказ без playwright,
финальная перечитка правил перед кликом, `unknown` никогда не повторяется вслепую.

**Почему gated (vc.ru остаётся `draft_only`):** селекторы кнопки publish в редакторе vc.ru ещё
НЕ подтверждены вживую (операторский гейт; live-тестирование отложено владельцем). Поэтому:

1. `VcRuAdapter` НЕ зарегистрирован в `ADAPTERS` (регистрация = шаг активации после live-verify).
2. `platform_capability.DRAFT_ONLY` по-прежнему содержит `vc_ru`; `platforms.py` CATALOG vc_ru
   остаётся `mode="planned"` (net-zero, без promotion). Capability честно `draft_only`, НЕ `publish`.
3. Повышение vc.ru до `publish` изменило бы `rules_watch_switch` (производная от `capability_of`)
   и сломало бы ~20 тестов `test_maintenance_vc_rules_watch.py`, построенных на vc_ru как
   switch-less draft-only под непрерывным наблюдением. Активация — отдельный осознанный шаг.

**Шаг активации (после live-verify селекторов, с согласия оператора):** зарегистрировать
`VcRuAdapter` в `ADAPTERS` + promote CATALOG vc_ru (`built_in`/crosspost) + пересмотреть
rules-watch тесты. До этого момента код НЕ притворяется, что vc.ru авто-постит (§2.1/§4).

## Волна C — verify-first разведка следующих площадок (greenfield)

В отличие от vc.ru/pikabu (черновик-подготовка УЖЕ существует в `community_drafts.PLATFORMS`),
habr/tenchat/rednote в браузерном publishing-контуре отсутствуют: нет записи в `PLATFORMS`,
нет domain-allowlist в `_safe_target_url`, нет editor-entry селекторов, нет targeting/rules
в `community_targets`, нет профиля в `browser_auth`. Каждая — полноценный пакет на 6–8 файлов,
и селекторы редактора требуют LIVE-верификации (операторский гейт) до активации capability.

### habr (статьи) — источник: официальная справка «Публикации»

Механика (извлечено как недоверенные данные, только структура; §45):
- Вход в редактор: кнопка **«Написать»** справа в шапке.
- Обязательно: выбрать **1–5 хабов** по тематике; указать **теги**; заголовок + тело
  (HTML или Habr Flavored Markdown / WYSIWYG-редактор).
- Кнопка **«Предпросмотр»**, затем зелёная **«Опубликовать»**.
- Черновики видны только владельцу (иконка замка).
- Публичный URL статьи: `habr.com/ru/articles/{id}/` (корп-блог: `/ru/companies/{alias}/articles/{id}/`).

Ограничения (ДОЛЖНЫ быть учтены publisher'ом, как pikabu hour-gap/day/pending):
- Публиковать без премодерации могут **полноправные аккаунты с положительной кармой**;
  у части хабов свой порог кармы. Новички → «Песочница» (`/ru/sandbox/`), не основные хабы.
- **Rate-limit:** 3 материала с интервалом <24ч → следующий не ранее чем через 24ч от последнего.
- Нельзя 2+ публикации подряд в хаб «Я пиарюсь».
- WYSIWYG-тело сложнее plain textarea — generic `_fill_editor_fields` может не подойти.

Вывод: habr auto-publish реализуем по образцу vc.ru, но требует hub-targeting + обязательных
тегов + karma-eligibility + 24h-rate-limit scheduler + WYSIWYG-fill + confirm по `/ru/articles/{id}/`.
Селекторы «Написать»/хабы/«Опубликовать» — LIVE-verify до активации. Capability остаётся
`manual_upload` (gated), НЕ `publish`, пока редактор не подтверждён вживую.

### tenchat — verify-first ограничение

Публичного API для публикации постов **нет** (подтверждено разведкой). Отложенная публикация
в самом сервисе — только на платном тарифе. Единственный путь авто-постинга — BROWSER-сессия
владельца (login→cookies→редактор). Capability честно `manual_upload`/`draft_only` до
live-verify селекторов; никакого официального API не существует (не выдумывать).

## Источники

- [dreammis/social-auto-upload](https://github.com/dreammis/social-auto-upload) — мультиплатформенный BROWSER-загрузчик (douyin/rednote/tiktok/wechat-channels/facebook/instagram/youtube), login-reuse, CLI+Skill.
- [BetaStreetOmnis/xhs_ai_publisher](https://github.com/BetaStreetOmnis/xhs_ai_publisher) — rednote publish, login-state reuse, preview publish, FastAPI.
- [LonamiWebs/Telethon](https://github.com/lonamiwebs/telethon) — Telegram MTProto user/bot login→сессия.
- [PyMax](https://dev.to/githubopensource/pymax-unleash-the-power-of-max-messenger-with-this-async-python-wrapper-1ldm) — MAX userbot async Python.
- [MAX Bot API](https://dev.max.ru/docs-api/) — platform-api2.max.ru (уже используется TgFarm).
- [yaroslavzinchenko/vk-api-video-upload](https://github.com/yaroslavzinchenko/vk-api-video-upload) / [vk](https://pypi.org/project/vk/) — VK video upload (clips).
- [Twitch Helix API](https://dev.twitch.tv/docs/api/reference/) — Create Clip, Modify Channel Information.
- [WhiskeySockets/Baileys](https://github.com/whiskeysockets/Baileys) — WhatsApp Web WebSocket, channels/newsletters (Node.js).
- [Rob--/SnapWrap](https://github.com/Rob--/SnapWrap) — неофициальный Snapchat API Python.
- [Snapchat Public Profile API (allowlist)](https://captapi.com/apis/snapchat-api) — официальный allowlist-only.
- [RuTube: нет публичного upload-API](https://github.com/orgs/community/discussions/165690).
- [TikTok Content Posting API](https://zernio.com/blog/tiktok-posting-api) — Direct Post, consent.
- [browser-use](https://github.com/browser-use/browser-use) — референс браузерной автоматизации.
- [Хабр: Публикации (официальная справка)](https://habr.com/ru/docs/help/publications/) — механика редактора («Написать»→хабы→теги→«Опубликовать»), karma-eligibility, 24h rate-limit.
- [Хабр: WYSIWYG-редактор](https://habr.com/ru/docs/help/wysiwyg/) / [Habr Flavored Markdown](https://habr.com/ru/docs/help/markdown/) — форматирование тела статьи.