# docs/EXECUTION_PLAN.md — реестр прогресса TgFarm

Файл-реестр для продолжения работы без повторного исследования репозитория.
Статусы: `todo`, `in_progress`, `blocked`, `implemented` (код + тесты локально), `verified` (есть подтверждающая проверка).

Обновлён: 2026-11-04 (continuous loop). Ветка: `main`. HEAD: `f428ee2` + доска сверки.

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
| P6. Автопубликация, очереди, восстановление | partially verified (живая эксплуатация) | tgfarm/s7_publish/__init__.py, tgfarm/api.py (_reconcile_*), tgfarm/db/models.py (CrosspostClaim) | есть: claim+heartbeat, publish_unknown при отмене, сверка оператором, backoff по классам ошибок, retry_authorized, per-platform лимиты, requeue_stuck, per-post deadline (P6c). Открыто: Dzen-очередь почти пуста из-за гейтов качества |
| P6c. Per-post deadline и изоляция публикаций | verified (локально) | tgfarm/s7_publish/__init__.py (PublishTimeout, _await_publish, publish_timeout_seconds), tgfarm/core/settings.py (VK_PUBLISH_TIMEOUT_S, TELEGRAM_PUBLISH_TIMEOUT_S, PUBLISH_CANCEL_GRACE_S), tests/test_publish_timeout.py | у каждой публикации свой бюджет (vk 120s, telegram 60s, dzen DZEN_PUBLISH_TIMEOUT_S); зависший адаптер отменяется, grace-период ограничен, задача-зомби отбрасывается с логом; таймаут → publish_unknown (нет слепого повтора); цикл изолирует падение одного поста; 5 тестов (зависание, поедание отмены, состояние, отсутствие дубля, бюджеты) |
| P6b. Классификация проверки авторизации | verified (локально) | tgfarm/platform_auth.py, tests/test_platform_auth_classification.py | 408/425/429/5xx и VK-коды 1/6/9/10 → authorized=None («не отказ токена»), а не False; живой инцидент 2026-11-04 (ложный алерт «переподключи VK»); 6 тестов |
| P7. Аналитика и самообучение | todo | — | |
| P8. Память, импорт истории, бэкапы | implemented (частично) | tests/test_backup.py, tests/test_history_import_backup.py | 23 проверки прошли 2026-11-03 |
| P8b. Аналитика ВК (просмотры/лайки/комментарии/репосты) | implemented (блокер токена) | tgfarm/s8_analytics/vk_stats.py, tgfarm/s8_analytics/__init__.py, tgfarm/api.py (_capabilities), tests/test_vk_metrics.py | сборщик готов: wall.getById батчами по 50, запись в post_stats; НО групповой токен VK отклоняется с error 27 — нужен VK_USER_TOKEN (user scope wall). Статус /api/status.capabilities.vk_metrics показывает это честно. Post_stats не пополнялись с 29.10 (было 22 строки, ВК: 0) |
| P6c. Per-post deadline и изоляция публикаций | verified (локально) | tgfarm/s7_publish/__init__.py (PublishTimeout, _await_publish, publish_timeout_seconds), tgfarm/core/settings.py, tests/test_publish_timeout.py | бюджет на каждую публикацию (vk 120s / telegram 60s / dzen DZEN_PUBLISH_TIMEOUT_S), отмена с grace, задача-зомби отбрасывается, таймаут → publish_unknown, цикл изолирует падение поста; 5 тестов |
| P6d. Реанимация незапущенных публикаций | verified (живой прогон) | tgfarm/s7_publish/__init__.py (requeue_unsubmitted), tgfarm/cli.py, tests/test_requeue_unsubmitted.py | 3 корзины: свежие pre-submit → requeue с новым бюджетом; старше 24–48ч → failed «expired» (не публикуем старые новости); неясный outcome драйвера → publish_unknown. Живой прогон: 365 expired + 51 parked |
| P6e. Гарантия обложки перед Дзеном | verified (живой прогон) | tgfarm/s7_publish/__init__.py (_ensure_cover_for_post, PublishImageRequired), tests/test_publish_cover_guard.py | Дзен без изображения не публикует: 7 постов имели 13 попыток и 0 медиа. Теперь обложка рендерится локально перед отправкой, а нерешаемое — терминальный отказ без повторов; 7 живых постов вылечены |
| P7b. LLM health из provenance черновиков | verified (локально) | tgfarm/core/llm_health.py, tgfarm/s4_generation (plans_after_llm_gate), tgfarm/s7_publish (_llm_recently_working), tests/test_llm_health_gate.py | пока все последние черновики офлайн, Дзен не генерируется и не публикуется (DZEN_REQUIRES_LLM); 153/156 отклонений за 6ч были именно такими офлайн-черновиками |
| P7c. 403 consent не валит весь LLM-провайдер | verified (локально) | tgfarm/llm/client.py, tests/test_llm_model_level_403.py | причина «всё офлайн с 27.10»: 403 consent_required для первой free-модели убивал провайдера на час. Теперь паркуется только модель; 401/402 по-прежнему демоутят провайдера |
| P7d. Честная проверка авторизации VK | verified (живой прогон) | tgfarm/platform_auth.py, tests/test_platform_auth_classification.py | VK 5.199 отдаёт response.groups объектом — KeyError(0) записывался как «доступ не пройден» (ложный алерт). Разбор обоих форматов; лимиты/5xx → authorized=None, а не False. Живьём: «Всякая всячина» подтверждена |
| P9. Медиастудия (изображения/видео) | частично verified (живой прогон) | tgfarm/s6_visual/, tgfarm/s11_video/renderer.py, tgfarm/ui/app.js | обложки: детерминированный Pillow-рендер + AI-арт как опция (Pollinations упирается в free-лимит и уходит в шаблон). Вертикальное видео: канонический renderer (storyboard → кадры → ffmpeg zoompan → 1080×1920 MP4 + sidecar JSON) проверен живьём на опубликованном посте: 14 с, 1.98 МБ, запись Media kind=video. Кнопка «Создать вертикальное видео» есть в панели. Дублирующий модуль не создаём — один путь |
| P10c. Доска сверки оператора | verified (локально + браузер) | tgfarm/review.py, tgfarm/api.py (/api/review/queue), tgfarm/ui/index.html, tgfarm/ui/app.js, tgfarm/ui/app.css, tests/test_review_queue.py, tests/test_ui_pipeline_contracts.py | три корзины: publish_unknown (51), failed (365), gated без слота (41); у каждой карточки причина, возраст, число попыток и действия на существующих эндпоинтах (reconcile/retry/publish/reject); вкладка «Сверка» с счётчиком проверена в браузере |
| P10. Интерфейс управления | in_progress | tgfarm/ui/, tgfarm/platforms.py, tgfarm/api.py | дефект ID ручного пакета исправлен + регрессия; статусы цикла/Promise.all уже верны и закрыты тестами; в карточке площадки — честный уровень возможности (см. P10b); добавлена вкладка «Сверка» (P10c) |
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

