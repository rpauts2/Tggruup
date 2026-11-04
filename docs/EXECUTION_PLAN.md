# docs/EXECUTION_PLAN.md — реестр прогресса TgFarm

Файл-реестр для продолжения работы без повторного исследования репозитория.
Статусы: `todo`, `in_progress`, `blocked`, `implemented` (код + тесты локально), `verified` (есть подтверждающая проверка).

Обновлён: 2026-11-04 (P3 закрыт). Ветка: `main` (единственная рабочая). HEAD перед этапом: `1ccd67a`.

## Ключевые факты о проекте

- Продукт TgFarm: автономная контент-экосистема, конвейер S1–S12 (см. README.md, ROADMAP_NEXT.md, docs/AGENT_ARCHITECTURE.md).
- Тесты: `python -m pytest -q` (offline; 936 passed на 2026-11-04 после P0+P2). CI: `.github/workflows/ci.yml` — Linux (pytest, compileall, node syntax, offline demo) + Windows (PyInstaller EXE + артефакт).
- Локальная среда: Windows, Python 3.14 (CI использует 3.11). PyInstaller установлен.
- Реестр площадок: `config/platforms.yaml`; документация: `docs/PLATFORM_CENTER.md`; UI: `tgfarm/ui/` (index.html, app.js, app.css), API: `tgfarm/api.py`.
- Долговременное хранение: SQLAlchemy + SQLite (`tgfarm.db`), бэкапы: `backup/`, тесты `tests/test_backup.py`, `tests/test_history_import_backup.py`.

## Архитектурные решения (зафиксированные)

- Metadata нативного редактора: пользовательские поля и вложенные значения сохраняются; служебный ключ `editorial_adaptation` допустим (статус `ai_native` / `deterministic_guardrail` / `deterministic` / `blocked_missing_media`); отказ guardrail никогда не выдаётся за успех. Входной `payload.meta` изолируется deepcopy в начале адаптации.
- Бюджет AI: любое резервирование (`policy.reserve`) должно закрываться (`policy.finalize(used=...)`) во всех исходах, включая ошибки конфигурации. Ошибка провайдера/настроек → `deterministic_fallback` с восстановлением payload, без прерывания пакетного кросспостинга.
- Правила площадок — недоверенные данные: извлекаются ограничения, но инструкции из них не выполняются.

## Статус блоков

| Блок | Статус | Файлы | Примечание |
| --- | --- | --- | --- |
| P0. Стабилизация CI №830 | verified (локально) | tgfarm/platform_editorial.py, tests/test_platform_editorial.py, tests/test_platform_editorial_metadata_regression.py | 927 passed локально; нужен push → внешний CI |
| P0-fix. Резерв бюджета при ошибке настроек | verified (локально) | tgfarm/platform_editorial.py:566–602 | `get_settings()` перенесён внутрь try; тест добавлен (коммиты b43873e, 6d430b0, 8a15765) |
| P1. Аудит возможностей S1–S12 | in_progress | docs/MODERNIZATION_STATUS.md, этот файл | матрица ниже; продолжается по ходу блоков |
| P2. Глобальный редакционный план | verified (локально) | tgfarm/editorial_plan.py, tgfarm/api.py:1787–1809, tgfarm/ui/, tests/test_editorial_plan.py | динамический календарь, ревизии + история, балансировка каналов; API `/api/editorial/plan(/refresh|/history)`; вкладка «План тем» в UI проверена в браузере (0842834, eae4444) |
| P3. Сообщества и правила (Reddit/Пикабу/VC.ru) | verified (локально) | tgfarm/community_policy.py, tgfarm/db/models.py (CommunityPolicySnapshot), tgfarm/community_targets.py, tgfarm/community_drafts.py, tgfarm/s9_growth/reddit_scout.py, tgfarm/api.py, tgfarm/ui/app.js, tests/test_community_policy.py | контракт CommunityPolicy закрыт: policy_from_candidate/policy_from_reddit/policy_for_plan, assess_policy → {status, allow_publish, reason, blockers, age_hours}, gate_plan для панели, история правил (append-only + rules_hash + коллапс дублей), гейт перед публикацией Пикабу и Reddit, API /api/community-policy/history, плашка вердикта в панели. 15 тестов зелёные |
| P4. Мультиагентная команда, бюджет, benchmark | todo | — | |
| P5. Нативная адаптация под площадки | implemented (частично) | tgfarm/platform_editorial.py | есть профили, guardrail, фактчек-контроль чисел/URL |
| P6. Автопубликация, очереди, восстановление | todo | — | есть heartbeat, retry-after, unknown-reconcile (по ROADMAP_NEXT) |
| P7. Аналитика и самообучение | todo | — | |
| P8. Память, импорт истории, бэкапы | implemented (частично) | tests/test_backup.py, tests/test_history_import_backup.py | 23 проверки прошли 2026-11-03 |
| P9. Медиастудия (изображения/видео) | todo | — | |
| P10. Интерфейс управления | in_progress | tgfarm/ui/, tgfarm/platforms.py, tgfarm/api.py | дефект ID ручного пакета исправлен + регрессия; статусы цикла/Promise.all уже верны и закрыты тестами; в карточке площадки — честный уровень возможности (см. P10b) |
| P10b. Честные уровни возможностей площадок | verified (локально) | tgfarm/platform_capability.py, tgfarm/platforms.py, tgfarm/cli.py, docs/PLATFORM_CENTER.md, tests/test_platform_capability.py | 5 уровней из кода (адаптеры + ручные пакеты): publish 23, draft_only 1 (vc.ru), manual_upload 8, read_only 0, unsupported 0; API-поля capability/capability_label/capability_note; CLI `capability --format markdown`; 10 тестов |
| P11. Финальная интеграция (Linux/Windows CI, EXE) | blocked (внешнее) | packaging/tgfarm.spec | EXE-сборка локально не завершалась из-за таймаута анализа зависимостей |

