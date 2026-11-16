# TgFarm — полный план до релиза (от текущего момента)

> **Для исполнителей:** REQUIRED SUB-SKILL: superpowers:executing-plans. В этой сессии субагенты недоступны (типы Explore/Plan/general-purpose удалены из harness) — исполнение и review-проходы последовательные, координатором; независимое субагентное ревью не выполнялось, это явно помечается в отчёте. Шаги — checkbox (`- [ ]`).

**Goal:** от текущего checkpoint `0f1a993` (запушен в `github`) до полного релиза. Релиз = два РАЗНЫХ понятия: (1) **техническая готовность** — код + тесты + CI на обеих ОС + Windows EXE + offline-приёмка, закрывается координатором; (2) **live-готовность** — реальные OAuth/публикации/метрики на боевых аккаунтах, закрывается ВЛАДЕЛЬЦЕМ по OPS-гейтам. Зелёный CI ≠ live-приёмка.

**Architecture:** одна ветка `main`, прямые коммиты; каждый блок: diff → тесты → no-secrets → commit → push `github` → сверка remote SHA → НЕЗАВИСИМОЕ чтение CI per-job. Реестр — `docs/EXECUTION_PLAN.md`, машинное состояние — `docs/orchestration/queue.yaml`, передача — `docs/CONTINUATION_STATE.md`, разделение доказательств — `docs/READINESS_MATRIX.md`.

**Tech Stack:** Python 3.11 (target CI; локально 3.14), SQLAlchemy/SQLite, FastAPI, vanilla JS UI, pywebview/pystray, PyInstaller (CI Windows), pytest, playwright (visual QA).

**Spec:** `docs/AUTONOMY_ACCEPTANCE.md` (release gate), `docs/READINESS_MATRIX.md`, `docs/EXECUTION_PLAN.md`.

## Global Constraints

- Push только в `github`; `origin` — stale trap. Без force, без переписывания истории (§51).
- Запрещены `git reset --hard`, `git clean`, безусловный `git checkout --`; неизвестная работа сохраняется, не удаляется.
- Все локальные прогоны: `NO_PROXY= no_proxy= .venv/Scripts/python.exe -m pytest …` (env-артефакт `[::1]` ломает httpx 0.28.1).
- Секреты не в Git/логах/UI (§44); токен в логе — только рекомендация ротации оператору (§9), ротацию НЕ выполнять.
- Никаких live-действий без отдельного согласия владельца: production-БД, LIVE mode, реальные публикации, ротация токенов, перезапуск serve, ввод OAuth-секретов (§0.3, §47, §80).
- Offline/CI ≠ live-приёмка. Неизвестно ≠ ноль. `publish_unknown` не повторять вслепую.
- TDD: красный тест до кода; assertions не ослаблять; xfail не прятать. FP-epsilon на верхней границе бюджета — это гигиена сравнения, НЕ ослабление контракта (нижняя граница и отсечение реального переполнения сохраняются).
- ruff: line-length 110, target py311, E/F/I/UP/B, ignore B008. CRLF working copies / LF blobs.
- Консоль cp1252: не печатать кириллицу из python-скриптов (писать в UTF-8 файл / PYTHONIOENCODING=utf-8).
- 18 symlink-тестов локально SKIP (WinError 1314) — выполняются только на CI Windows; легитимный skip, пост-условия ревьюить вручную.
- Закрытие PR #60/#56 (общее состояние GitHub) — ТОЛЬКО с явным подтверждением владельца.

## Фактическая база (замер 2026-11-16, HEAD `0f1a993`)

