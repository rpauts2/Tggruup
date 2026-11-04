# TgFarm: интеграционная проверка первого этапа

Дата: 2026-11-03. Полная модернизация продукта **не завершена**. Этот этап закрывает контракт metadata нативного редактора и локальную offline-регрессию. Реальные аккаунты и рабочие БД не использовались; TASK.md и приёмочные фикстуры не изменялись.

## Фактическое состояние main и CI

- `git rev-parse HEAD`: `1ccd67abd92fe72d62e46d4116c0ba94671df919`, ветка `main`.
- GitHub API `GET /repos/rpauts2/Tggruup/commits/main` вернул тот же SHA. Это проверка удалённого main, а не вывод из исторического задания.
- Локальный `origin/main`: `b5266b83bb102f0058bffe2691e1faeeddb20f93`; статус ahead 439 относится к этой локальной tracking-ссылке, а не доказывает отставание GitHub main. Ссылки не изменялись.
- GitHub API actions/runs: последний запуск CI №830, run ID `41599822680`, тот же SHA, conclusion `failure`. В обоих jobs тесты завершились ошибкой; smoke и Windows packaging были пропущены. Локальные исправления ещё не опубликованы, новый внешний CI не запускался.

## Что реализовано

- `tgfarm/platform_editorial.py`: deepcopy входного metadata изолирует вложенные dict/list от исходного пользовательского объекта. Существующие ветки и guardrail сохранены.
- `tests/test_platform_editorial.py`: законный служебный `editorial_adaptation` допускается отдельно от обязательного сохранения пользовательских полей; проверки статуса отказа, причины, модели, провайдера и finalize сохранены.
- `tests/test_platform_editorial_metadata_regression.py`: четыре offline-сценария (успех, отказ по числам, отказ по URL, fallback), включая проверку отсутствия общих вложенных ссылок. Исправлена Windows-совместимость сетевого запрета: внутренний socketpair asyncio разрешён только внутри синхронной stdlib-операции; application connect запрещён.
- `.sharrowkin/scripts/integration_smoke.py`: запускает существующий CLI demo на временной БД и media, обнуляет перечень credentials из тестовой конфигурации, запрещает внешние соединения. Это проверочный инструмент, не новый pipeline.

## Проверки

Среда: Windows, Python 3.14.3 (соответствует >=3.11 в pyproject.toml, но отличается от Python 3.11 в CI), PyInstaller 6.19.0.

| Команда | Результат |
| --- | --- |
| `python -m pytest -q tests/test_platform_editorial.py tests/test_platform_editorial_metadata_regression.py` | 15 passed; до исправления fixture было 11 passed / 4 failed из-за блокировки внутреннего Windows socketpair |
| `python -m pytest -q` | 927 passed, 2 DeprecationWarning (cookies TestClient, SQLite datetime adapter) |
| `python -m compileall -q tgfarm tests` | exit 0 |
| `node --check tgfarm/ui/app.js` | exit 0; не является визуальной приёмкой UI |
| `python .sharrowkin/scripts/integration_smoke.py` | exit 0; 17 synthetic items, 6 clusters, 12 drafts, 12 covers, 5 scheduled, 3 dry-run, 0 publish failures, 2 skipped_llm; can_publish=false |
| `python -m pytest -q tests/test_backup.py tests/test_history_import_backup.py tests/test_demo_safety.py` | 23 passed; disposable backup/restore, не восстановление рабочей БД |
| `python -m PyInstaller packaging/tgfarm.spec --noconfirm --distpath .sharrowkin/integration-dist --workpath .sharrowkin/integration-build` | остановлена таймаутом 600 секунд при анализе зависимостей общей среды; EXE и запуск не подтверждены |
| `python -m ruff check tgfarm/platform_editorial.py tests/test_platform_editorial_metadata_regression.py` | первоначально 23 замечания; новый порядок импортов исправлен, остальные E501/E731 относятся к неизменённым строкам; lint не является существующим CI gate |

После последней перестановки импортов повторены адресные тесты: 15 passed; compileall exit 0; ruff --select I: All checks passed; git diff --check: exit 0. Полные 927 тестов прошли до этой перестановки импортов; поведенческих изменений после полного прогона не было.

## Контракт и приёмка

Сохранение пользовательских ключей, вложенных значений и независимости ссылок проверено тестами. Служебный результат отражает ai_native / deterministic_guardrail; отказ не выдаётся за успех. Числовой и URL guardrail не обходятся. Все проверки выполнялись без реальной отправки.

Существующий CI (`.github/workflows/ci.yml:31–57,78–120`) устанавливает `.[dev,dzen]` / `.[dev,dzen,desktop,build]`, выполняет compileall, node syntax, pytest, offline demo; Windows job дополнительно PyInstaller, наличие EXE, upload artifact. Наличие EXE само по себе не проверяет запуск frozen-приложения.

Windows spec (`packaging/tgfarm.spec:21–25,31–60,71–100`) включает config/UI/.env.example, ленивые импорты и desktop entry. При локальном запуске spec прошёл добавление data; предупреждения анализа общей среды не представлены доказанными ошибками CI. Чистая Windows/Python 3.11, WebView2, ресурсы frozen bundle и браузерный runtime ещё требуют проверки.

## Карта аудита и реальные ограничения

Командные read-only отчёты, не runtime-приёмка:
- UI существует в `tgfarm/ui/index.html`, `app.js`, `app.css`; HTTP-выдача — `tgfarm/api.py`. Обнаружены кандидаты дефектов: метод запроса цикла, интерполяция manual package ID, общий Promise.all. В этом этапе не исправлялись и в браузере не проверялись.
- Редактор: профили, choose_native_format, build_editorial_brief, adapt_payload_with_ai; наличие профиля не доказывает publisher или актуальность внешних правил.
- Настройки площадки, авторизация и реальная приёмка публикации — разные статусы. Ни одна площадка не получила новую live-приёмку на основании этих тестов.

## Что осталось

Полная модернизация S1–S12, сравнение агентных frameworks, устранение UI-дефектов и приёмка всех площадок остаются незавершёнными. Текущий результат — проверенная локальная регрессия редактора, не новая автономная экосистема. Следующий ограниченный блок: воспроизвести и исправить manual package ID и HTTP-метод цикла, затем частичные GET-отказы, сохранив consent и unknown-reconcile; принять через изолированный UI без аккаунтов. Windows packaging необходимо повторить в изолированной среде с объявленными extras, затем проверить запуск без доступа к рабочим данным.