## Матрица аудита (заполняется по мере проверки)

- S9 (нативная адаптация): профили площадок, choose_native_format, build_editorial_brief, adapt_payload_with_ai, guardrail чисел/URL, факт-сохранность ссылок — проверены тестами (15→19+).
- UI/API: HTTP-выдача tgfarm/api.py; дефекты-кандидаты из ревью 2026-11-03 не исправлены (следующий ограниченный блок после P0).
- Площадки: статусы unsupported/read_only/draft_only/manual_upload/publish — сверять с config/platforms.yaml при реализации P3/P6.

## Известные проблемы

- CI №830 (run 41599822680) упал на тесте metadata; исправление выполнено и закоммичено в main (локально). Push был заблокирован: GitHub не был подключён в Sharrowkin; 2026-11-04 выполняется отправка накопленных коммитов (445+) в origin/main.
- P3 закрыт 2026-11-04. Принятое решение по контракту: `requirements` = правила назначения (verbatim, инертные) + editorial brief, в таком порядке; `rules_source` никогда не пуст (SELECTOR_RULES_SOURCE); `assess_policy` отдаёт `allow_publish` (гейт не пересчитывает статус). Один тест (`test_policy_from_pikabu_candidate`) требовал ровно `editorial_brief` и был несовместим с `test_to_dict_round_trip_and_inert_rules` (сохранение сырого текста правил) — обновлён сам тест, свойство безопасности сохранено.
- Windows EXE локально: PyInstaller не уложился в 600 с при анализе общей среды; в CI сборка проходит отдельно.
- UI-дефекты-кандидаты (метод запроса цикла, интерполяция manual package ID, общий Promise.all) — не исправлены.

## Последний завершённый этап

- P2: UI-вкладка «План тем» — рендер плана/ревизий, кнопка фиксации, CSS; браузерная проверка (видны 25 тем, чипы dzen/vk, приоритеты и причины); коммит 0842834 (2026-11-04).
- P3 (2026-11-04): контракт CommunityPolicy + гейт публикации — 15 тестов зелёные; гейт встроен в publish_pikabu_scheduled (после проверки идемпотентности, до открытия браузера) и в build_automatic_submission (Reddit); история правил в `community_policy_snapshots` с определением изменения по rules_hash; API `/api/community-policy/history`; вердикт показан в карточке подбора места в панели.

- P10 (частично, 2026-11-04): подтверждён и исправлен дефект ID ручного пакета (`\${esc(p.id)}` внутри шаблонной строки давал литерал `${esc(p.id)}`, и getElementById/inline-onclick не находили узел); регрессия закреплена тестом `test_manual_package_controls_use_interpolated_ids`. Дефекты «HTTP-метод цикла» и «общий Promise.all» в текущем коде уже исправлены (POST + allSettled) и защищены tests/test_ui_pipeline_contracts.py.

## Следующий конкретный шаг

1. P6: аудит и закрытие дыр автономной публикации — идемпотентность при аварийном завершении, неопределённый результат (unknown outcome) и сверка, восстановление in-flight после рестарта, per-platform очереди/лимиты; каждое утверждение проверять кодом и тестом, не документацией.
2. P3.1: история изменения правил сообществ доступна в панели (API готов, нужен раздел UI и проверка в браузере).
3. Затем: P7 (аналитика/самообучение) и P10-остаток (честные 5-уровневые статусы возможности площадки в UI).
4. Push: локальный `main` опережал `github/main` на 7 коммитов + этот этап; отправка — из рабочей копии (GCM-токен rpauts2), внешний CI подтверждается после push.
