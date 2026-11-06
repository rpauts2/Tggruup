# READINESS_MATRIX — что доказано, а что только написано (W31-D)

Один вопрос, на который отвечает этот файл: для каждого этапа конвейера —
реализован ли код, покрыт ли он офлайн-тестом, запускается ли Планировщиком
сейчас, и наблюдали ли мы его на живой ферме. Это четыре разных утверждения, и
репозиторий регулярно смешивал их. Реестр работ живёт в
[`EXECUTION_PLAN.md`](EXECUTION_PLAN.md), чекпойнт — в
[`CONTINUATION_STATE.md`](CONTINUATION_STATE.md), процесс — в
[`TEAM_PROCESS.md`](TEAM_PROCESS.md); здесь нет ни роадмапа, ни пересказа —
только разделение четырёх типов доказательства.

Замер: 2026-11-06, база `183daa4`, источники — чтение кода,
`tests/test_autonomy_chain_offline.py` (прогон в этом сеансе: 17 passed,
4 xfailed, 3.53 s), `tools/autonomy_chain_report.py` против живой базы
`tgfarm.db` (strict `mode=ro`) и журнала `farm_serve25.log`. Точные команды —
в приложении; матрицу можно переснять, не доверяя этому тексту.

## Колонки и теги

| Колонка | Вопрос |
|---|---|
| Implemented | есть ли код этапа в дереве |
| Offline-tested | утверждает ли поведение этапа офлайн-тест (без сети) |
| Scheduler-reachable | вызывает ли запущенный планировщик этот код сам, по часам |
| Live-observed | есть ли строка журнала/базы/лога, доказывающая фактический прогон на ферме |

Теги ячеек (верbatim): `OBSERVED`, `CODE_VERIFIED`, `TEST_VERIFIED`,
`CI_VERIFIED`, `DOCUMENTED_ONLY`, `INFERRED`, `UNKNOWN`. Ячейка `UNKNOWN`
печатается как `UNKNOWN` — её не заполняют, чтобы таблица выглядела полной.

## Одно измерительное свойство, которое портит наивное чтение

Живая ферма крутит сборку старше базы `183daa4`: в живой базе у таблицы `posts`
нет колонок `plan_slot_at` и `plan_entry_id` (`PRAGMA table_info(posts)`), а
`meta` строк журнала пуст (`{}`), поэтому трёхсостоящие счётчики
(measured/partial/not_reported) в живой колонке не читаются. Все ячейки
`Live-observed` ниже описывают запущенную сборку, а не обещания репозитория.
Это ровно та путаница, из-за которой файл существует.

## Матрица

