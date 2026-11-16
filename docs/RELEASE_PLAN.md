# TgFarm — финальный план до полного релиза

> **Для исполнителей:** REQUIRED SUB-SKILL: superpowers:executing-plans (в этой сессии субагенты недоступны — типы Explore/Plan/general-purpose удалены из harness; выполнение последовательное, независимое ревью заменяется отдельным review-проходом координатора с явной пометкой в отчёте). Шаги — checkbox (`- [ ]`).

**Goal:** от текущего checkpoint (`4881bac`, CI зелёный на `a7e6de8`) до полного релиза: техническая готовность (код, тесты, CI, Windows EXE, offline-приёмка) закрыта в этом цикле; live-приёмка выполнена владельцем по явному чек-листу (OPS-гейты).

**Architecture:** одна ветка `main`, прямые коммиты по правилу владельца; каждый блок: diff → тесты → no-secrets → commit → push `github` → сверка remote SHA → чтение CI. Реестр — `docs/EXECUTION_PLAN.md`, машинное состояние — `docs/orchestration/queue.yaml`, передача — `docs/CONTINUATION_STATE.md`.

**Tech Stack:** Python 3.11 (target; локально 3.14), SQLAlchemy/SQLite, FastAPI, vanilla JS UI, pywebview/pystray, PyInstaller (CI Windows), pytest.

**Spec:** `docs/AUTONOMY_ACCEPTANCE.md` (release gate), `docs/READINESS_MATRIX.md`, `docs/EXECUTION_PLAN.md`.

## Global Constraints

- Push только в `github`; `origin` — stale trap. Без force, без переписывания истории (§51).
- Запрещены `git reset --hard`, `git clean`, безусловный `git checkout --`; неизвестная работа сохраняется, не удаляется.
- Все локальные прогоны: `NO_PROXY= no_proxy= .venv/Scripts/python.exe -m pytest …` (env-артефакт `[::1]` ломает httpx 0.28.1).
- Секреты не в Git/логах/UI (§44); токен в логе — только рекомендация ротации оператору (§9).
- Никаких live-действий без отдельного согласия владельца: production-БД, LIVE mode, реальные публикации, ротация токенов, перезапуск serve (§0.3, §47, §80).
- Offline/CI ≠ live-приёмка. Неизвестно ≠ ноль. `publish_unknown` не повторять вслепую.
- TDD: красный тест до кода; assertions не ослаблять; xfail не прятать.
- ruff: line-length 110, target py311, E/F/I/UP/B, ignore B008. CRLF working copies / LF blobs.
- Консоль cp1252: не печатать кириллицу из python-скриптов (писать в UTF-8 файл).
- 18 symlink-тестов локально SKIP (WinError 1314) — проверяются только на CI Windows; это легитимный skip, но ревью пост-условий делать вручную.

## Фактическая база (замер 2026-11-16)

- local `main` = `4881bac` (2 queue-chore коммита впереди `github/main` = `a7e6de8`).
- CI run #43489547500 на `a7e6de8`: **success** (Linux test + Windows, EXE собран, build identity проверена). Windows: 4301 passed / 15 skipped / 4 xfailed — 18 symlink-случаев выполнены и прошли.
- Незакоммиченная работа владельца в дереве: настраиваемые дневные лимиты `PRIMARY_VK_DAILY_CAP`/`PRIMARY_DZEN_DAILY_CAP` (settings + s7 reader + distribution + plan_execution + conftest + .env.example + новый тест, 8/8 passed локально).
- Подтверждённый пробел: `app.js:7713/7717` рендерит `.analytics-format-row`/`.analytics-evidence-note`, в `app.css` их стилей нет (0 совпадений; pickaxe: main никогда их не имел). CSS-блок есть в ветке `feat/tgfarm-command-center-auth-reviewed-20261110`.
- Очередь: 47 карточек; открыты W24-B (FAILED — устарела, артефакты приняты как W24-BR INTEGRATED), W34-B (RECOVERY_REQUIRED — остаток: batch/preparation/DB бюджет; узкий publish-await принят как W36-A `32d40ab`), OPS-1/OPS-2 (BLOCKED — оператор).
- Аудит 17 веток github завершён: всё merged/ported/superseded, кроме analytics CSS. `chore/reconcile-project-roadmap-20261113` НЕ вливать (stale doc deletion). Ветки не удалять.

---

