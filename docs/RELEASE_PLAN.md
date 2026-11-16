# TgFarm — фулл-план до релиза (реализация ВСЕХ запланированных функций)

> **Для исполнителей:** REQUIRED SUB-SKILL: superpowers:executing-plans. Субагенты в этой сессии недоступны (типы Explore/Plan/general-purpose удалены из harness) — исполнение и review-проходы последовательные, координатором; независимое субагентное ревью не выполнялось, это явно помечается. Шаги — checkbox (`- [ ]`).

**Goal (определение владельца):** фулл-релиз = реализация всех изначально запланированных функций, три блока:
1. **Автоавторизация всех площадок** — кнопка «Войти» в приложении открывает страницу входа площадки в браузере; владелец входит; приложение само забирает нужное (OAuth-токен / сессию / cookies).
2. **Постинг всех площадок** — реализация publish для каждой площадки. **Live-тест НЕ проводится до отдельного сообщения владельцу о готовности.** Реализация разрешена; боевая публикация — нет.
3. **Все циклы программы** — S1 сбор → S2 отсеивание → … → S12 (полный автономный контур).

**Architecture:** одна ветка `main`, прямые коммиты; каждый блок: diff → TDD-тесты → no-secrets → commit → push `github` → сверка remote SHA → НЕЗАВИСИМОЕ чтение CI per-job. Реестр — `docs/EXECUTION_PLAN.md`, машинное состояние — `docs/orchestration/queue.yaml`, передача — `docs/CONTINUATION_STATE.md`, доказательства — `docs/READINESS_MATRIX.md`.

**Tech Stack:** Python 3.11 (target CI; локально 3.14), SQLAlchemy/SQLite, FastAPI, vanilla JS UI, pywebview/pystray, PyInstaller (CI Windows), pytest, playwright (браузерный вход + visual QA).

**Spec:** `docs/AUTONOMY_ACCEPTANCE.md`, `docs/READINESS_MATRIX.md`, `docs/EXECUTION_PLAN.md`, `.qoder/operator_checklist.md`.

## Global Constraints

- Push только в `github`; `origin` — stale trap. Без force, без переписывания истории (§51).
- Запрещены `git reset --hard`, `git clean`, безусловный `git checkout --`; неизвестная работа сохраняется, не удаляется.
- Все локальные прогоны: `NO_PROXY= no_proxy= .venv/Scripts/python.exe -m pytest …` (env-артефакт `[::1]` ломает httpx 0.28.1).
- Секреты не в Git/логах/UI (§44); токен в логе — только рекомендация ротации оператору (§9), ротацию НЕ выполнять.
- Никаких live-действий без отдельного согласия владельца: production-БД, LIVE mode, реальные публикации, ротация токенов, перезапуск serve, ввод OAuth-секретов (§0.3, §47, §80). **Постинг реализуется и проверяется offline/dry-run; live-тест отложен до сообщения о готовности.**
- Offline/CI ≠ live-приёмка. Неизвестно ≠ ноль. `publish_unknown` не повторять вслепую.
- TDD: красный тест до кода; assertions не ослаблять; xfail не прятать. FP-epsilon на верхней границе бюджета — гигиена сравнения, НЕ ослабление контракта.
- ruff: line-length 110, target py311, E/F/I/UP/B, ignore B008. CRLF working copies / LF blobs.
- Консоль cp1252: не печатать кириллицу из python-скриптов (писать в UTF-8 файл / PYTHONIOENCODING=utf-8).
- 18 symlink-тестов локально SKIP (WinError 1314) — выполняются только на CI Windows.
- Закрытие PR #60/#56 (общее состояние GitHub) — ТОЛЬКО с явным подтверждением владельца.
- **Честность capability:** «connected» ≠ «can publish». Площадка без поддерживаемого publish-API остаётся `manual_upload`/`draft_only` с зафиксированным ограничением; не выдавать пакет за автопубликацию (§2.1, §4 — код не притворяется).

## Фактическая база (замер 2026-11-16, HEAD `5126aaf`)

