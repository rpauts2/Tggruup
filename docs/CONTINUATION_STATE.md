# TgFarm — continuation checkpoint

> Этот файл — краткая передача следующему инженерному циклу. Единственный подробный реестр статуса: [EXECUTION_PLAN.md](EXECUTION_PLAN.md). Для всех требований см. [MASTER_REQUIREMENTS_TRACEABILITY.md](MASTER_REQUIREMENTS_TRACEABILITY.md).

**Дата:** 2026-11-16  
**Основная ветка:** `main`  
**Последний функциональный checkpoint:** `f4b9496c6919584abb46860b95502186afedcb02`

## Что перенесено в main в этом заходе

- `e61cd3c` — backend Desktop OAuth, стабильный loopback listener, system browser, LinkedIn/TikTok/YouTube PKCE, корректный YouTube reconnect, платформенные credential flows и регрессионные тесты.
- `c81ac6a` — UI для Client ID/Secret, official docs/Redirect URI, system browser в PyWebView, Dzen flow completion, четыре независимых proof статуса платформы и восемь Desktop/OAuth тестов.
- `4ca2570` — S12 failure memory: ошибки pipeline и crosspost агрегируются отдельно, crosspost связан с конкретным каналом, secret redaction, повторное чтение идемпотентно, permanent rejection включён, сбой чтения одного источника не уничтожает данные другого.

Открытые PR #60 и #56 содержат теперь уже перенесённые изменения и должны быть закрыты как superseded после успешного CI. Не пытаться повторно вливать старые ветки.

## Текущее CI