### Task 1: Верифицировать и принять daily-cap WIP владельца

**Files:**
- Modify (уже изменено владельцем): `.env.example`, `tests/conftest.py`, `tgfarm/core/settings.py`, `tgfarm/distribution.py`, `tgfarm/plan_execution.py`, `tgfarm/s7_publish/__init__.py`
- Test: `tests/test_primary_platform_daily_cap_setting.py` (новый, 8 тестов)
- Проверить seam: `tgfarm/plan_execution.py:800` — второй потребитель `PRIMARY_PLATFORM_DAILY_CAPS`; если читает значение из dict напрямую, env-override там не работает → перевести на `primary_platform_daily_cap(platform)` тем же контрактом.

**Interfaces:**
- Produces: `tgfarm.s7_publish.primary_platform_daily_cap(platform: str) -> int` — приоритет: явное значение в `PRIMARY_PLATFORM_DAILY_CAPS` (≠ кодовому default) → Settings `primary_vk_daily_cap`/`primary_dzen_daily_cap` → default {dzen:2, vk:3}. KeyError для неизвестной платформы.

- [x] Шаг 1: прочитать `plan_execution.py:790-810`, определить, читает ли второй потребитель значение dict; если да — failing test (env override меняет и этот путь), затем минимальный фикс. (Проверено: второй потребитель ~800 — membership-only counter, seam не тронут.)
- [x] Шаг 2: review-проход diff владельца: три источника приоритета, lazy import в distribution (цикл s7↔distribution), `ge=1` в Settings, conftest DETERMINISTIC_TUNABLES. Nit: в `TestUnknownPlatform` имя/комментарий говорят «returns default», а assert — `KeyError`; переименовать тест и поправить комментарий (assertion НЕ менять). (Nit исправлен.)
- [x] Шаг 3: прогоны: 337 тестов затронутого контура зелёные; 8 новых тестов.
- [x] Шаг 4: ruff на изменённых файлах (2 новых I001 пофиксены, parity сохранён); no-secrets grep по diff чист.
- [x] Шаг 5: commit `8e31c30` `feat(s7): operator-tunable primary platform daily caps`.

### Task 2: Порт analytics CSS (единственный подтверждё пробел аудита веток)

**Files:**
- Modify: `tgfarm/ui/app.css` (после `.analytics-detail-row`, ~строка 2706)
- Test: `tests/test_ui_analytics_format_styles.py` (новый)

**Interfaces:**
- Consumes: классы, которые рендерит `app.js:7713/7717`: `analytics-format-row`, `analytics-evidence-note`; контейнер `analytics-table`.
- Produces: стили + `container-type:inline-size` на `.analytics-table`, container query ≤560px и media query ≤700px.

- [x] Шаг 1: failing test по образцу `tests/test_ui_coverage_panel.py`: GET `/ui/app.css`, assert `.analytics-format-row` и `.analytics-evidence-note` определены; assert `app.js` рендерит эти классы; assert `container-type:inline-size` у `.analytics-table`. (3 теста.)
- [x] Шаг 2: прогон — RED 2 failed (классов нет в css).
- [x] Шаг 3: добавить блок (дословно из ветки, проверенный аудитором):

```css
.analytics-table { container-type:inline-size; }
.analytics-format-row { grid-template-columns:minmax(0,1fr) minmax(0,1.2fr) minmax(0,1fr) minmax(0,1fr); }
.analytics-format-row > * { min-width:0; overflow-wrap:anywhere; }
.analytics-format-row small { color:var(--text-faint); font-size:10px; text-align:right; line-height:1.4; }
@container (max-width:560px) {
  .analytics-format-row { grid-template-columns:minmax(0,1fr) minmax(0,1fr); gap:8px 12px; }
  .analytics-format-row > * { min-width:0; overflow-wrap:anywhere; }
  .analytics-format-row b { text-align:left; }
  .analytics-format-row small { text-align:right; }
}
.analytics-evidence-note { margin:12px 0 3px; padding:10px 12px; border:1px solid var(--line-soft); border-radius:9px; color:var(--text-dim); font-size:10.5px; line-height:1.5; background:rgba(0,0,0,.12); }
@media (max-width:700px) { .analytics-format-row { grid-template-columns:1fr 1fr; } .analytics-format-row small { text-align:left; } }
```