- local `main` = `github/main` = **`0f1a993ff72305cf8a352e46ead160eaf28ba274`** (push сверен).
- Цепочка этого захода (поверх зелёного `a7e6de8`): `8e31c30` W38-A daily-cap → `15c14c9` W38-B analytics CSS → `f4b9496` W34-B per-delivery budget → `8069985` queue → `fd7c3a0` docs → `a1cefbc` visual connect-pill fix → `6bf00ab` visual keyboard fix → `0f1a993` FP batch-bound fix.
- **Локальный полный suite на `0f1a993`: `4298 passed, 33 skipped, 4 xfailed, 0 failed`** (289 с, exit 0).
- **CI run [#43788431649](https://github.com/rpauts2/Tggruup/actions/runs/43788431649) на `0f1a993`: Linux `test` — SUCCESS; Windows — FAILURE** (шаг «Run test suite»). Windows EXE/build-identity шаги SKIPPED (гейтятся на suite).
- **Visual QA run [#43788547309](https://github.com/rpauts2/Tggruup/actions/runs/43788547309) на `0f1a993`: SUCCESS** (`browser-visual-smoke`, независимо подтверждён).
- Единственный блокер технической готовности: **FP-класс верхних границ бюджета** в `tests/test_crosspost_publish_deadline.py`. CI Windows py3.11 вычисляет доли как `150.00000000000006` / `450.00000000000006` (IEEE-754 шум ~6e-14), строгие `<= N` падают. `0f1a993` закрыл 3 из 5 границ (384/385/388); **остались 149 (`140 < budget <= 150`) и 200 (`55 < budget <= 60`)**. Локально py3.14 даёт точные значения, поэтому локальный suite зелёный, а CI Windows — красный.
- Offline-приёмка (Task 7 шаги 1–4) выполнена на `6bf00ab`/`0f1a993`: demo smoke ok (throwaway store, published 0), recovery-сценарии покрыты (12 сценариев → тесты), chain report на temp DB честный (absent/unknown ≠ measured-zero), visual QA green. UI-дерево `0f1a993` == `6bf00ab` (diff пуст).
- Аудит 17 веток github завершён: всё merged/ported/superseded; единственный пробел (analytics CSS) закрыт W38-B. Ветки не удалять.
- Операторский чек-лист готов: `.qoder/operator_checklist.md` (OPS-1/OPS-2/OAuth/контролируемые публикации/emergency stop/ротация токена/PR).

## Уже закрыто (ledger, с SHA и evidence)

- [x] W38-A `8e31c30` — operator-tunable дневные лимиты VK/Dzen; `primary_platform_daily_cap`; 337 тестов контура, 8 новых.
- [x] W38-B `15c14c9` — analytics CSS (TDD RED 2 → GREEN 41); container-query + media fallback; 3 UI contract теста.
- [x] W34-B `f4b9496` — per-delivery preparation budget; timeout подготовки = failed/skipped, никогда unknown; TDD RED 4 → GREEN; контур 125 + 640 passed.
- [x] Queue `8069985` — W24-B CANCELLED (superseded), W34-B INTEGRATED, W38-A/B; queue_check 49 tasks/0 problems.
- [x] Visual-QA stale fixes `a1cefbc` (connect-pill copy) + `6bf00ab` (keyboard walk со studio tab) — оба pre-existing drift, не регрессии волны; visual QA green.
- [x] FP batch-bound fix `0f1a993` — epsilon 1e-6 на границах 384/385/388 (частично; класс закрыт не полностью).
- [x] Offline-приёмка шаги 1–4: demo / recovery coverage / chain report / visual QA.

---

### Task A: Закрыть FP-класс верхних границ ЦЕЛИКОМ → зелёный CI на обеих ОС

**Files:**
- Modify: `tests/test_crosspost_publish_deadline.py` (строки 149, 200 — оставшиеся границы; 384/385/388 уже в `0f1a993`).

**Корень (не симптом):** все range-assertions бюджета, введённые W34-B/W36-A, сравнивают вычисленную долю с номинальным целым через строгий `<= N`. На CI Windows py3.11 доля накапливает IEEE-754 шум ~6e-14 поверх номинала. Фикс — один проход: epsilon 1e-6 на ВСЕХ верхних границах класса, нижние границы не трогать.

- [ ] Шаг 1: failing evidence уже есть — CI Windows `assert 150.00000000000006 <= 150` (строка 149). Добавить `+ 1e-6` к верхним границам строк 149 (`140 < budget <= 150 + 1e-6`) и 200 (`55 < budget <= 60 + 1e-6`); комментарий о FP-шуме уже есть у batch-границ, сослаться на тот же класс.
- [ ] Шаг 2: локально `NO_PROXY= no_proxy= .venv/Scripts/python.exe -m pytest tests/test_crosspost_publish_deadline.py -q` (16 passed) + арифметическая проверка: epsilon поглощает `150.00000000000006`/`450.00000000000006`, но отсекает реальное переполнение (151/451); нижние границы сохранены.
- [ ] Шаг 3: `ruff check tests/test_crosspost_publish_deadline.py` — clean; no-secrets по diff чист; миграций нет.
- [ ] Шаг 4: NEW commit (не amend) `test(crosspost): absorb IEEE-754 noise across all publish-budget upper bounds`; push `github main`; сверить `git rev-parse github/main` == HEAD.
- [ ] Шаг 5: `gh run watch <id> --exit-status` (фон) + **НЕЗАВИСИМО** `gh run view <id> --json status,conclusion,headSha,jobs` per-job. Ожидание: Linux `test` success, Windows success (`4316 passed, 15 skipped, 4 xfailed` = 4298 локальных + 18 symlink).
- [ ] Шаг 6: подтвердить Windows шаги «Stamp Windows build identity» / «Build Windows desktop application» / «Verify Windows executable» / «Verify Windows build identity travels with the bundle» / «Upload Windows build artifact» — все success (не skipped).

### Task B: Записать зелёный CI и checkpoint в реестр/передачу/матрицу

**Files:**
- Modify: `docs/EXECUTION_PLAN.md` (§6 строка 103, §11 «CI и checkpoint» 204–205), `docs/CONTINUATION_STATE.md` (новый блок продолжения), `docs/READINESS_MATRIX.md` (offline-acceptance секция), `docs/RELEASE_PLAN.md` (чекбоксы по факту).

- [ ] Шаг 1: записать run/SHA/счётчики: CI `<id>` на `<sha>` — Linux success, Windows success (4316/15/4); visual QA #43788547309 success; локальный suite 4298/33/4. Checkpoint `<sha>` зелёный на обеих ОС.
- [ ] Шаг 2: READINESS_MATRIX — добавить секцию offline-приёмки нового checkpoint (техническая готовность CLOSED); **live-колонки остаются UNKNOWN/DOCUMENTED_ONLY** до операторских проверок; не переписывать live-наблюдения.
- [ ] Шаг 3: commit `docs: record green CI on both OSes and close technical release readiness`; push `github`; сверка remote SHA.

### Task C: Финализировать offline-приёмку на зелёном SHA

- [ ] Шаг 1: скачать CI artifact `TgFarm-Windows-<sha>`; проверить наличие EXE и build identity (без запуска live-фермы).
- [ ] Шаг 2: подтвердить demo smoke / recovery coverage / chain report / visual QA evidence на финальном SHA (UI-дерево не менялось с `6bf00ab`; visual QA #43788547309 уже green на `0f1a993`).
- [ ] Шаг 3: зафиксировать в EXECUTION_PLAN/READINESS_MATRIX: техническая готовность закрыта; что осталось владельцу.

### Task D: Операторские гейты (BLOCKED — выполняет ВЛАДЕЛЕЦ; координатор готовит чек-лист)

- [ ] Шаг 1: спросить владельца о закрытии PR #60/#56 как superseded (AskUserQuestion; общее состояние GitHub — только с подтверждением). Ветки не удалять.
- [ ] Шаг 2: чек-лист live-приёмки (`.qoder/operator_checklist.md`): OPS-1 перезапуск serve с `PLAN_EXECUTION_ENABLED=true` в окне между publish_tick; OPS-2 `VK_USER_TOKEN` + согласие на онлайн-LLM; OAuth Client ID/Secret локально (не в чат); по одной контролируемой публикации на тестовый аккаунт (§47 — отдельное согласие на каждый тест); reconcile remote ID/URL; полный S1–S12 на тестовых аккаунтах; метрики coverage.
- [ ] Шаг 3: ротация токена, уже попавшего в лог — рекомендация оператору (факт зафиксирован; ротацию НЕ выполнять).
- [ ] Шаг 4: emergency stop / `publish_unknown` reconcile — подтвердить инварианты владельцу.

### Task E: Финальный русский отчёт

- [ ] Шаг 1: что закрыто кодом/CI/offline (точные SHA/run/счётчики), что осталось владельцу (OPS-гейты), разделение технической и live-готовности.
- [ ] Шаг 2: определения «full release»: техническая — CLOSED координатором; live — CLOSED владельцем по OPS-гейтам. Не объявлять live-готовность без evidence владельца.

## Self-review

- Покрытие spec: safety/recovery → Task C + READINESS_MATRIX; build identity/EXE → Task A шаг 6 + Task C шаг 1; platform acceptance → Task D (owner); autonomy loop → chain report + READINESS_MATRIX; очередь/реестр → Task B.
- Placeholder scan: конкретные команды/строки приведены; `<id>`/`<sha>` заполняются фактическими значениями после зелёного CI (не выдумываются заранее).
- Корень блокера назван целиком (FP-класс, 5 границ), не симптом-за-симптомом; Task A закрывает класс одним проходом.
- Типы/имена: `_publish_budget_seconds` ContextVar, `primary_platform_daily_cap` — существующие контракты.