| Этап | Implemented | Offline-tested | Scheduler-reachable | Live-observed |
|---|---|---|---|---|
| **S1 Research** (`tgfarm/s1_sources/`) | CODE_VERIFIED — collectors.py, telegram_source.py; регистрация `tgfarm/pipeline.py:1530` | TEST_VERIFIED — test_s1_dedup_reprints.py, test_collector_registry.py; РЕАЛЬНАЯ сетевая сборка офлайн недостижима и это закреплено strict-xfail (tests/test_autonomy_chain_offline.py:1202) | CODE_VERIFIED — job `cycle`, `tgfarm/pipeline.py:2772` (сбор за `collect=True`) | OBSERVED — `s1_collect` ok 1626 строк, последняя 2026-11-06 13:06:55 (запрос A3); в базе 151 источник, включено 60 |
| **S2 Trend Scout** (`tgfarm/s2_trends/`) | CODE_VERIFIED — `__init__.py`, `pipeline.py:1532` | TEST_VERIFIED — test_autonomy_chain_offline.py:851, :860; test_s2_trend_acceleration.py | CODE_VERIFIED — job `cycle` | OBSERVED — `s2_trends` ok 2494 строки, последняя 2026-11-06 13:08:46 |
| **S3 Topic Router** (`tgfarm/s3_router/`) | CODE_VERIFIED — `__init__.py` (plan_cluster, skip-реестр) | TEST_VERIFIED — test_router_capability.py, test_router_skips_api.py, test_pipeline.py | CODE_VERIFIED — отдельного джоба нет; вызывается из недр S4/S5: s4_generation/__init__.py:1888, s5_quality/__init__.py:487 | INFERRED — собственный строк журнала у этапа нет; выводим из `s4_generate` ok 2437 (последняя 2026-11-05 12:46:48) — маршрутизация произошла бы внутри, но доказывающей строки нет |
| **S4 Writer + Fact Checker** (`tgfarm/s4_generation/`) | CODE_VERIFIED — `__init__.py`, memory.py; `pipeline.py:1543` | TEST_VERIFIED — test_autonomy_chain_offline.py:1029 (провенанс черновика); test_s4_durable_format_lessons.py | CODE_VERIFIED — job `cycle` | OBSERVED — `s4_generate` ok 2437; сбой фиксировался: 83 строки «stage s4_generate failed» в farm_serve25.log (grep A4); в логе 16:47 виден LLM-фолбэк на offline fallback |
| **S5 Quality Review Board** (`tgfarm/s5_quality/`) | CODE_VERIFIED — `__init__.py`; `pipeline.py:1549` | TEST_VERIFIED — test_autonomy_chain_offline.py:1046; test_quality.py, test_publish_gate_matrix.py | CODE_VERIFIED — job `cycle` | OBSERVED — `s5_quality` ok 2426, последняя 2026-11-05 12:47:11; вердикты в базе: rejected 18903 / gated 49 / scheduled 242 постов |
| **S6 Visual** (`tgfarm/s6_visual/`) | CODE_VERIFIED — `__init__.py`, article_images.py; `pipeline.py:1560` (условно: только для прошедших гейт) | TEST_VERIFIED — test_autonomy_chain_offline.py:1072 (media-строка + файл на диске); test_article_images.py | CODE_VERIFIED — job `cycle` | OBSERVED с оговоркой — ok 529 (последняя 2026-11-05 12:49:17) против 304 строк `error` (последняя 2026-11-02 17:58); media 1637 строк: template 1436 / article_image 188 / infographic 11 / ai_art 1 |
| **S7 Publishing** (`tgfarm/s7_publish/`) | CODE_VERIFIED — `pipeline.py:1563` + publish-tick `pipeline.py:2783` | TEST_VERIFIED — test_autonomy_chain_offline.py:1082 (слот или hold), :1096 (решение в исходном статусе) | CODE_VERIFIED — полный цикл и отдельный job `publish-tick` каждые 5 мин | OBSERVED — `s7_publish` ok 3699, последняя 2026-11-06 13:46:20; 527 опубликованных постов, последний 2026-11-06 13:16:23; `publish_tick` ok 1298 + 3 timeout. КАВЕАТ: строка `s7_publish_due` в живой базе отсутствует (0 строк) — это имя живёт только в харнессе; поведение Due-публикации покрыто `publish_tick`-строками |
| **S8 Analytics** (`tgfarm/s8_analytics/`) | CODE_VERIFIED — `__init__.py`, audit.py, portfolio.py; `pipeline.py:1569` + collector-джобы `pipeline.py:2943` | TEST_VERIFIED — test_autonomy_chain_offline.py:1116 (честный `skipped` и запрет выдуманных метрик); test_dzen_analytics.py; реальность метрик офлайн — strict-xfail :1240 | CODE_VERIFIED — job `cycle` + `_register_collector_jobs` | OBSERVED, но голодно — `s8_analytics` ok 2414 (последняя 2026-11-05 12:49:39), при этом `post_stats` всего 22 строки на 527 опубликованных постов: сбор метрик на живой ферме почти не измеряет ничего |
| **S9 Crosspost** (`tgfarm/s9_growth/adapters.py`) | CODE_VERIFIED — `pipeline.py:1566` + job `crosspost-tick` `pipeline.py:2792` | TEST_VERIFIED — test_autonomy_chain_offline.py:1096 (журнал исходов), test_crosspost_skipped.py, test_crosspost_retry_after.py | CODE_VERIFIED — два пути: цикл и самостоятельный тик каждые 10 мин | OBSERVED — `s9_crosspost` ok 2413; crosspost_logs: vk posted 455 (последний 2026-11-06 13:16:23), dzen posted 37 (последний 2026-11-04 16:18:55), vk failed 146 (последний 2026-11-06 02:06:13, категории auth/exception) |
| **S9 Native Editorial** (`tgfarm/platform_editorial.py`) | CODE_VERIFIED — choose_native_format/adapt_payload, вызывается из `distribution.py:423` | TEST_VERIFIED — test_platform_editorial.py, test_platform_editorial_policy.py; реестр (EXECUTION_PLAN, строка S9) утверждает 15→19+ тестов — DOCUMENTED_ONLY для самих чисел | UNKNOWN — производский вызов доказан только из `api.py:3842` (ручной предпросмотр); что планировщик доходит до адаптации по часам — не установлено | UNKNOWN — атрибуции в живой базе нет: колонки адаптации в `crosspost_logs` в живой схеме отсутствуют, отдельного имени в журнале нет |
| **S10 Format Planner** (`tgfarm/s10_format/`) | CODE_VERIFIED — `__init__.py` (plan_for_cluster, plan_with_history) | TEST_VERIFIED — test_format_performance_planner.py, test_format_planner_history.py, test_evidence_gated_format_lessons.py | CODE_VERIFIED — отдельного джоба нет; вызывается из S4: s4_generation/__init__.py:1740 | OBSERVED — `post_format` заполнен на всех 22624 постах: analysis 11124 / news 7893 / stat 1833 / flash 1769 / article 5; пять `article` за месяц — измерение, а не мнение |
| **S11 Video pipeline** (`tgfarm/s11_video/`) | CODE_VERIFIED — докстринг `__init__.py` называет модуль лицензионным гейтом видео и подтверждает утверждение онбординга («S11 — это видео-конвейер»); renderer.py рядом | TEST_VERIFIED — test_video_renderer.py, test_format_video.py | CODE_VERIFIED — только опосредованно: s4_generation/__init__.py:1890 (`fetch_video_for`) и s9_growth/__init__.py:364 (`render_post_short`); отдельного джоба в `_build_scheduler` нет — grep `s11`/`video` по `pipeline.py` пуст | OBSERVED единично — ровно 1 строка `media` с kind=video (2026-11-04 18:42:55, data/video/rendered/post_*.mp4); в farm_serve25.log упоминаний видео — 0 |
| **S12 Learning** (`tgfarm/s12_learning/`) | CODE_VERIFIED — `__init__.py`; `pipeline.py:1572` | TEST_VERIFIED — test_autonomy_chain_offline.py:1129 (рекомендация обязана назвать sample size); test_learning.py, test_gate_failure_learning.py; урок с живой выборкой — strict-xfail :1255 | CODE_VERIFIED — job `cycle` | OBSERVED, но на пустоте — `s12_learning` ok 2011 (последняя 2026-11-05 12:49:39), `learning_memory` 24 строки; уроки из вовлечения читали бы 22 строки post_stats — этот путь живьём не наполняется |
| **Шов editorial plan** (`tgfarm/plan_execution.py`, между S3 и S4) | CODE_VERIFIED — build/apply/booking, `pipeline.py:2830`, :2839, :2857 | TEST_VERIFIED — test_autonomy_chain_offline.py:873, :920, :973, :1276 (бронь слота = колонка posts, не доставка); test_plan_execution_apply.py | CODE_VERIFIED — три джоба зарегистрированы, но за флагом `settings.plan_execution_enabled` (`pipeline.py:2822`); в этом замере флаг не включался | UNKNOWN — строк `editorial_plan_build/apply` и `s4_plan_booking` в живой базе 0, `editorial_plans` пуста, и даже колонок `plan_slot_at`/`plan_entry_id` в живой схеме нет: включать флаг не в чем, и читать результат негде |

