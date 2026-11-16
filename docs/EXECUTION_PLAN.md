# TgFarm — единый план работ

> Рабочий реестр состояния. Перед любым новым изменением повторно сверять main, файлы и CI; исторические счётчики и SHA не считать текущими без проверки.

**Дата среза:** 2026-11-16  
**Основная ветка:** `main`  
**Последний функциональный checkpoint:** `f4b9496c6919584abb46860b95502186afedcb02`  
**Правило интеграции:** по запросу владельца продолжаем непосредственно в `main`; отдельные ветки не плодим.  
**Live-ограничение:** без отдельного подтверждения владельца не меняем production-БД, секреты, режим LIVE и реальные публикации.

## 1. Что теперь находится в main

Уже присутствует и сохраняется:

- Command Center и рабочая очередь публикаций: PR #62/#63 были объединены раньше.
- Content Studio, AI Team и Analytics: добавлены в main в ноябрьском цикле.
- Editorial Planner: обзор плана, состояние исполнителя, очередь ожидающих брони, список решений редакции и история ревизий. Используются существующие API, freshness, сохранение последнего успешного снимка и различение пустого ответа от ошибки.
- Community Policy и platform capability registry: политика площадок и фактические уровни возможностей не подменяются обещанием «всё автопостится».
- S1–S12, база данных, адаптеры, очередь, аналитика, видео и механизмы восстановления присутствуют в кодовой базе. Код и offline-тесты сами по себе не подтверждают работу с реальными аккаунтами.

### Перенос открытых PR

| Источник | Что было перенесено в main | Коммиты |
|---|---|---|
| PR #60 | Desktop OAuth, стабильный loopback callback, запуск системного браузера, PKCE LinkedIn/TikTok/YouTube, безопасный YouTube reconnect, credential-based flows Medium/WeChat/Odnoklassniki, четыре независимых статуса площадки и UI подключения | `e61cd3c`, `c81ac6a` |
| PR #56 | Агрегация недавних PipelineRun и CrosspostLog ошибок, связь CrosspostLog с каналом через Post, redaction секретов, bounded/idempotent sample; учтены permanent-отказы и изолированы сбои двух источников | `4ca2570` |

Функциональный код обоих PR перенесён выборочно; более новые UI/планировщик/аналитика из main не заменялись старыми версиями веток. Открытые PR #60 и #56 остаются дубликатами до подтверждения CI и закрытия как superseded.

### Авторизация и готовность площадок

- Desktop OAuth открывает официальный сайт через системный браузер, а callback слушает loopback-интерфейс на стабильном порту установки.
- LinkedIn native OAuth использует PKCE с S256 Base64URL; TikTok Desktop использует PKCE с S256 hex; YouTube Desktop использует PKCE S256 и loopback callback.
- YouTube reconnect не должен подставлять refresh token предыдущего аккаунта, если новый OAuth grant его не вернул.
- Реквизиты приложения и токены сохраняются на backend; UI не получает сохранённые секреты.
- Площадка показывает отдельно: авторизацию аккаунта, доступ API/transport, назначение и готовность публикации. Сессия браузера или заполненный Client Secret не означает готовую публикацию.
- Medium и WeChat Official Account используют их credential-based flows; Odnoklassniki переведён на серверный OAuth (ключи приложения → login → авто-захват токена, `2027845`); VKHost и browser profile остаются отдельными способами доступа.
- Реальный OAuth по-прежнему требует credentials приложения и интерактивного согласия владельца аккаунта.

### S12 — обучение на сбоях

- Pipeline failures агрегируются по стадии; crosspost failures — по площадке и только для конкретного канала через связь `CrosspostLog → Post`.
- В память сохраняется ограниченная последняя выборка, несколько свежих примеров и идентификаторы источников. Повторное чтение тех же строк не раздувает sample.
- Секреты редактируются до записи в LearningMemory.
- В выборку входят `failed` и `permanent` отказы назначения; `publish_unknown` не превращается в обычную ошибку и не даёт основания для повтора.
- Ошибка чтения PipelineRun не выбрасывает CrosspostLog evidence и наоборот.