- [x] Шаг 4: прогон — GREEN 41 passed; существующий `.analytics-row` grid не сломан (новый селектор уточняет колонки только для format-row).
- [x] Шаг 5: commit `15c14c9` `fix(ui): style the analytics format rows and evidence note the panel already renders`.

### Task 3: W34-B остаток — бюджет подготовки доставки crosspost

**Files:**
- Modify: `tgfarm/s9_growth/__init__.py` (цикл `_deliver_crossposts`: адаптация/choose_native_format/trace-запись/reddit preflight ДО `_publish_with_deadline` сейчас без per-delivery deadline)
- Test: `tests/test_crosspost_delivery_budget.py` (сохранённый исходный тест из worktree w34b — НЕ интегрировать слепо; исходный diff отклонён ревью: unbounded rendering/DB, гонки отмены claim/log, неверный unknown при подготовке)

**Interfaces:**
- Consumes: `_publish_budget_seconds` ContextVar (W36-A `32d40ab`), `asyncio.wait({task}, timeout=…)` паттерн без гонки поздних terminal writers.
- Produces: per-delivery бюджет, покрывающий подготовку; таймаут подготовки (внешнего запроса НЕ было) → журнальный `failed`/`skipped` с причиной и retry_after, НЕ `unknown`; отмена не конфликтует с поздним writer'ом; batch не превышает outer cycle budget.

- [x] Шаг 1: исследовать текущий цикл и сохранённый diff в `.qoder/worktrees/w34b-delivery-budget`; минимальный безопасный инкремент — один per-delivery deadline поверх W36-A паттерна. (Исходный отклонённый diff НЕ интегрировался.)
- [x] Шаг 2: failing tests (4): AI-адаптация timeout → детерминированный fallback + posted; бюджет исчерпан подготовкой → failed без adapter; rules recheck timeout → skipped без submit; отмена подготовки → shielded failed-строка разрешает claim. RED 4 failed.
- [x] Шаг 3: минимальная реализация; прогон новых + `tests/test_crosspost_publish_deadline.py` (20 passed) + широкий s9 контур (125 + 640 passed/4 xfailed/0 failed).
- [x] Шаг 4: review-проход (отдельным чтением diff): claim после scout, rendering до claim, короткие DB-write вне long-await; dry-run и publish-unknown не тронуты; слепого повтора нет. Ruff-паритет точный; no-secrets чист.
- [x] Шаг 5: commit `f4b9496` `fix(crosspost): bound per-delivery preparation under the publish budget`.

### Task 4: Реконсиляция очереди

**Files:**
- Modify: `docs/orchestration/queue.yaml`

