# TgFarm — continuation checkpoint

> Этот файл — краткая передача следующему инженерному циклу. Единственный подробный реестр статуса: [EXECUTION_PLAN.md](EXECUTION_PLAN.md). Для всех требований см. [MASTER_REQUIREMENTS_TRACEABILITY.md](MASTER_REQUIREMENTS_TRACEABILITY.md).

**Дата:** 2026-11-13  
**Основная ветка:** `main`  
**Последний функциональный checkpoint:** `64cd0e515c5e9bd5fa815b129e4e273e3f5ec9a0`

## Что перенесено в main в этом заходе

- `e61cd3c` — backend Desktop OAuth, стабильный loopback listener, system browser, LinkedIn/TikTok/YouTube PKCE, корректный YouTube reconnect, платформенные credential flows и регрессионные тесты.
- `c81ac6a` — UI для Client ID/Secret, official docs/Redirect URI, system browser в PyWebView, Dzen flow completion, четыре независимых proof статуса платформы и восемь Desktop/OAuth тестов.
- `4ca2570` — S12 failure memory: ошибки pipeline и crosspost агрегируются отдельно, crosspost связан с конкретным каналом, secret redaction, повторное чтение идемпотентно, permanent rejection включён, сбой чтения одного источника не уничтожает данные другого.

Открытые PR #60 и #56 содержат теперь уже перенесённые изменения и должны быть закрыты как superseded после успешного CI. Не пытаться повторно вливать старые ветки.

## Текущее CI

CI run [#43441301090](https://github.com/rpauts2/Tggruup/actions/runs/43441301090) запущен на `4ca2570e`.

На последнем просмотре:
- Linux: Python compile, JS syntax и platform delivery regression checks прошли; полный test suite выполнялся.
- Windows: установка зависимостей выполнялась; затем запланированы compile, JS syntax, тесты, offline smoke и сборка EXE.
- Итогового результата ещё нет. Не объявлять этот commit прошедшим CI, пока workflow не завершится.

## Что уже есть в UI

- Command Center и публикационная очередь.
- AI Team, Content Studio и Analytics.
- Editorial Planner: overview, execution, ожидающие брони, plan topics и revision history.
- Platform Center/Connections с раздельными статусами auth/API/destination/publish.

Старый утверждённый факт про наличие planner: не строить второй workspace. Следующие изменения должны расширять существующий контракт только если аудит покажет конкретный пробел.

## Следующие действия

1. Проверить завершение CI run #43441301090 и все неуспешные шаги.
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

Функциональный checkpoint: `64cd0e515c5e9bd5fa815b129e4e273e3f5ec9a0`.

Дополнительные коммиты после OAuth/S12 переноса:
- `389276e` — первые исправления CI: актуальные UI selectors, channel-owned Post fixtures, восстановление поведения worst_angle.
- `6aaa865` — helper `_drop_lesson` для удаления устаревшего worst_angle.
- `971bdae` — atomic Desktop lock, уникальная запись desktop.port, неизменяемый сохранённый OAuth callback port, Reddit API approval gate в UI и matrix, VK token detection; регрессионные тесты.
- `64cd0e5` — очистка S12 lessons, выпавших из 20-row window. Источник может удалять старые записи только при успешном чтении, при сбое сохранённая память остаётся.

CodeAnt замечания по PR #60 (4 пункта) и #56 (3 пункта) перенесены в исправления; новые regression tests покрывают эти контракты.

Текущий CI: [run #43445803450](https://github.com/rpauts2/Tggruup/actions/runs/43445803450). Полная suite и Windows build ещё должны завершиться. Не закрывать PR #60/#56 до зелёного CI.