## 2. S1–S12: состояние и незакрытая приёмка

| Стадия | Реализованная основа | Что ещё не доказано |
|---|---|---|
| S1 Research | сбор источников и сохранение source context | качество/свежесть реальных источников на длительном цикле |
| S2 Trend Intelligence | тренды, кластеры и сигналы | устойчивость ранжирования и объяснимость на реальном потоке |
| S3 Router | маршрутизация по площадкам/каналам и community policy | проверка каждого фактического назначения и платформенных правил |
| S4 Editorial | briefs, генерация и редакционный план | полностью автономный end-to-end переход темы в корректный материал на каждой площадке |
| S5 Review Board | quality gates, review и блокировки | независимость проверок и достаточность фактчекинга на реальных материалах |
| S6 Visual Media | media preparation и cover workflow | релевантность, provenance, лицензии и обязательные требования всех адаптеров |
| S7 Scheduling & Publishing | очередь, таймауты, unknown state, изоляция ошибок и восстановление | разрешённая контролируемая live-публикация и reconcile для каждой площадки |
| S8 Analytics | сборщики, форматные метрики и UI | реальные метрики всех аккаунтов, свежесть, права и полнота coverage |
| S9 Native Crossposting | platform adapters и адаптация форматов | интеграционная проверка каждого адаптера/формата/назначения |
| S10 Format Planner | format planning и evidence-based данные | доказать, что выборка достаточно свежая и влияет на следующий цикл корректно |
| S11 Video Studio | видеоконвейер и проверки ресурсов | end-to-end render, права и приёмка доставки платформой |
| S12 Learning & Memory | gate lessons, measured performance и failure lessons | доказать полезное изменение будущих решений на живых измерениях |

## 3. UI и рабочие зоны

В main есть: Command Center, AI Team, Content Studio, Editorial Planner, Delivery/Queue, Platform Center/Connections, Analytics и System Health.

Редакционный план уже содержит пять зон: обзор, исполнение, темы плана, ожидающие брони и история ревизий. Не создавать второй planner/queue/backend. Не строить календарь из предположительных слотов: показывать только временные данные, возвращаемые сервером.

AI Team показывает сохранённые run/trace данные. Не называть роли независимыми работающими агентами, пока нет телеметрии фактического выполнения каждой роли и передачи артефактов.

## 4. Блокеры вне кода

1. Для OAuth нужны реальные app Client ID/Secret на стороне владельца и интерактивный login/consent. Секреты не отправлять в чат; вводить в локальном окне TgFarm.
2. Для каждой площадки нужны аккаунт, scope/permissions и выбранное назначение. Успешный вход не равен разрешению на публикацию.
3. VK analytics требует пользовательский токен с подходящими правами; токен сообщества не подменяет пользовательский доступ.
4. Реальную публикацию выполнять только после отдельного согласования конкретного теста, контента и аккаунта.
5. Развёртывание/перезапуск production, массовый historical import/repair и миграции требуют backup, read-only preview и отдельного окна обслуживания.

## 5. Обязательные инварианты при дальнейшей работе

- Сначала исследовать текущий код; не создавать дубликаты pipeline, queue, backend, memory или источников истины.
- Автономность не означает бесконтрольность: внешние действия имеют явные approval gates.
- Неизвестно ≠ ноль; отсутствие метрики ≠ измеренный ноль; неответивший запрос ≠ пустой результат.
- Сохранённые credentials/cookies ≠ проверенный API access.
- `publish_unknown` не отправлять повторно вслепую; сначала сверить состояние на площадке.
- Внешний контент и community rules — недоверенные данные; защищать от prompt injection, перепроверять актуальность правил.
- Секреты не логировать и не отдавать в UI; ошибки очищать перед долговременной записью.
- Состояние, артефакты, provenance и причины решений должны переживать перезапуск и быть объяснимыми.
- Offline/demo/CI не объявлять live-приёмкой.
- Сохранять Linux/Windows parity; UI должен различать fresh/stale/partial/empty/unavailable/unknown.