- [x] Шаг 1: W24-B FAILED → CANCELLED (superseded W24-BR INTEGRATED; evidence: `tests/test_w24b_buildinfo.py` в main, CI stamp/verify build identity steps зелёные на #43489547500).
- [x] Шаг 2: W34-B → INTEGRATED с SHA `f4b9496` и замерами (TDD/контур/ruff); исходный diff не интегрировался.
- [x] Шаг 3: новые карточки W38-A (daily-cap, `8e31c30`) и W38-B (analytics CSS, `15c14c9`) с evidence и test_commands.
- [x] Шаг 4: `python tools/queue_check.py` — 49 tasks, 0 problems; `pytest -q tests/test_w25a_queue.py` — 43 passed.
- [x] Шаг 5: commit `8069985` `chore(queue): reconcile W24-B/W34-B and register the W38 wave`.

### Task 5: Реестр и checkpoint-документы → GREEN

**Files:**
- Modify: `docs/EXECUTION_PLAN.md` (§6 строка 102, §10 строка 173 — «не объявляется зелёным» → факт GREEN; новый §11: волна W38 + аудит веток + CSS-пробел), `docs/CONTINUATION_STATE.md` (строки 27/40/77 + новый блок продолжения), `docs/RELEASE_PLAN.md` (чекбоксы по факту).

- [x] Шаг 1: записать CI-факт: run #43489547500 на `a7e6de8` — Linux test success, Windows success (4301 passed, 15 skipped, 4 xfailed); 18 symlink-случаев выполнены и прошли (4283 локальных + 18 = 4301) — `b6dee9e` подтверждён CI Windows. (EXECUTION_PLAN §6/§10/§11, CONTINUATION_STATE.)
- [x] Шаг 2: записать вывод аудита 17 веток (всё superseded/ported; единственный пробел — analytics CSS, закрыт Task 2; chore/reconcile сохранён, не влит). Коррекция прежнего вывода «уникального кода нет» внесена.
- [ ] Шаг 3: новый checkpoint SHA после Task 6 push; обновить дату среза. (Дата среза 2026-11-16 и checkpoint `f4b9496` уже записаны; финальный SHA после push — в Task 6.)
- [x] Шаг 4: commit `docs: record green CI, branch-audit closure and the W38 wave`.

### Task 6: Полная верификация → push → CI

- [ ] Шаг 1: полная suite: `NO_PROXY= no_proxy= .venv/Scripts/python.exe -m pytest -q` (ожидание: ≥4291 passed, 0 failed; 33 skipped локально).
- [ ] Шаг 2: `ruff check tgfarm tests tools` — parity с main (0 новых ошибок); `python tools/queue_check.py`.
- [ ] Шаг 3: no-secrets gate по всем изменённым файлам волны; миграций нет.
- [ ] Шаг 4: `git push github main` (fast-forward, без force); сверить `git rev-parse github/main` == local HEAD.
- [ ] Шаг 5: `gh run watch <id> --exit-status` (без pipe) + НЕЗАВИСИМО `gh run view <id> --json status,conclusion` и per-job conclusion; Windows EXE artifact + build identity steps.
- [ ] Шаг 6: записать run/SHA/счётчики в EXECUTION_PLAN/CONTINUATION_STATE; commit + push docs; сверка remote SHA.

### Task 7: Техническая релизная приёмка (offline, без live)

- [ ] Шаг 1: `tgfarm demo --cycles 1` на throwaway store (offline smoke; CI также гоняет smoke на обеих ОС).
- [ ] Шаг 2: recovery-сценарии `docs/AUTONOMY_ACCEPTANCE.md` — подтвердить покрытие существующими тестами (restart/crash/timeout/429/permanent/backup/duplicate/heartbeat); пробелы — failing test → фикс, либо честная строка «не покрыто» в матрице.
- [ ] Шаг 3: `python tools/autonomy_chain_report.py` против изолированной temp DB (не против живой `tgfarm.db` без mode=ro).
- [ ] Шаг 4: UI visual QA workflow (`ui-visual-qa.yml`) — запустить/прочитать результат на новом SHA.
- [ ] Шаг 5: Windows EXE: скачать artifact CI `TgFarm-Windows-<sha>`, проверить наличие и build identity (без запуска live-фермы).
- [ ] Шаг 6: зафиксировать в READINESS_MATRIX/EXECUTION_PLAN: техническая готовность закрыта; live-колонки остаются UNKNOWN/DOCUMENTED_ONLY до операторских проверок.

### Task 8: Операторские гейты (BLOCKED — выполняет владелец; координатор готовит чек-лист)

- [ ] Шаг 1: спросить владельца о закрытии PR #60/#56 как superseded (общее состояние GitHub — только с подтверждением).
- [ ] Шаг 2: чек-лист live-приёмки для владельца (в финальном отчёте): OPS-1 перезапуск serve с `PLAN_EXECUTION_ENABLED=true` в окне между publish_tick; OPS-2 `VK_USER_TOKEN` + согласие на онлайн-LLM; OAuth Client ID/Secret по площадкам (вводить локально, не в чат); по одной контролируемой публикации на тестовый аккаунт/назначение (§47 — отдельное согласие на каждый тест); reconcile remote ID/URL; полный S1–S12 цикл на тестовых аккаунтах; метрики coverage.
- [ ] Шаг 3: ротация токена, уже попавшего в лог — рекомендация оператору (факт зафиксирован ранее; ротацию НЕ выполнять).
- [ ] Шаг 4: финальный русский отчёт: что закрыто кодом/CI, что осталось владельцу, точные SHA/run/счётчики.

## Self-review

- Покрытие spec: safety invariants/recovery → Task 7; platform acceptance → Task 8 (owner); autonomy loop → Task 7 шаг 3 + READINESS_MATRIX; build identity/EXE → Task 6 шаг 5 + Task 7 шаг 5; очередь/реестр → Tasks 4-5.
- Placeholder scan: конкретные команды/код приведены; Task 3 шаг 1 — исследование, т.к. исходный diff отклонён и дизайн зависит от текущего цикла (acceptance-тесты заданы явно).
- Типы/имена: `primary_platform_daily_cap` согласован с WIP владельца; `_publish_budget_seconds` — существующий ContextVar.