## Слабое звено каждого ряда — один исполнимый шаг

- **S1** — слабое: Offline-tested (реальный сбор в strict-xfail). Шаг: дать
  collectors точку входа с инъектированным транспортом вместо сети; маркер
  tests/test_autonomy_chain_offline.py:1202 снимется сам через XPASS, и это и
  есть сигнал, не решение — решение примет координатор.
- **S2** — слабое: Live-observed по глубине (счётчики строк журнала пусты).
  Шаг: задеплоить сборку базы и после следующего цикла повторить запрос A3 —
  ждать `meta.counters="measured"` вместо `{}`.
- **S3** — слабое: Live-observed (INFERRED). Шаг: добавить в `_stage`-метафиз
  S4 число маршрутизированных каналов и отказов skip-реестра; тогда A3 станет
  печатать attributable-строку и ячейка станет OBSERVED.
- **S4** — слабое: стабильность live (83 падения в логе). Шаг: сгруппировать
  причины командой A4 и закрыть регрессией самую частую категорию — не «починить
  LLM», а назвать первую причину по числу строк.
- **S5** — слабое: разбор вердиктов (rejected 18903 без известной разбивки).
  Шаг: `SELECT reject_reason, COUNT(*) … GROUP BY 1` (A3) и зафиксировать
  топ-причину тестом на её переформулирование.
