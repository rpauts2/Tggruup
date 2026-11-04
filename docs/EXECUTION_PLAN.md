# docs/EXECUTION_PLAN.md — реестр прогресса TgFarm

Файл-реестр для продолжения работы без повторного исследования репозитория.
Статусы: `todo`, `in_progress`, `blocked`, `implemented` (код + тесты локально), `verified` (есть подтверждающая проверка).

Обновлён: 2026-11-04. Ветка: `main` (единственная рабочая). HEAD перед этапом: `1ccd67a`.

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
| P3. Сообщества и правила (Reddit/Пикабу/VC.ru) | todo | — | единый контракт CommunityPolicy |
| P4. Мультиагентная команда, бюджет, benchmark | todo | — | |
| P5. Нативная адаптация под площадки | implemented (частично) | tgfarm/platform_editorial.py | есть профили, guardrail, фактчек-контроль чисел/URL |
| P6. Автопубликация, очереди, восстановление | todo | — | есть heartbeat, retry-after, unknown-reconcile (по ROADMAP_NEXT) |
| P7. Аналитика и самообучение | todo | — | |
| P8. Память, импорт истории, бэкапы | implemented (частично) | tests/test_backup.py, tests/test_history_import_backup.py | 23 проверки прошли 2026-11-03 |
| P9. Медиастудия (изображения/видео) | todo | — | |
| P10. Интерфейс управления | todo | tgfarm/ui/ | известные кандидаты дефектов: HTTP-метод цикла, интерполяция manual package ID, общий Promise.all |
| P11. Финальная интеграция (Linux/Windows CI, EXE) | blocked (внешнее) | packaging/tgfarm.spec | EXE-сборка локально не завершалась из-за таймаута анализа зависимостей |

## Матрица аудита (заполняется по мере проверки)

- S9 (нативная адаптация): профили площадок, choose_native_format, build_editorial_brief, adapt_payload_with_ai, guardrail чисел/URL, факт-сохранность ссылок — проверены тестами (15→19+).
- UI/API: HTTP-выдача tgfarm/api.py; дефекты-кандидаты из ревью 2026-11-03 не исправлены (следующий ограниченный блок после P0).
- Площадки: статусы unsupported/read_only/draft_only/manual_upload/publish — сверять с config/platforms.yaml при реализации P3/P6.

## Известные проблемы

- CI №830 (run 41599822680) упал на тесте metadata; исправление выполнено и закоммичено в main (локально). Push заблокирован: GitHub не подключён в Sharrowkin.
- Windows EXE локально: PyInstaller не уложился в 600 с при анализе общей среды; в CI сборка проходит отдельно.
- UI-дефекты-кандидаты (метод запроса цикла, интерполяция manual package ID, общий Promise.all) — не исправлены.

## Последний завершённый этап

- P2: UI-вкладка «План тем» — рендер плана/ревизий, кнопка фиксации, CSS; браузерная проверка (видны 25 тем, чипы dzen/vk, приоритеты и причины); коммит 0842834 (2026-11-04).

## Следующий конкретный шаг

1. P3: контракт `CommunityPolicy` (площадка, сообщество, аудитория, разрешённые/запрещённые темы, требования к форматам/тегам/ссылкам/медиа, дата и источник проверки правил) + выбор назначения для Reddit/Пикабу/VC.ru на базе его; повторная проверка правил перед публикацией; отказ при неизвестных правилах.
2. Затем: P10-дефекты UI (manual package ID, HTTP-метод цикла, Promise.all) изолированной приёмкой.
3. Затем: P6–P8 (публикация/аналитика/память) по реестру.
4. Push: GitHub не подключён в Sharrowkin — требуется «Подключить GitHub» в панели Git; до этого CI/EXE не проверяются извне.