- local `main` = `github/main` = **`5126aaf8b8a3004656b7feb90483d97fe0c6d97d`** (push сверен).
- **CI run [#43828861262](https://github.com/rpauts2/Tggruup/actions/runs/43828861262) на `5126aaf`: Linux `test` SUCCESS + Windows SUCCESS** (независимо подтверждено per-job). FP-класс верхних границ бюджета закрыт целиком (`7b541e2`). Техническая база зелёная на обеих ОС.
- Локальный полный suite на `0f1a993`: `4298 passed, 33 skipped, 4 xfailed, 0 failed`. Visual QA #43788547309 success.
- Контур зрелый: **32 площадки в каталоге**, из них **21 уже автопубликуют** (реальные адаптеры), **24 уже имеют вход «браузер→захват»**. Циклы S1–S12 реализованы (CODE_VERIFIED + TEST_VERIFIED по READINESS_MATRIX).

### Точная матрица площадок (снята read-only из фактов кода: CATALOG × `_mode_for` × `capability_of`)

Capability: `publish` 21 · `manual_upload` 10 · `draft_only` 1 · `read_only` 0 · `unsupported` 0.
Auth-режимы: `vkhost` 1 (vk) · `oauth` 12 · `browser` 11 · `token` 8.

| Площадка | Каталог | Auth-режим | Capability | Что уже есть | Пробел до фулл-релиза |
|---|---|---|---|---|---|
| telegram | built_in | token | **publish** | Bot API publisher; браузер-вход web.telegram.org | API-токен бота вводится вручную (BotFather); login→token невозможен по дизайну Bot API |
| vk | built_in | vkhost | **publish** | VKHost login→авто-захват token; wall.post + фото | — (готово) |
| dzen | built_in | browser | **publish** | отдельная кнопка входа в профиль; браузерный редактор publish | — (готово) |
| max | built_in | token | **publish** | MAX messages adapter; браузер-вход web.max.ru | bot-токен вручную; login→token нет |
| x | built_in | oauth | **publish** | OAuth consent→token; tweets API | — |
| reddit | built_in | oauth | **publish** | OAuth; submit API; REDDIT_API_APPROVED gate | — |
| bluesky | built_in | token | **publish** | AT Protocol createSession+createRecord; браузер-вход | app-password вручную; **кандидат: AT Proto OAuth→авто-захват** |
| youtube | built_in | oauth | **publish** | OAuth; resumable upload MP4 | — |
| instagram | built_in | oauth | **publish** | OAuth; Graph container publish | — |
| threads | built_in | oauth | **publish** | OAuth; two-step container API | — |
| facebook | built_in | oauth | **publish** | OAuth; Page feed publish | — |
| linkedin | built_in | oauth | **publish** | OAuth; rest/posts | — |
| pinterest | built_in | oauth | **publish** | OAuth; pin create | — |
| odnoklassniki | built_in | token | **publish** | OK adapter; браузер-вход ok.ru | access-token вручную; **кандидат: OK OAuth→авто-захват** |
| discord | built_in | token | **publish** | webhook adapter; браузер-вход | webhook URL вручную (создаётся в настройках канала, не через login) |
| mastodon | built_in | oauth | **publish** | OAuth; statuses API + idempotency | — |
| medium | built_in | token | **publish** | legacy token; posts API | legacy-токен вручную; Medium API deprecated |
| pikabu | built_in | browser | **publish** | login→сессия; браузерный scheduled publish | — |
| tumblr | built_in | oauth | **publish** | OAuth; post create | — |
| telegraph | built_in | token | **publish** | createPage API; браузер-вход | токен вручную; **кандидат: createAccount→авто-токен (без login)** |
| wechat | built_in | token | **publish** | Official Account adapter; браузер-вход | app id/secret вручную (админка official account) |
| tiktok | built_in | oauth | manual_upload | OAuth consent→token; adapter = connection probe | **publish() намеренно skipped: нужен consent-gated Direct Post (per-video согласие)** |
| douyin | built_in | oauth | manual_upload | OAuth; adapter = connection probe | **publish() skipped: нужен consent-gated video create** |
| vc_ru | planned | browser | draft_only | login→сессия; браузерный редактор ЗАПОЛНЯЕТ черновик | **дожать до авто-publish через редактор (кнопку жмёт ферма)** |
| vk_clips | planned | browser | manual_upload | login→сессия vk.com/clips; manual-пакет | **адаптер VK Clips (VK API) — проверить поддержку, реализовать** |
| rutube | planned | browser | manual_upload | login→сессия; manual-пакет | **адаптер загрузки видео Rutube API — проверить, реализовать** |
| habr | planned | browser | manual_upload | login→сессия; manual-пакет | **браузерный авто-publish через редактор (расширить community_drafts)** |
| tenchat | planned | browser | manual_upload | login→сессия; manual-пакет | **браузерный авто-publish через редактор** |
| rednote | planned | browser | manual_upload | login→сессия xiaohongshu; manual-пакет | **браузерный авто-publish через редактор** |
| twitch | planned | browser | manual_upload | login→сессия; manual-пакет | **нет поддерживаемого publish-API для текстовых постов → честный manual_upload** |
| whatsapp_channels | planned | browser | manual_upload | login→сессия web.whatsapp; manual-пакет | **нет публичного posting-API каналов → честный manual_upload** |
| snapchat | planned | browser | manual_upload | login→сессия; manual-пакет | **нет публичного posting-API → честный manual_upload** |

**Вывод:** фулл-релиз — это НЕ «написать всё с нуля». 21/32 постинг и 24/32 авто-вход уже реализованы. Остаток — точечный, по трём блокам, с честными ограничениями там, где платформа не даёт поддерживаемого пути.

---

## БЛОК 1 — Автоавторизация всех площадок (login→браузер→захват)

**Уже готово (24):** vk (vkhost авто-token), 12 OAuth (consent→авто-token), 11 browser (login→сессия/cookies сохранены в профиль). `login_in_browser` реален: headed Chromium-профиль, ожидание auth-cookie, сохранение на диск, статус verified/saved_unverified. `PLATFORM_LOGIN_URLS` покрывает все 32.

**Пробел:** 8 token-площадок имеют браузер-вход (сессия сохраняется), но publisher использует API-ключ, который сессия НЕ отдаёт. Делим на «можно автоматизировать» и «честно вручную по дизайну платформы».

### Task 1A: bluesky — AT Protocol OAuth (авто-захват вместо app-password)
**Files:** investigate `tgfarm/bluesky_oauth.py` (создать), `tgfarm/platform_authorize.py` (OAUTH_PLATFORMS), `tgfarm/s9_growth/adapters.py` BlueskyAdapter, `tgfarm/platforms.py` _AUTH_FIELDS; tests.
- [ ] Шаг 0 (verify-first): подтвердить, что AT Protocol OAuth (bsky.social OAuth client metadata + PAR + DPoP) поддерживается и даёт access-token для createRecord. Если нет — зафиксировать ограничение, оставить app-password, НЕ притворяться.
- [ ] Шаг 1: RED-тест на oauth-режим bluesky в auth_matrix + авто-сохранение токена после consent.
- [ ] Шаг 2: GREEN — OAuth-модуль по образцу `x_oauth.py`/`mastodon_oauth.py`; перевести bluesky token→oauth; адаптер читает OAuth-token.
- [ ] Шаг 3: контур тестов + ruff + no-secrets; commit; push github; CI per-job.

### Task 1B: odnoklassniki — OK OAuth (авто-захват)
**Files:** investigate `tgfarm/odnoklassniki_oauth.py` (создать), `platform_authorize.py`, `s9_growth/odnoklassniki.py`, `platforms.py`; tests.
- [ ] Шаг 0 (verify-first): подтвердить OK OAuth 2.0 (application key/secret + redirect → access-token) для wall.post. Если публикация требует иных прав — зафиксировать.
- [ ] Шаг 1–3: RED→GREEN по образцу существующих OAuth-модулей; token→oauth; контур+CI.

### Task 1C: telegraph — авто-провижининг токена (createAccount, без login)
**Files:** `tgfarm/s9_growth/telegraph.py`, `platform_authorize.py`, `platforms.py`; tests.
- [ ] Шаг 0: подтвердить `createAccount` API возвращает access_token без пользовательского login (анонимный аккаунт).
- [ ] Шаг 1: RED-тест на авто-создание токена при подключении telegraph.
- [ ] Шаг 2: GREEN — кнопка «Подключить» вызывает createAccount, сохраняет токен локально; ручной ввод остаётся fallback.
- [ ] Шаг 3: контур+CI.

### Task 1D: честные ограничения token-площадок (документ + UI-подсказка)
**Files:** `platform_authorize.py` MODE_HINTS, `docs/READINESS_MATRIX.md`, UI connect-dialog copy; tests.
- [ ] Шаг 1: зафиксировать, что telegram (BotFather bot-token), max (bot-token), discord (webhook URL), medium (legacy token, API deprecated), wechat (app secret) НЕ имеют пути login→API-credential по дизайну платформы. Браузер-вход сессию сохраняет, но publish-ключ вводится вручную.
- [ ] Шаг 2: RED→GREEN тест, что UI честно показывает «нужен ключ площадки» для этих 5, а не «авто-вход». Не выдавать сессию за publish-credential.
- [ ] Шаг 3: контур+CI. **Решение владельца:** принять честное ручное ограничение ИЛИ заказывать отдельный продукт (Telegram MTProto user-auth / Discord OAuth bot-install) — это другой механизм постинга, вне текущего scope.

## БЛОК 2 — Постинг всех площадок (реализация; live-тест отложен)

**Уже готово (21 publish):** telegram, vk, dzen (primary) + bluesky, discord, facebook, instagram, linkedin, mastodon, max, medium, odnoklassniki, pikabu, pinterest, reddit, telegraph, threads, tumblr, wechat, x, youtube (реальные адаптеры с API-вызовами, idempotency, honest unknown/permanent/failed).

**Пробел (11):** tiktok, douyin (consent-gated), vc_ru (draft), vk_clips, rutube, habr, tenchat, rednote (manual), twitch, whatsapp_channels, snapchat (нет API).

### Task 2A: tiktok — consent-gated Direct Post
**Files:** `tgfarm/s9_growth/adapters.py` TikTokAdapter, `tgfarm/tiktok_oauth.py`, API/UI consent-endpoint, `platform_capability.py` (consent-gated остаётся manual_upload до явного согласия); tests.
- [ ] Шаг 0 (verify-first): подтвердить TikTok Content Posting API (Direct Post) — init→upload→publish, privacy options query, per-video consent. Требования к одобрению приложения зафиксировать.
- [ ] Шаг 1: RED-тест: publish без согласия = skipped (текущий инвариант); publish с явным per-video согласием = posted с remote-id.
- [ ] Шаг 2: GREEN — реализовать consent-gated flow; capability остаётся manual_upload пока владелец не дал согласие на конкретное видео (НЕ повышать до publish вслепую).
- [ ] Шаг 3: offline/dry-run тесты (без live); контур+CI. Live-тест — только после сообщения о готовности + отдельное согласие (§47).

### Task 2B: douyin — consent-gated video create
**Files:** `s9_growth/adapters.py` DouyinAdapter, `douyin_oauth.py`, consent-endpoint; tests.
- [ ] Шаг 0: подтвердить Douyin open API create_video (preview + owner confirmation).
- [ ] Шаг 1–3: RED→GREEN по образцу 2A; offline/dry-run; контур+CI.

### Task 2C: vc_ru — draft_only → браузерный авто-publish
**Files:** `tgfarm/community_drafts.py` (vc_ru spec), `platform_capability.py` DRAFT_ONLY, `s9_growth/adapters.py` (новый vc_ru adapter или community-publish путь); tests.
- [ ] Шаг 0: подтвердить, что редактор vc.ru позволяет ферме нажать «Опубликовать» в сохранённой сессии (сейчас только заполняет черновик).
- [ ] Шаг 1: RED-тест на авто-publish vc_ru через браузерный редактор (scheduled/posted outcome, honest unknown).
- [ ] Шаг 2: GREEN — расширить community_drafts publish-шагом для vc_ru; убрать из DRAFT_ONLY только если publish реально подтверждён offline.
- [ ] Шаг 3: offline-тесты (mock браузера, без live); контур+CI.

### Task 2D: vk_clips — адаптер VK Clips
**Files:** investigate `tgfarm/s9_growth/vk_clips.py` (создать), `adapters.py` ADAPTERS, `platforms.py` mode planned→built_in, `platform_capability.py`; tests.
- [ ] Шаг 0 (verify-first): подтвердить VK API для Clips (загрузка видео + публикация клипа через VK token). Если API не даёт publish — честный manual_upload.
- [ ] Шаг 1–3: RED→GREEN адаптер по образцу VkAdapter; offline/dry-run; контур+CI.

### Task 2E: rutube — адаптер загрузки видео
**Files:** investigate `tgfarm/s9_growth/rutube.py` (создать), `adapters.py`, `platforms.py`, `platform_capability.py`; tests.
- [ ] Шаг 0 (verify-first): подтвердить Rutube API загрузки/публикации видео (требования к партнёрскому доступу зафиксировать). Если нет поддерживаемого publish — честный manual_upload.
- [ ] Шаг 1–3: RED→GREEN; offline/dry-run; контур+CI.

### Task 2F: habr / tenchat / rednote — браузерный авто-publish
**Files:** `tgfarm/community_drafts.py` (новые PLATFORMS specs), `adapters.py`/community-publish, `platforms.py`, `platform_capability.py`; tests.
- [ ] Шаг 0 (verify-first, по каждой): подтвердить структуру редактора (поля заголовка/текста, загрузчик изображений, кнопка publish) в сохранённой сессии. rednote (xiaohongshu) — проверить доступность веб-редактора.
- [ ] Шаг 1: RED-тесты на браузерный publish каждой площадки (offline mock).
- [ ] Шаг 2: GREEN — расширить community_drafts по образцу pikabu/vc_ru; honest unknown если outcome не подтверждён.
- [ ] Шаг 3: offline-тесты; контур+CI. Площадка без надёжного редактора остаётся manual_upload с зафиксированным ограничением.

### Task 2G: честные ограничения — twitch / whatsapp_channels / snapchat
**Files:** `docs/READINESS_MATRIX.md`, `platform_capability.py` (остаются manual_upload), UI note; tests.
- [ ] Шаг 1: зафиксировать отсутствие поддерживаемого публичного posting-API: twitch (стримы/клипы, не текстовые посты), whatsapp_channels (нет публичного API каналов), snapchat (нет публичного posting-API).
- [ ] Шаг 2: RED→GREEN тест, что capability честно = manual_upload, пакет готовится (текст/заголовок/теги/медиа), загрузку выполняет человек. НЕ притворяться автопубликацией.
- [ ] Шаг 3: контур+CI. **Решение владельца:** принять честное ограничение (эти 3 не могут авто-публиковать поддерживаемым способом).

## БЛОК 3 — Все циклы программы S1–S12

**Уже реализовано (CODE_VERIFIED + TEST_VERIFIED):** S1 сбор, S2 отсеивание/dedup, S3 ranking, S4 generation/planner, S5 quality, S6 media, S7 schedule, S8 analytics, S9 crosspost, S10 metrics, S11 recommendation, S12 failure memory. Editorial planner, autonomy chain report, recovery-сценарии — есть.

### Task 3A: offline-верификация полноты контура S1–S12
**Files:** `tests/test_autonomy_chain_offline.py`, `tools/autonomy_chain_report.py`, `docs/READINESS_MATRIX.md`; read-only.
- [ ] Шаг 1: прогнать autonomy chain report на temp DB — каждый_stage absent/unknown ≠ measured-zero; подтвердить scheduler-reachable для всех стадий.
- [ ] Шаг 2: подтвердить recovery-сценарии (остановка/перезапуск, идемпотентность, publish_unknown не повторяется вслепую).
- [ ] Шаг 3: зафиксировать в READINESS_MATRIX: циклы реализованы + offline-tested; **live-колонки UNKNOWN** до OPS-1 (editorial-plan шов не наблюдался live).

### Task 3B: editorial-plan шов (scheduler-reachable) — offline-подтверждение
- [ ] Шаг 1: подтвердить, что plan_booking→generation→schedule шов покрыт offline-тестами (W19-A/B/C уже интегрированы).
- [ ] Шаг 2: live-наблюдение editorial_plan_build/apply/s4_plan_booking — ТОЛЬКО владелец после OPS-1 (PLAN_EXECUTION_ENABLED=true). Координатор НЕ включает флаг, НЕ перезапускает serve.

## БЛОК 4 — Техническая приёмка и операторские гейты (финализация)

### Task 4A: записать зелёный CI `5126aaf` в реестр/передачу/матрицу
**Files:** `docs/EXECUTION_PLAN.md` (§6, §11), `docs/CONTINUATION_STATE.md`, `docs/READINESS_MATRIX.md`.
- [ ] Шаг 1: CI #43828861262 на `5126aaf` — Linux success + Windows success; FP-класс закрыт `7b541e2`. Техническая база CLOSED.
- [ ] Шаг 2: убрать stale «f4b9496 ещё не запушен»; записать актуальный checkpoint.
- [ ] Шаг 3: commit docs; push github; сверка remote SHA.

### Task 4B: Windows EXE артефакт + build identity (offline)
- [ ] Шаг 1: скачать CI artifact `TgFarm-Windows-5126aaf`; проверить EXE + build identity (без запуска live-фермы).
- [ ] Шаг 2: подтвердить demo smoke / recovery / chain report / visual QA evidence на финальном SHA.

### Task 4C: операторские гейты (BLOCKED — выполняет ВЛАДЕЛЕЦ)
- [ ] Шаг 1: спросить владельца о закрытии PR #60/#56 как superseded (AskUserQuestion). Ветки не удалять.
- [ ] Шаг 2: чек-лист `.qoder/operator_checklist.md`: OPS-1 (serve restart PLAN_EXECUTION_ENABLED=true), OPS-2 (VK_USER_TOKEN + онлайн-LLM согласие), OAuth локально, по одной контролируемой публикации на тестовый аккаунт (§47 отдельное согласие на каждый), reconcile remote ID/URL, полный S1–S12 на тестовых аккаунтах.
- [ ] Шаг 3: ротация токена в логе — рекомендация оператору (НЕ выполнять).
- [ ] Шаг 4: emergency stop / publish_unknown reconcile — подтвердить инварианты.

### Task 4D: сообщение о готовности + финальный русский отчёт
- [ ] Шаг 1: когда Blocks 1–3 реализованы и CI зелёный — отправить владельцу **сообщение о готовности площадок** (после него владелец разрешает live-тест).
- [ ] Шаг 2: отчёт: что реализовано (SHA/run/счётчики), честные ограничения (какие площадки не могут авто-публиковать/авто-входить и почему), разделение технической и live-готовности. Не объявлять live-готовность без evidence владельца.

## Порядок волн (приоритет)

1. **Волна A (быстрые auth-апгрейды):** 1A bluesky OAuth, 1B OK OAuth, 1C telegraph auto-token, 1D честные ограничения. Независимые файлы → можно параллелить при возврате субагентов.
2. **Волна B (consent-gated post):** 2A tiktok, 2B douyin. Общий consent-endpoint → координатор закрывает шов.
3. **Волна C (браузерный post):** 2C vc_ru, 2F habr/tenchat/rednote. Общий community_drafts → последовательно, чтобы не конфликтовать.
4. **Волна D (video API):** 2D vk_clips, 2E rutube. Verify-first.
5. **Волна E (честные ограничения post):** 2G twitch/whatsapp/snapchat.
6. **Волна F (циклы + приёмка):** 3A/3B offline-верификация, 4A/4B документы+EXE, 4C операторские гейты, 4D сообщение о готовности.

## Self-review

- Покрытие трёх блоков владельца: Block 1 (auth) → Tasks 1A–1D; Block 2 (post) → Tasks 2A–2G; Block 3 (циклы) → Tasks 3A–3B. Все 32 площадки имеют строку матрицы и явный путь (готово / реализуемо / честное ограничение).
- Placeholder scan: каждый task имеет verify-first шаг (не выдумывать поддержку API), RED→GREEN, контур+CI. Конкретные файлы названы.
- Честность: площадки без поддерживаемого publish-API (twitch/whatsapp/snapchat) и без login→token (telegram/max/discord/medium/wechat) НЕ выдаются за автоматические; ограничение фиксируется, решение владельца спрашивается.
- Live-тест отложен: все post-задачи проверяются offline/dry-run; сообщение о готовности (4D) предшествует live-тесту владельца.
- Типы/имена: `capability_of`, `_mode_for`, `ADAPTERS`, `MANUAL_DESTINATIONS`, `community_drafts.PLATFORMS`, `login_in_browser`, `PLATFORM_LOGIN_URLS` — существующие контракты, сняты read-only.