- **S6** — слабое: live-надёжность (304 error-строки). Шаг: `SELECT error FROM
  pipeline_runs WHERE stage='s6_visual' AND status='error' ORDER BY id DESC
  LIMIT 5` (A3) и сделать реджект-тест на самый свежий не-сетевой текст.
- **S7** — слабое: именование шва (живой журнал не знает `s7_publish_due`).
  Шаг: завести это имя в live-тике `_publish_tick` через `_stage` — тогда
  A2-репорт перестанет печатать «absent» против живой базы; тестов для этого у
  исполнителя нет права, шв закрывает координатор.
- **S8** — слабое: глубина live (22 метрики на 527 постов). Шаг: проверить
  авторизацию Telethon-сессии панелью фермы и повторить A3 — `post_stats`
  обязан перестать быть константой между замерами.
- **S9 crosspost** — слабое: VK failed=146 продолжает расти. Шаг: повторить
  запрос A3 через сутки и сравнить счётчик failed; рост без смены токена —
  это новый инцидент, а не «история».
- **S9 native editorial** — слабое: обе правые колонки UNKNOWN. Шаг: назвать
  production-вызов адаптации (или признать, что это ручной предпросмотр API) и
  завести атрибут исхода адаптации в журнале кросспоста; до этого любая
  строчка про «S9 работает на ферме» — DOCUMENTED_ONLY.
- **S10** — слабое: `article` выбран 5 раз за месяц. Шаг: запросом A3
  (раскладка post_format на опубликованных) измерить, отбраковывает ли правила
  article на коротких кластерах, и зафиксировать измерение тестом, а не менять
  пороги на глаз.
- **S11** — слабое: live-частота n=1. Шаг: скормить одному живому кластеру
  источник с right-media того же домена и повторить A3-запрос по `media
  kind='video'`; если гейт отказал — записать отказ как измерение стоимости
  лицензии.
- **S12** — слабое: живые выборки пусты. Шаг: закрыть S8 (метрики), затем
  A3-запрос `learning_memory` обязан показать рост sample_size — иначе этот
  ряд остаётся честным нулём.
- **Editorial plan** — слабое: UNKNOWN целиком. Шаг: после деплоя сборки базы
  включить флаг (этим документом — нет; это действие координатора) и ждать
  непустые строки `editorial_plan_build/apply` в запросе A3; до первой строки
  любая оценка этого шва — UNKNOWN, и это правильный ответ.

## Сколько ячеек стало UNKNOWN и почему это не провал