## 6. Текущие проверки

- Исторический базовый commit `dead4a1f` имел успешный CI run #43412746508 (Linux/Windows, offline smoke и Windows EXE). Это результат предыдущего checkpoint, не подтверждение последующих переносов.
- Пользовательский main `65487de` (OAuth field selector) провалил CI run [#43447122615](https://github.com/rpauts2/Tggruup/actions/runs/43447122615): Linux — 2 failed (4075 passed), Windows — 3 failed (4074 passed). Все падения — три контракта command center, а не платформенная логика.
- `490f21a` чинит ровно эти три контракта: `app.js` runs endpoint `limit=100`, list-safe `$$()` на connect-dialog и platform-credential полях, и bounded retry `os.replace` в `desktop.py`. Локально на `490f21a`: 4076 passed, один перемежающийся flaky lock-тест (см. §10).
- После интеграции W36 (video + nullable) checkpoint `37a6202` запушен в `github` (fast-forward `65487de..37a6202`, без force); remote SHA сверен равен `37a62021d2c5efa6b998ccd70c03414b665a5660`.
- CI run [#43484978287](https://github.com/rpauts2/Tggruup/actions/runs/43484978287) на `37a6202` завершён: **Linux `test` — success**, **Windows — failure** (`18 failed, 4283 passed, 15 skipped`). Все 18 падений — один параметризованный тест `test_redirected_adjacent_sidecar_is_rejected_before_encode_or_write[symlink-…]`: на CI Windows (где есть privilege на symlink) `readlink()` возвращает target с extended-length NT-префиксом `//?/C:/…`, а пост-условие сравнивало его с plain `foreign`. Renderer корректно отклонил sidecar (`pytest.raises(ValueError)` прошёл); падало только пост-условие. Локально эти 18 случаев SKIP (WinError 1314), что скрыло дефект от ревью пакета.
- `073e22f` чинит перемежающийся flaky lock-тест Windows (placeholder write внесён в loser guard; ~48% падений → 30/30). `b6dee9e` чинит symlink-пост-условие (сравнение `as_posix()` со снятым `//?/`; no-op на POSIX, репоинт на другой файл по-прежнему ловится). Оба — READY-WITH-NITS независимого ревью.
- Новый функциональный checkpoint `b6dee9e4efb8a67c46b9d2dd5e6c1038a8e6ad59` (lock + symlink fixes поверх `37a6202`). Локальная полная suite: `4283 passed, 33 skipped, 4 xfailed, 0 failed`.
- **CI GREEN:** run [#43489547500](https://github.com/rpauts2/Tggruup/actions/runs/43489547500) на `a7e6de8a9d44ce74b1e0d9c2272961d56daf04ae` (docs поверх `b6dee9e`) завершён: **Linux `test` — success, Windows — success** (`4301 passed, 15 skipped, 4 xfailed`). 18 symlink-случаев выполнены и прошли на CI Windows (4283 локальных + 18 = 4301), что подтверждает `b6dee9e`; flaky lock устранён `073e22f`. Checkpoint `b6dee9e` зелёный.

## 7. Следующий приоритет

1. Полная верификация текущего checkpoint (`f4b9496` + queue/docs коммиты) → push `github` → прочитать CI per-job → записать run.
2. После зелёного CI закрыть PR #60 и #56 как superseded **только с подтверждением владельца** (общее состояние GitHub), сохранив ссылку на commits в main.
3. Техническая релизная приёмка offline: demo smoke, recovery-сценарии, autonomy chain report на temp DB, ui-visual-qa, Windows EXE артефакт + build identity.
4. Подготовить матрицу реальной OAuth/публикации/метрик по платформам и выполнить только разрешённые владельцем полевые проверки (OPS-1/OPS-2, live-credentials).
5. Доказать полный S1–S12 lifecycle: от обнаружения темы до измеренных результатов и урока, который меняет следующий цикл.
6. Закрыть deployment/restart/backup acceptance на Linux server и Windows Desktop.

Документ обновляется после каждого значимого интеграционного набора. Не переносить старые статусы процессов, PID, БД и тестов в новый checkpoint без повторной проверки.


## 8. Дополнительное закрытие ревью и надёжности — 2026-11-13

После переноса PR #60/#56 проверены все замечания CodeAnt. В `main` добавлены исправления:

- Desktop single-instance использует удерживаемый OS advisory lock (Windows и POSIX), а не схему check-then-write по PID. Lock-файл постоянный; блокировка автоматически освобождается ядром при завершении или аварии процесса.
- Desktop port metadata записывается через уникальный временный файл и атомарный replace. Существующий Redirect URI не меняется автоматически: если зарегистрированный порт занят, запуск явно останавливается с объяснением вместо незаметного callback mismatch.
- Reddit OAuth в матрице и UI разрешён только при наличии Client ID/Secret и явного `REDDIT_API_APPROVED=true`. Значение по умолчанию остаётся `false`; владелец должен получить одобрение Reddit и вручную включить флаг. API start endpoint сохраняет серверный 403 gate.
- VKHost проверяет наличие уже сохранённого `VK_ACCESS_TOKEN`; само наличие токена отражается как pending, пока реальная проверка API не подтверждена.
- S12 удаляет уроки по pipeline/crosspost, выпавшие из свежего окна, только после успешного чтения соответствующего источника. При сбое запроса прежние уроки сохраняются, а другой источник продолжает обрабатываться независимо.
- Добавлены регрессионные тесты конкурентного запуска Desktop, безопасного сохранения порта, Reddit approval, сохранённого VK токена и очистки устаревших S12 уроков.

Кодовые commits: `971bdaeb340fd5f2aa1d23245e0aeae7c3854af4` и `64cd0e515c5e9bd5fa815b129e4e273e3f5ec9a0`.

## 9. Полный перенос открытых веток

Уникальные функциональные части PR #60 и #56 перенесены в `main`; исходные PR остаются открытыми до успешного CI текущего checkpoint, после чего будут закрыты как superseded (не объединять повторно). PR #59 — ранняя версия OAuth ветки #60. Ветки planner/editorial, posts workspace, command center, S5, Retry-After и legal windows уже представлены в main через соответствующие merged PR или совпадающие файловые SHAs; отдельный дубль не создаётся.

Новая полная проверка: [CI run #43445803450](https://github.com/rpauts2/Tggruup/actions/runs/43445803450) для `64cd0e5`. На момент обновления документа выполняется на Linux и Windows; пока не объявлена зелёной.

## 10. CI-ремонт и интеграция W36 — 2026-11-13

### Аудит пользовательских веток (только чтение)

Четыре независимых read-only агента сверили все ветки/remote с `main` на уровне содержимого (не только patch-id). Вывод unanimous: уникального пользовательского кода, которого нет в `main`, не осталось — всё уже перенесено и переработано под более новые UI/планировщик/аналитика. `git cherry` показывает `+` (patch-unique) для всех веток, но это обманчиво: `main` содержит те же контракты под другими SHA. Авторитетен content-grep, а не patch-id. Подтверждено собственным §9 владельца: «отдельный дубль не создаётся», «не объединять повторно». Whole-branch merge запрещён — все ветки net-negative относительно `main` (регрессировали бы более новый код). Ничего не портировалось повторно; rejected/unknown работа сохранена.

### Ремонт красного CI пользовательского main

Пользовательский `65487de` провалил CI run #43447122615 (Linux 2 failed, Windows 3 failed). Диагностика: три контракта command center, не платформенная логика. `490f21a` чинит ровно их:

- `app.js`: runs endpoint `limit=40 → limit=100` (контракт editorial workspace wiring).
- `app.js`: single-element `$().forEach → $$()` на connect-dialog и platform-credential полях (дефект-класс 2026-11-04, обрывавший весь refresh).
- `desktop.py`: bounded retry `os.replace` на Windows `PermissionError`, чтобы конкурентные писатели `_store_desktop_port` не теряли порт и не оставляли temp-файлы.

Отдельно диагностирован env-артефакт: `NO_PROXY=…,[::1],…` ломает httpx 0.28.1 (`InvalidURL: Invalid port: ':1]'`), давая 8 ложных auth/telegram падений локально. Это не дефект кода; CI-runner не несёт этого значения. Локальные прогоны выполняются с `NO_PROXY= no_proxy=`.

### Интеграция W36 (video + nullable)

Два независимо проверенных пакета интегрированы в `main` через `w36-integration`:

- **W36-B video provenance** (`b27ad52`, commits `cd600b6`/`2e2f109`): rollback-бэкапы в disjoint namespace `frame_dir/"backups"`; per-destination try/except по `reversed(published)` с recovery в `tgfarm-short-recovery-*`. Reviewer READY, mutation-tested. `tests/test_video_cache_provenance.py` (+905), `tests/test_video_renderer.py`, `tgfarm/s11_video/renderer.py`. 18 skips — Windows symlink winerror 1314 (среда, не код).
- **W36-C nullable rate contract** (`37a6202`, commits `3705eb8`/`97a16e3`/`c602442`): отсутствующая rate/quality остаётся `None`, не coerced в ноль; `_quality(None) → None`, `estimate_group` фильтрует `None`; None-quality исключён из slot/rate denominators и baselines. `tests/test_nullable_quality_consumers.py` (+232), `tests/test_post_metric_rate_validity.py` (+266), `tgfarm/s8_analytics/{__init__,portfolio}.py`, `tgfarm/s12_learning/__init__.py`.

Инвариант «неизвестно ≠ ноль; отсутствие метрики ≠ измеренный ноль» сохранён; +159/-46 rewrite S12 из `main` не заменён старой версией ветки — None-gate применён поверх.

### Проверка и публикация

- Интеграционное дерево `37a6202`: 399 затронутых тестов зелёные; полная suite `4282 passed, 33 skipped, 4 xfailed, 1 failed` (единственное падение — перемежающийся flaky lock-тест, впоследствии починен `073e22f`).
- No-secrets gate: 9 файлов, без миграций, чисто.
- `main` fast-forward `65487de..37a6202` запушен в `github` (без force); remote SHA сверен равен `37a62021d2c5efa6b998ccd70c03414b665a5660`.
- CI run #43484978287 на `37a6202` завершён: Linux success, Windows failure (18 symlink readlink-префикс, см. §6). Это выявило дефект тест-пост-условия, скрытый локальными WinError 1314 skips.

### Пост-интеграционный ремонт CI (checkpoint `b6dee9e`)

Два независимых исправления поверх `37a6202`, оба READY-WITH-NITS отдельного ревью:

- `073e22f5bd131aa853fda2b4d28e1d897ed502e4` — **flaky SingleInstance lock (Windows)**. Root cause подтверждён эмпирически: `PermissionError [Errno 13]` на `desktop.py:77` (flush placeholder-записи). Winner держит mandatory lock на byte 0 и truncate-ит файл; более медленный конкурент всё ещё делает pre-lock append на заблокированный byte 0 → исключение вне inner `OSError` guard → re-raise через `BaseException`. Fix: placeholder write внесён в тот же loser guard (`except (OSError, BlockingIOError) → return False`). Инвариант exactly-one-winner сохранён (placeholder конкурента падает только когда byte 0 уже заблокирован winner-ом). POSIX flock advisory — не затронут. Доказательство: до фикса ~48% падений (12/25), после — 30/30; полный desktop-файл 57/57; ревьюер независимо воспроизвёл pre-fix 10/50 PermissionError vs post-fix 0/50.
- `b6dee9e4efb8a67c46b9d2dd5e6c1038a8e6ad59` — **symlink readlink NT-префикс (Windows CI)**. 18 параметризованных случаев `test_redirected_adjacent_sidecar_is_rejected_before_encode_or_write[symlink-…]` падали на пост-условии `readlink() == foreign`: CI Windows возвращает `//?/C:/…`, foreign — plain. Renderer отклонил sidecar корректно (`ValueError` прошёл); падало только сравнение. Fix: `readlink().as_posix().removeprefix("//?/") == foreign.as_posix()` — no-op на POSIX, репоинт на другой файл по-прежнему ловится (assertion НЕ ослаблен). `resolve()`/`realpath()`/`normpath()` префикс сохраняют — работает только stripping. Локально symlink-ветка SKIP (WinError 1314), поэтому проверено pure-path red-green (OLD False → NEW True для same-parent/outside-parent/dangling) + 154 passed/18 skipped файла; реальное подтверждение — CI Windows.

Локальная полная suite на `b6dee9e`: `4283 passed, 33 skipped, 4 xfailed, 0 failed` (flaky lock устранён). **CI GREEN:** run #43489547500 на `a7e6de8` завершён — Linux `test` success, Windows success (`4301 passed, 15 skipped, 4 xfailed`); 18 symlink-случаев прошли на CI Windows, подтверждая `b6dee9e`.

### Открытый остаток

- После зелёного CI закрыть PR #60/#56 как superseded со ссылкой на commits в `main` (подтвердить перед закрытием — общее состояние GitHub).
- Локальный аккаунт Windows не имеет privilege на symlink (WinError 1314), поэтому 18 symlink-случаев локально SKIP и проверяются только на CI Windows. Это легитимный skip по privilege, не vacuous-тест; но он скрыл дефект пост-условия от ревью пакета — учитывать при будущих symlink-тестах.

## 11. Финальная волна к релизу — 2026-11-16

> Субагенты (Explore/Plan/general-purpose) в этой сессии недоступны — исполнение и review-проходы последовательные, координатором. Независимое субагентное ревью не выполнялось; это зафиксировано в [RELEASE_PLAN.md](RELEASE_PLAN.md).

### Принятие пользовательского WIP и пробел аудита веток

- **W38-A `8e31c30`** — operator-tunable дневные лимиты VK/Dzen. Пользовательский WIP верифицирован (337 тестов контура), отревьюен, nit исправлен. `primary_platform_daily_cap(platform)`: явный override ≠ default → Settings field (`PRIMARY_VK_DAILY_CAP`/`PRIMARY_DZEN_DAILY_CAP`, `ge=1`) → code default `{dzen:2, vk:3}`; unknown platform = KeyError. Plan report и publisher читают одну функцию; lazy import в distribution разрывает цикл s7↔distribution. 8 новых тестов.
- **W38-B `15c14c9`** — analytics CSS. Повторный content-аудит 17 github-веток нашёл **один настоящий пробел**: панель уже рендерит `.analytics-format-row`/`.analytics-evidence-note`, но `app.css` их не определял. Портирован через TDD (RED 2 failed → GREEN 41 passed): container-query + media fallback, markup не менялся. 3 offline UI contract теста.
- **Коррекция аудита:** прежний вывод §10 «уникального кода вне main не осталось» был неточен — analytics CSS оставался неперенесённым. После W38-B пробел закрыт; whole-branch merge по-прежнему запрещён (net-negative).

### W34-B — per-delivery preparation budget

- **`f4b9496`** — один deadline на доставку покрывает community selection, AI adaptation, rules recheck и publish wait. AI-доля `AI_ADAPTATION_BUDGET_SHARE = 0.5`, чтобы зависший LLM не съел publish-окно; timeout там сохраняет детерминированную адаптацию. Timeout подготовки ДО любого publish-запроса = retryable `failed`/`skipped` с Retry-After, **никогда `unknown`**. Отмена после claim пишет shielded `failed`-строку и разрешает attempt. Dry-run и publish-phase `unknown` контракт W36-A не тронуты.
- TDD: RED 4 failed → GREEN 20 passed (4 новых + 16 W36-A; эволюция 5 assertions с сохранением инвариантов — ranges всё ещё фиксируют share-formula upper bound, «not unknown» усилен разрешением claim). Широкий контур: 125 passed + 640 passed/4 xfailed/0 failed. Ruff-паритет точный (те же 5 pre-existing ошибок, новых нет). No-secrets scan чист.
- Исходный отклонённый diff W34-B **не интегрировался**; байты сохранены в worktree. Review-проход координатора подтвердил: claim после scout, rendering до claim, короткие DB-write вне long-await.

### Реконсиляция очереди

- **`8069985`** — W24-B FAILED → CANCELLED (superseded W24-BR INTEGRATED; артефакты спасены `14c165a`, CI build-identity шаги зелёные на #43489547500). W34-B RECOVERY_REQUIRED → INTEGRATED (`f4b9496`). Новые карточки W38-A/W38-B. `queue_check`: **49 tasks, 0 problems**; `tests/test_w25a_queue.py` 43 passed.

### CI и checkpoint

- **CI GREEN:** run [#43489547500](https://github.com/rpauts2/Tggruup/actions/runs/43489547500) на `a7e6de8` — Linux `test` success, Windows success (`4301 passed, 15 skipped, 4 xfailed`). Это подтверждает checkpoint `b6dee9e` (lock + symlink).
- Текущий локальный checkpoint `f4b9496` (+ queue `8069985`, docs) — **ещё не запушен**; полный suite, push `github`, чтение нового CI per-job и запись run выполняются в задаче 6 RELEASE_PLAN. Не объявлять новый checkpoint зелёным до завершения и чтения CI.

### Волна A — auth-апгрейды площадок (API) — 2026-11-16

- **telegraph `5a0b262`** — авто-выдача access-token через `createAccount` (анонимный, без login): токен захватывается и сохраняется локально, не возвращается в браузер. CI Linux green.
- **OK (odnoklassniki) `2027845`** — серверный OAuth 2.0: владелец сохраняет только ключи приложения OK, жмёт «войти» → `connect.ok.ru/oauth/authorize` → callback меняет код на `api.ok.ru/oauth/token.do`, выводит секрет сессии `MD5(access_token+application_secret)` и подтверждает профиль (`users.getCurrentUser`). Токен хранится локально, не эхо в браузер. Площадка переведена из token-режима в oauth в auth-матрице; start/callback endpoints подключены к общему redaction-middleware и public-path exemption (`apiauth.PUBLIC_PATHS`). TDD: 8 модульных тестов RED→GREEN + API-callback seam-тест (RED 401 → root-cause `PUBLIC_PATHS` → GREEN). Контур 155+66 passed; полный suite **4316 passed, 33 skipped, 4 xfailed, 0 failed**. Ruff: новые файлы чисты; api.py +9 ошибок того же baseline-паттерна, что у всех 12 sibling-callback (I001 `import html` + E501 HTML-строки), CI ruff не запускает. Публикация в группу требует одобрения OK `GROUP_CONTENT` — токен сам по себе права не даёт (ограничение зафиксировано).
- **bluesky — verify-first, ОТЛОЖЕН.** AT Proto OAuth требует hosted client-metadata URL + DPoP + PKCE — блокер для локальной фермы без публичного домена. Существующий app-password API (`createSession`+`createRecord`) уже авто-постит и остаётся лучшим практическим методом. Коррекция внесена в [PLATFORM_POSTING_RESEARCH.md](PLATFORM_POSTING_RESEARCH.md) (по образцу vk_clips verify-first).
- **Итог Волны A:** API auth-апгрейды закрыты. `capability_of`: bluesky/odnoklassniki/telegraph = `publish`. Остальные 11 пробелов (vc_ru draft_only; twitch/whatsapp_channels/snapchat/vk_clips/rutube/habr/tenchat/rednote/douyin/tiktok manual_upload) честно классифицированы и НЕ притворяются publish. Их авто-publish — браузерные селекторы, требующие LIVE-верификации (операторский гейт, отложен до сообщения о готовности).

### Волна C — браузерный авто-publish: vc.ru (gated) — 2026-11-16

- **vc.ru `6ada5cb`** — реализован `community_drafts.publish_vc_ru` + `_vc_article_url` + `s9_growth.adapters.VcRuAdapter` по проверенному образцу pikabu (`publish_pikabu_scheduled`). Гейты: rules-proof (`check_destination_rules_proof` + `policy_verdict.allow_publish`), защита от дублей (fingerprint + state-machine preparing/submitting/published/unknown/failed), проверка сессии (`browser_session_status` verified+profile_saved), отказ без playwright (optional-extra), финальная перечитка правил перед кликом, честное подтверждение успеха по URL статьи `vc.ru/{topic}/{id}-{slug}` (редактор `vc.ru/editor` НЕ считается публикацией), `unknown` никогда не повторяется вслепую. Адаптер: `published`→`posted`, `unknown`→`unknown`, `CommunityDraftError`→`skipped`+retry 6h.
- **TDD:** 7 новых тестов RED→GREEN (`tests/test_community_drafts.py`): rules-proof-missing→skipped, no-verified-session→raise, changed-material≠published→raise (no dup), same-material published→idempotent, adapter not-configured→skipped, confirmed→posted, unknown→unknown (no retry). Полный suite **4323 passed, 33 skipped, 4 xfailed, 0 failed** (4316 baseline + 7). Ruff-паритет с HEAD по всем 4 файлам (20=20, 25=25, 3=3, 1=1), ноль новых ошибок.
- **Gated-activation (честно, §2.1/§4):** `VcRuAdapter` НЕ зарегистрирован в `ADAPTERS`; `platform_capability.DRAFT_ONLY` по-прежнему содержит `vc_ru`; `platforms.py` CATALOG vc_ru остаётся `mode="planned"` (**net-zero**, без promotion). Capability честно `draft_only`, НЕ `publish`. Причина: селекторы кнопки publish в редакторе vc.ru НЕ подтверждены вживую (операторский гейт; live-тестирование отложено владельцем). Повышение vc.ru до `publish` изменило бы `rules_watch_switch` (производная от `capability_of`) и сломало бы ~20 тестов `test_maintenance_vc_rules_watch.py` (vc_ru как switch-less draft-only под непрерывным наблюдением). **Шаг активации** (после live-verify + согласия оператора): зарегистрировать адаптер + promote CATALOG + пересмотреть rules-watch тесты.
- **Разведка следующих площадок (verify-first, записана в [PLATFORM_POSTING_RESEARCH.md](PLATFORM_POSTING_RESEARCH.md)):** habr/tenchat/rednote — greenfield в браузерном publishing-контуре (нет в `community_drafts.PLATFORMS`, нет domain-allowlist/targeting/profile), каждая пакет на 6–8 файлов. habr: «Написать»→1–5 хабов→обязательные теги→WYSIWYG→«Опубликовать», karma-eligibility + 24h rate-limit после 3 быстрых постов, confirm по `/ru/articles/{id}/`. tenchat: публичного API постинга НЕТ, только BROWSER-сессия владельца. Селекторы под LIVE-verify до активации; capability честно не повышается вслепую.
- **CI:** код-коммит `6ada5cb` + docs `db75dfa` запушены в `github` (fast-forward `cd1faa1..db75dfa`, без force; remote SHA сверен == `db75dfae4d78a0ca87250fd66278d1d71d486022`). Run [#43848029942](https://github.com/rpauts2/Tggruup/actions/runs/43848029942) на `db75dfa` — **GREEN, independently verified per-job:** `test`=success/completed, `windows`=success/completed, conclusion=success. Предыдущий run [#43841607366](https://github.com/rpauts2/Tggruup/actions/runs/43841607366) на `cd1faa1` (Wave A docs) — independently verified: `test`=success, `windows`=success.