- 2026-11-04, continuous loop (9 коммитов от 59a5c87 до f428ee2): P3 закрыт; P10b уровни возможностей; P6c per-post deadline; P6d requeue/expire незапущенных (365 expired + 51 parked живьём); P6e обложка перед Дзеном (7 постов вылечены); P7b LLM health gate; P7c 403 consent не убивает провайдера; P7d разбор response.groups и authorized=None для лимитов; P8b сборщик метрик ВК.
- 2026-11-04, доска сверки: `/api/review/queue` + вкладка «Сверка» с тремя корзинами (51/365/41) и действиями на существующих эндпоинтах; проверено в браузере на живой базе; тесты 1008 passed.

## Следующий конкретный шаг

1. Перезапустить ферму для загрузки P6e/P8b/P7d (в v20 ещё нет гарантии обложки и сборщика ВК) — после проверки, что нет постов в `publishing`.
2. Блокер оператора: `VK_USER_TOKEN` (user scope wall) — без него метрики ВК и фото в ВК недоступны (error 27); после получения включить сбор и S12-обучение по ВК.
3. LLM: free-тир ограничен (1 запрос/мин на модель, часть моделей требует consent на unorouter). После согласия на deepseek/flash цепочка начнёт отдавать онлайн-черновики — тогда Дзен оживёт сам (health-gate снимется по provenance).
4. Дальше: reconcile 51 publish_unknown (нужна ручная сверка на Дзен), S12 по реальным метрикам, UI-раздел истории правил CommunityPolicy, медиастудия/видео.
