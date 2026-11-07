# Очередь подтверждённых дефектов — 2026-11-07

Read-only сессия: HEAD `1fdfd08`, ветка `missions/t1-t4-20261106`, рабочее дерево чистое (только нетронутый `.qoder/`). Все пункты ниже проверены координатором по реальному коду в этой сессии; правки ещё НЕ внесены. Начиная отсюда, не перепроверять заново — брать якоря и делать.

## Адаптеры S9 (подтверждено, изолированные правки)

1. `tgfarm/s9_growth/tumblr.py:19-24` — `_valid_http_url`: `urlsplit()` без `try/except ValueError`; URL вида `http://[bad` роняет адаптер исключением вместо отказа. Фикс: обернуть, при `ValueError` возвращать `""`.
2. `tgfarm/s9_growth/tumblr.py:123-129` — любой сбой `get_valid_tumblr_access_token()` помечается `status="permanent"`, включая сетевые/5xx. Фикс: заменить на `crosspost_error_status(exc)` из `tgfarm/s9_growth/adapters.py:175-187` (тот же хелпер уже используется в пути публикации).
3. `tgfarm/s9_growth/pinterest.py:36-42` и `tgfarm/s9_growth/instagram.py:47-53` — `Retry-After` парсится только как `int(raw)`; HTTP-дата отбрасывается (`except` → `None`). Фикс: добавить разбор HTTP-даты (`email.utils.parsedate_to_datetime`, UTC, clamp 0..86400, нечисловую неразбираемую — как сейчас).
4. `tgfarm/s9_growth/instagram.py:29-60` (`_http_result`) — HTTP 400 с Graph-кодом троттлинга 4/17/32/613 в теле ответа помечается `permanent` и не повторяется. Фикс: разобрать `response.json()["error"]["code"]` (защитно, через try), коды троттлинга → retryable.

Тесты: расширить `tests/test_tumblr_adapter.py`, `tests/test_pinterest_adapter.py`, `tests/test_instagram_adapter.py` (файлы существуют).

## S11 видео (подтверждено)

5. `tgfarm/s11_video/__init__.py:108-116` — `_same_domain` сравнивает только два последних элемента хоста (`a[-2:] == b[-2:]`): `evilsamedomain.com` считается тем же доменом, что `samedomain.com`. Фикс: сравнение по границе метки (`host == domain or host.endswith("." + domain)`).

## Сетка расписания (подтверждено ревью, требуется аккуратный фикс)

6. `tgfarm/editorial_plan.py:93-178` (`_first_free_and_legal_slot`) и зонд `no_legal_hours` (~:716-731) — фиксированный шаг сетки 90 минут (`SLOT_STEP_MINUTES`) может целиком перепрыгнуть легальное окно < 90 минут: слоты теряются или план даёт ложное `no_legal_hours`. Фикс: кандидаты = точки сетки ∪ начало каждого легального часа; зонд легальности звать независимо от сетки.

## Остальное из отчётов агентов (не подтверждено координатором — проверять перед правкой)

7. `tgfarm/s12_learning/__init__.py:~949-956` — PipelineRun читается newest-first, список ошибок может оказаться oldest-first/перезаписан; `worst_angle` противоречит `best_angle`.
8. `tgfarm/llm/client.py` — offline-fallback: если провайдер реально ответил, но ответ не распарсился, spend может не финализироваться (finalize-точки ~:560-620); таймаут клиента равен полному провайдерному deadline (`tgfarm/llm/reliability.py:38`).
9. `tgfarm/ui/app.js:~5639` — `Number(result.body.length || 0)` падает, если `body` отсутствует в ответе package-эндпоинта.
10. Токен-обновление Tumblr: что именно бросает `tgfarm/tumblr_oauth.py:223` — проверить типы исключений перед пунктом 2, чтобы классификация не смазала настоящие `invalid_grant`.

## Контекст

- Полный прогон на `1fdfd08`: последний сохранённый — 3183 passed, 4 xfailed (в `docs/SESSION_CLOSE_20261106.md`), в этой сессии код не менялся.
- Открыто из прошлых сессий: изолированная Windows-сборка, внешний CI, безопасная Git-синхронизация расходившихся remotes (origin → rpauts2/tgfarm, github/upstream → rpauts2/Tggruup; без force push).


## Исправлено в сессии 2026-11-07 (Freebuff)

Проверено по текущему коду: пункты 1 и 5 уже закрыты в `0a633f6`. Остальные закрыты здесь.

- **п.2 Tumblr.** `TumblrOAuthError` несёт HTTP-статус обмена токена. Сетевой сбой и 5xx больше не `permanent` (публикация ещё не отправлялась, повтор безопасен); мёртвый grant (4xx, кроме 429) остаётся `permanent`. Тесты: `tests/test_tumblr_oauth_failure_category.py`.
- **п.3 Pinterest/Instagram.** `Retry-After` в форме HTTP-даты читается общим `crosspost_retry_after_seconds`, а не только `int()`. Тесты: `tests/test_adapter_retry_after_date.py`.
- **п.4 Instagram.** Graph-коды троттлинга 4/17/32/613 при HTTP 400 классифицируются как retryable (`failed`), а не `permanent`. Там же.
- **п.6 Сетка слотов.** Кандидатом стала не только точка 90-минутной сетки, но и начало легального часа, в который не попадает ни одна точка. 24/7-путь и занятая легальная точка сохраняют прежние ответы. Тест: `tests/test_plan_slot_narrow_window.py`. Следствие: `next_legal_slot_after` теперь возвращает 09:00 локального времени (12:00 UTC) вместо 10:29 в `tests/test_plan_execution_apply.py`; ожидание закреплено точным литералом и объяснено.
- **п.7 S12.** Ошибки стадий и площадок читаются по возрастанию времени, поэтому новее — значит последнее в upsert. Устаревший `mistake/worst_angle` удаляется, когда тот же угол стал лучшим. Тест: `tests/test_s12_mistake_recency.py`.
- **п.9 UI.** Отсутствующий `body` в пакете ручной подготовки больше не роняет блок: `(result.body || "").length`.

Проверки: полный прогон на изменённом дереве — 3334 passed, 4 xfailed, 16 failed; 15 из 16 воспроизводятся на чистом HEAD отдельным клоном (community policy, W29-B, W25-A, отсутствующий модуль в `test_ideas2`), шестнадцатое было устаревшим ожиданием и после правки проходит (79 passed по затронутым файлам). Ruff по новым тестам — clean, `compileall` — 0, `tools/check_platform_increment.py` — PASS. Реальные публикации и production-база не затрагивались.
