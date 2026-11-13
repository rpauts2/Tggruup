# TgFarm — continuation checkpoint

> Этот файл — краткая передача следующему инженерному циклу. Единственный подробный реестр статуса: [EXECUTION_PLAN.md](EXECUTION_PLAN.md). Для всех требований см. [MASTER_REQUIREMENTS_TRACEABILITY.md](MASTER_REQUIREMENTS_TRACEABILITY.md).

**Дата:** 2026-11-13  
**Основная ветка:** `main`  
**Последний функциональный checkpoint:** `b6dee9e4efb8a67c46b9d2dd5e6c1038a8e6ad59`

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

Локальная полная suite на `b6dee9e`: `4283 passed, 33 skipped, 4 xfailed, 0 failed`. Новый CI run запускается после push; не объявлять checkpoint зелёным, пока workflow не завершится и результат не будет прочитан.

## Что уже есть в UI

- Command Center и публикационная очередь.
- AI Team, Content Studio и Analytics.
- Editorial Planner: overview, execution, ожидающие брони, plan topics и revision history.
- Platform Center/Connections с раздельными статусами auth/API/destination/publish.

Старый утверждённый факт про наличие planner: не строить второй workspace. Следующие изменения должны расширять существующий контракт только если аудит покажет конкретный пробел.

## Следующие действия

1. Проверить завершение нового CI run на checkpoint `b6dee9e` (после push) и все неуспешные шаги; run #43484978287 на `37a6202` уже завершён (Windows 18 symlink) и исправлен `073e22f`/`b6dee9e`.
2. Исправить регрессии, если появятся, не переписывая UI/backend целиком.
3. При зелёном CI закрыть PR #60 и #56 как superseded, указав main commits.
4. Составить платформенную матрицу полевых испытаний; credentials вводятся локально, live публикации — только с отдельным согласием.
5. Пройти полный цикл S1–S12 на изолированных тестовых аккаунтах и проверить восстановление после остановки/перезапуска.
6. Проверить чистую установку Windows EXE и Linux server deployment/backup/restore.

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

Аудит всех пользовательских веток (4 read-only агента, content-level): уникального кода вне main не осталось; whole-branch merge запрещён (net-negative). Ничего повторно не портировалось.

CodeAnt замечания по PR #60 (4 пункта) и #56 (3 пункта) перенесены в исправления; новые regression tests покрывают эти контракты.

Текущий CI: run #43484978287 на `37a6202` завершён (Linux success, Windows failure — 18 symlink). Исправлено `073e22f` + `b6dee9e`; новый run запускается после push `b6dee9e`. Не закрывать PR #60/#56 до зелёного CI.

Открытый остаток: flaky lock и symlink-префикс ПОЧИНЕНЫ (см. выше). Локальный Windows-аккаунт без symlink privilege (WinError 1314) — 18 symlink-случаев локально SKIP, проверяются только на CI Windows; легитимный skip, но скрывает дефекты пост-условий от ревью. После зелёного CI закрыть PR #60/#56 как superseded (подтвердить перед закрытием).