Ровно **три** ячейки печатаются словом `UNKNOWN`: S9-native × Scheduler-reachable,
S9-native × Live-observed, editorial plan × Live-observed. Причина во всех трёх
одна: в живой базе нет ни строк журнала, ни колонок схемы, которые можно
показать пальцем; поставить туда `OBSERVED` или `DOCUMENTED_ONLY` означало бы
повторить тот самый дефект мышления, против которого этот файл написан. Одна
ячейка помечена `INFERRED` (S3 × Live): косвенное доказательство есть — этап
обязан выполняться внутри `s4_generate`, — прямого (атрибутируемой строки) нет.
Слабости вроде пустых счётчиков журнала или одной live-видео-строки названы в
ячейках как оговорки к `OBSERVED`, а не задним числом: их цифры приведены
рядом, и пересъёмка A3 их проверит.

## Приложение A — как переснять матрицу

Все команды читали; ничего не запускается на запись, база открывается строго
`mode=ro`. Консоль Windows в cp1252 не печатает кириллицу — вывод перенаправлять
в файлы с `PYTHONIOENCODING=utf-8` (как ниже) или писать в файл
`encoding="utf-8"`.

A0. База и дерево:
```bash
git rev-parse HEAD        # ожидается 183daa4 или потомок
```

A1. Офлайн-цепочка (реально прогоняется, 3.5 с):
```bash
PYTHONIOENCODING=utf-8 python -m pytest tests/test_autonomy_chain_offline.py -q --tb=line
# замер этого сеанса: 17 passed, 4 xfailed
```

A2. Отчёт по цепочке против живой базы (только чтение):
```bash
PYTHONIOENCODING=utf-8 python tools/autonomy_chain_report.py \
  --db "C:/Users/Administrator/Desktop/ai/Tggruup/tgfarm.db" \
  --run-id b33eee8584          # последний полный цикл на момент замера
```
До правки `4f1d6c4` этот же запуск печатал «s1_sources … absent» рядом со
строкой `s1_collect ok` — так выглядит потерянный этап в отчёте.

A3. Read-only зонды базы (заменить путь и выполнить `sqlite3 "file:...?mode=ro"`):
```sql
SELECT stage, status, COUNT(*), MAX(created_at) FROM pipeline_runs GROUP BY 1,2;
SELECT status, COUNT(*) FROM posts GROUP BY 1;
SELECT coalesce(post_format,'NULL'), COUNT(*) FROM posts GROUP BY 1;
SELECT kind, COUNT(*) FROM media GROUP BY 1;
SELECT platform, status, COUNT(*), MAX(created_at) FROM crosspost_logs GROUP BY 1,2;
SELECT COUNT(*) FROM post_stats; SELECT COUNT(*) FROM learning_memory;
SELECT COUNT(*) FROM editorial_plans;   -- и строки editorial_plan_* журнала: 0
PRAGMA table_info(posts);                -- план-колонок в живой схеме нет
SELECT name FROM sqlite_master WHERE type='table' AND name='videos'; -- пусто
```

A4. Живой лог:
```bash
grep -c "s1_collect\|s2_trends\|s4_generate\|s5_quality\|s6_visual\|s7_publish\|s8_analytics\|s9_crosspost\|s12_learning" farm_serve25.log   # 5300
grep -o "stage [a-z0-9_]* failed" farm_serve25.log | sort | uniq -c                                                                          # s4_generate 83, s5_quality 14
grep -ci "video" farm_serve25.log   # 0 — S11 в этом запуске не звучал
```

A5. Прошиться по швам кода:
```bash
grep -n "_stage(result" tgfarm/pipeline.py
grep -n "add_job" tgfarm/pipeline.py
grep -n "s10_format\|s11_video" tgfarm/s4_generation/__init__.py tgfarm/s9_growth/__init__.py
```

A6. Машинная проверка языка этого файла (репозиторий дважды ловил CJK и
латинские слова внутри русского текста):
```bash
python -c "import io,re,sys; t=io.open('docs/READINESS_MATRIX.md',encoding='utf-8').read(); sys.stdout.write(str(len(re.findall(r'[\u3040-\u30ff\u4e00-\u9fff\uac00-\ud7ff]', t))))"
# ожидается 0
```