Пользовательский main `65487de` провалил run [#43447122615](https://github.com/rpauts2/Tggruup/actions/runs/43447122615) (Linux 2 failed, Windows 3 failed) — три контракта command center. `490f21a` чинит ровно их. После интеграции W36 checkpoint `37a6202` запушен в `github` (fast-forward, без force), remote SHA сверен.

Run [#43484978287](https://github.com/rpauts2/Tggruup/actions/runs/43484978287) на `37a6202` завершён: **Linux success, Windows failure** (`18 failed, 4283 passed, 15 skipped`). 18 падений — symlink readlink NT-префикс в тест-пост-условии W36-B (локально скрыто WinError 1314 skips). Renderer поведение корректно; падало только сравнение `readlink() == foreign`.

Исправления поверх `37a6202` → checkpoint `b6dee9e`:
- `073e22f` — flaky SingleInstance lock Windows (placeholder write в loser guard; ~48% → 30/30).
- `b6dee9e` — symlink readlink `//?/` префикс (as_posix + removeprefix; no-op POSIX, assertion не ослаблен).

Локальная полная suite на `b6dee9e`: `4283 passed, 33 skipped, 4 xfailed, 0 failed`. **CI GREEN:** run [#43489547500](https://github.com/rpauts2/Tggruup/actions/runs/43489547500) на `a7e6de8` завершён — Linux `test` success, Windows success (`4301 passed, 15 skipped, 4 xfailed`); 18 symlink-случаев прошли на CI Windows, подтверждая `b6dee9e`.

## Что уже есть в UI

- Command Center и публикационная очередь.
- AI Team, Content Studio и Analytics.
- Editorial Planner: overview, execution, ожидающие брони, plan topics и revision history.
- Platform Center/Connections с раздельными статусами auth/API/destination/publish.

Старый утверждённый факт про наличие planner: не строить второй workspace. Следующие изменения должны расширять существующий контракт только если аудит покажет конкретный пробел.

## Следующие действия

1. Полная верификация текущего checkpoint `f4b9496` (+ queue `8069985`, docs) → push `github` → прочитать новый CI per-job → записать run. CI на `a7e6de8` (#43489547500) уже зелёный (Linux + Windows), подтверждает `b6dee9e`.
2. Исправить регрессии, если появятся, не переписывая UI/backend целиком.
3. При зелёном CI закрыть PR #60 и #56 как superseded, указав main commits — **только с подтверждением владельца**.
4. Техническая релизная приёмка offline: demo smoke, recovery-сценарии, autonomy chain report на temp DB, ui-visual-qa, Windows EXE артефакт + build identity.
5. Составить платформенную матрицу полевых испытаний; credentials вводятся локально, live публикации — только с отдельным согласием (OPS-1/OPS-2).
6. Пройти полный цикл S1–S12 на изолированных тестовых аккаунтах и проверить восстановление после остановки/перезапуска.
7. Проверить чистую установку Windows EXE и Linux server deployment/backup/restore.

## Ограничения

- Не менять production DB, секреты, LIVE mode и реальные публикации без отдельного подтверждения.
- Не повторять `publish_unknown` автоматически; сначала reconciliation.
- Не считать токен/сессию признаком publish-ready.
- Не считать offline smoke, мок, fixture или CI proof live-доступа.
- Не называть AI Team живым multi-agent execution без соответствующих run/traces.
- Не создавать новые ветки для каждой мелочи; пользователь предпочитает прямую работу в `main`.


## Последнее продолжение — 2026-11-13

Функциональный checkpoint: `b6dee9e4efb8a67c46b9d2dd5e6c1038a8e6ad59`.

Дополнительные коммиты после OAuth/S12 переноса:
- `389276e` — первые исправления CI: актуальные UI selectors, channel-owned Post fixtures, восстановление поведения worst_angle.
- `6aaa865` — helper `_drop_lesson` для удаления устаревшего worst_angle.
- `971bdae` — atomic Desktop lock, уникальная запись desktop.port, неизменяемый сохранённый OAuth callback port, Reddit API approval gate в UI и matrix, VK token detection; регрессионные тесты.
- `64cd0e5` — очистка S12 lessons, выпавших из 20-row window. Источник может удалять старые записи только при успешном чтении, при сбое сохранённая память остаётся.
- `65487de` — пользовательский: correct OAuth field selector и UI contract assertions.
- `490f21a` — ремонт трёх красных CI-контрактов command center: runs `limit=100`, list-safe `$$()` на connect-dialog/platform-credential, bounded retry `os.replace` в desktop.py.
- `cd600b6`/`2e2f109` → merge `b27ad52` — W36-B video provenance: disjoint rollback-бэкапы, per-destination restore с recovery; mutation-tested.
- `3705eb8`/`97a16e3`/`c602442` → merge `37a6202` — W36-C nullable rate contract: отсутствующая rate/quality остаётся None, не coerced в ноль; None исключён из denominators/baselines.
- `073e22f` — flaky SingleInstance lock Windows: placeholder write внесён в loser guard; PermissionError на byte 0 больше не крашит конкурентный старт (~48% → 30/30).
- `b6dee9e` — symlink readlink NT-префикс: тест-пост-условие W36-B сравнивает `as_posix()` со снятым `//?/`; чинит 18 падений CI Windows, скрытых локальными WinError 1314 skips.

Аудит всех пользовательских веток (4 read-only агента, content-level): уникального кода вне main не осталось; whole-branch merge запрещён (net-negative). Ничего повторно не портировалось. **Коррекция 2026-11-16:** повторный content-аудит нашёл один настоящий пробел — analytics CSS (W38-B `15c14c9`), который панель рендерит, но app.css не определял. После этого порта пробел закрыт.

CodeAnt замечания по PR #60 (4 пункта) и #56 (3 пункта) перенесены в исправления; новые regression tests покрывают эти контракты.

Текущий CI: run #43484978287 на `37a6202` завершён (Linux success, Windows failure — 18 symlink). Исправлено `073e22f` + `b6dee9e`. **CI GREEN:** run #43489547500 на `a7e6de8` завершён — Linux `test` success, Windows success (`4301 passed, 15 skipped, 4 xfailed`); 18 symlink-случаев прошли на CI Windows. Checkpoint `b6dee9e` зелёный. PR #60/#56 закрывать только с подтверждением владельца.

Открытый остаток: flaky lock и symlink-префикс ПОЧИНЕНЫ (см. выше). Локальный Windows-аккаунт без symlink privilege (WinError 1314) — 18 symlink-случаев локально SKIP, проверяются только на CI Windows; легитимный skip, но скрывает дефекты пост-условий от ревью. После зелёного CI закрыть PR #60/#56 как superseded (подтвердить перед закрытием).

## Последнее продолжение — 2026-11-16 (финальная волна к релизу)

Функциональный checkpoint: `f4b9496c6919584abb46860b95502186afedcb02`. Субагенты в этой сессии недоступны — исполнение и review-проходы последовательные, координатором; независимое субагентное ревью не выполнялось (см. [RELEASE_PLAN.md](RELEASE_PLAN.md)).

Коммиты этого захода (поверх `a7e6de8`, который уже зелёный на CI #43489547500):
- `8e31c30` — W38-A: operator-tunable дневные лимиты VK/Dzen (пользовательский WIP принят). `primary_platform_daily_cap`: explicit override → Settings (`PRIMARY_VK_DAILY_CAP`/`PRIMARY_DZEN_DAILY_CAP`, ge=1) → default {dzen:2, vk:3}; unknown = KeyError. 337 тестов контура, 8 новых.
- `15c14c9` — W38-B: analytics CSS (единственный настоящий пробел аудита веток). TDD RED 2 → GREEN 41; container-query + media fallback; 3 UI contract теста.
- `f4b9496` — W34-B: per-delivery preparation budget. Один deadline на доставку (scout/AI/rules/publish); AI-доля 0.5; timeout подготовки = failed/skipped, никогда unknown; отмена после claim → shielded failed-строка. TDD RED 4 → GREEN 20; контур 125 + 640 passed/4 xfailed/0 failed; ruff-паритет точный. Исходный отклонённый diff не интегрировался.
- `8069985` — реконсиляция очереди: W24-B FAILED→CANCELLED (superseded W24-BR), W34-B→INTEGRATED, карточки W38-A/W38-B. queue_check 49 tasks/0 problems; test_w25a_queue 43 passed.

CI: run #43489547500 на `a7e6de8` — **GREEN** (Linux test + Windows, 4301 passed/15 skipped/4 xfailed), подтверждает `b6dee9e`. Текущий локальный checkpoint `f4b9496` (+queue/docs) **ещё не запушен**: полный suite → push github → чтение нового CI per-job → запись run (задача 6 RELEASE_PLAN). Не объявлять новый checkpoint зелёным до чтения CI.

Следующее: задача 6 (верификация/push/CI), задача 7 (offline релизная приёмка), задача 8 (операторские гейты: подтверждение закрытия PR #60/#56, OPS-1/OPS-2 чек-лист, рекомендация по ротации токена, финальный отчёт). Live-публикации, production DB, секреты, LIVE mode — без отдельного подтверждения владельца не трогать.
