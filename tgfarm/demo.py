"""Offline end-to-end demo.

Seeds synthetic news items across several fake outlets, then drives the real
pipeline over them with no network access. This is how the farm is verified
without a single API key: everything except live HTTP is exercised for real —
embedding, clustering, heat scoring, routing, generation, the seven quality
gates, cover rendering, scheduling and crosspost logging.

The demo is a release gate (`docs/AUTONOMY_ACCEPTANCE.md`), so it has to fail
like one: `DemoGateError` is raised when a requested cycle reaches zero drafts,
and every stage reports the counts it actually reached instead of an `ok`. A
cycle that found no new material is reported as *not proven* rather than
*failed*, because the two zeroes mean different things — and the CLI runs each
invocation against a throwaway store so a repeat `tgfarm demo` is a real run
rather than a judgement of nothing.

The editorial plan is one of those stages now: between the ranked queue and the
draft the demo asks the plan what it makes of the material and records the
answer. On the store the demo owns it runs the scheduler's own two jobs, so the
journal rows and the persisted plan are production ones; on the operator's store
(`--configured-store`) it reads the active plan and takes the dry-run judgement
instead, because superseding a plan and moving a real gated draft is more than a
rehearsal asks for. Either way the plan's counters are numbers that were really
measured: a plan that books nothing says so in words (its entries are `produce`,
which is S4's to generate), a plan over an empty queue is *consulted and nothing
to plan*, and a stage the demo did not run reports nothing beside it at all —
never a zero standing in for a measurement that was never taken.

What is deterministic is the stage trail — items, duplicates, clusters,
promoted, drafts, gated, covers, scheduled and the named holds. It is identical
on every run over a fresh store. The wording of one draft is not: S4 picks an
angle through its own holdout lottery, so `final.sample` is evidence a human may
read, never something the gate asserts on.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from tgfarm.core.logging import get_logger
from tgfarm.core.schemas import RawItem
from tgfarm.db.bootstrap import ensure_bootstrapped
from tgfarm.db.models import RawItem as RawItemRow
from tgfarm.db.session import session_scope
from tgfarm.db.vector import get_index, pack
from tgfarm.llm.embedding import get_embedder

log = get_logger(__name__)

# One demo story = several outlets that cover it independently.
#
# The corpus has to walk a narrow corridor, and both walls are production gates
# the demo must not weaken:
#
#   * dedup (`s1_sources.base.is_content_reprint`, plus the 0.95 cosine hit in
#     `s2_trends._dedup_pass`) flags a document as a reprint of an earlier one.
#     The old corpus pasted one shared body under the headlines, so 11 of 17
#     items were flagged and every cluster was left with a single survivor.
#   * clustering (`s2_trends.same_story_score`) then needs the *headline* tokens
#     to overlap and the embeddings to agree, or the items are not the same story
#     at all and each lands in its own one-item cluster — which
#     `ready_clusters(min_items=2)` rejects just as hard.
#
# So per outlet: the story core is shared in the headline (that is the identity
# signal), while the headline tail and the second half of the body are this
# outlet's own text (that is the independence signal). Distinct documents about
# one event — which is exactly what `heat` rewards and what a reprint storm is
# not. No production threshold was touched to get there.
#
# (niche, label, [(outlet, source_weight, title, body), ...])
SEED_STORIES: list[tuple[str, str, list[tuple[str, float, str, str]]]] = [
    (
        "tech",
        "OpenAI выпустила GPT-5.5",
        [
            (
                "techcrunch_ai",
                0.72,
                "GPT-5.5: 40% задач инженеров решаются без человека — релиз и бенчмарки",
                "OpenAI выпустила GPT-5.5 и заявляет, что 40% типовых задач инженеров "
                "модель доводит до результата без промежуточных правок человека, а цена "
                "инференса ниже прошлого поколения примерно на 60%. Компания открывает "
                "доступ партнёрам через API и обещает опубликовать методику замера "
                "автономности, потому что именно на ней держится главный маркетинговый "
                "тезис релиза. Внутри первые бенчмарки выглядят аккуратно: короткие "
                "задачи с одной правкой действительно почти не требуют вмешательства, а "
                "задачи на рефакторинг нескольких модулей по-прежнему возвращаются "
                "человеку на доработку.",
            ),
            (
                "verge_ai",
                0.70,
                "GPT-5.5: 40% задач инженеров решаются без человека — провал длинных контекстов",
                "По релизу OpenAI модель GPT-5.5 забирает на себя сорок процентов "
                "рутинных инженерных задач и, по обещанию компании, обходится на "
                "шестьдесят процентов дешевле предыдущего поколения по цене инференса. "
                "Независимые "
                "тестировщики подтверждают экономию, но спорят о другом: на окнах свыше "
                "200 тысяч токенов модель теряет нить задачи и возвращается к "
                "начальному плану. Представитель компании не отрицает проблему и "
                "выносит такие сценарии из продакшена до отдельной итерации модели.",
            ),
            (
                "gnews_tech_ru",
                0.62,
                "GPT-5.5: 40% задач инженеров решаются без человека — реакция рынка",
                "Анонс GPT-5.5 компания строит на двух числах: автономное закрытие "
                "40% типовых задач разработчика и экономия 60% на инференсе против "
                "прошлого поколения. На новость первыми "
                "отреагировали хостинговые и аутсорсинговые компании: аналитики "
                "пересчитывают стоимость часа разработки и закладывают снижение маржи. "
                "Открытые веса не обсуждаются, доступ идёт только через API, и часть "
                "команд считает это главным ограничением релиза.",
            ),
        ],
    ),
    (
        "business",
        "ЦБ поднял ключевую ставку до 19%",
        [
            (
                "tass_economics",
                0.75,
                "ЦБ поднял ключевую ставку до 19% и сократил согласование экспортных "
                "контрактов до 3 дней — решение регулятора",
                "Банк России поднял ключевую ставку до 19% годовых и одновременно "
                "сократил срок согласования экспортных контрактов для малого бизнеса с "
                "14 до 3 дней. Регулятор объясняет решение устойчивостью инфляции: "
                "недельный рост цен ускорился третью неделю подряд. Упрощение касается "
                "только экспортных операций и не распространяется на внутренние "
                "расчёты, подчёркивают в ЦБ.",
            ),
            (
                "vedomosti_all",
                0.68,
                "ЦБ поднял ключевую ставку до 19% и сократил согласование экспортных "
                "контрактов до 3 дней — расчёт банков",
                "Ставка 19% и трёхдневное согласование экспортных контрактов вместо "
                "двухнедельного — так выглядит новое решение Банка России для малого "
                "бизнеса. Банки уже пересчитали pricing коротких оборотных "
                "кредитов: ставки по овернайтам выросли в течение суток после заседания. "
                "Выигрывают раньше остальных именно малые экспортёры — сокращение "
                "согласования снимает заметную часть операционных затрат.",
            ),
            (
                "gnews_business_ru",
                0.58,
                "ЦБ поднял ключевую ставку до 19% и сократил согласование экспортных "
                "контрактов до 3 дней — спор экономистов",
                "Жёсткая ставка 19% годовых и одновременно упрощение для экспортёров: "
                "срок согласования контракта сокращён до трёх дней. Экономисты "
                "разделились: одни видят в таком сочетании противоречие, другие "
                "напоминают, что упрощение не является монетарным стимулом и касается "
                "узкой операции. Опрошенные компании пока не меняли планы по найму.",
            ),
        ],
    ),
    (
        "crypto",
        "Ethereum перешёл на L2 по умолчанию",
        [
            (
                "coindesk",
                0.68,
                "Ethereum перешёл на L2 по умолчанию: комиссия упала с 4 до 0,02 доллара — "
                "первый месяц",
                "Ethereum перешёл на L2 по умолчанию, и средняя комиссия за простой "
                "перевод упала примерно с 4 до 0,02 доллара. Протоколы верхнего уровня "
                "уже переносят логику на новые секвенсоры, базовый слой остаётся слоем "
                "урегулирования. По данным сети, доля транзакций, идущих через L2, "
                "выросла до большинства за первые четыре недели после перехода.",
            ),
            (
                "theblock",
                0.60,
                "Ethereum перешёл на L2 по умолчанию: комиссия упала с 4 до 0,02 доллара — "
                "риск централизации",
                "Переход Ethereum в L2 по умолчанию обрушил комиссии: простой перевод "
                "стоит около 0,02 доллара против прежних четырёх. Исследование состояния сети "
                "связывает это падение с ростом централизации: число узлов, принимающих "
                "транзакции перед урегулированием, выросло на 18%, и большая их часть "
                "принадлежит трём операторам. Авторы предупреждают, что отказ одного из "
                "них теперь затрагивает заметную долю оборота.",
            ),
            (
                "gnews_crypto_ru",
                0.60,
                "Ethereum перешёл на L2 по умолчанию: комиссия упала с 4 до 0,02 доллара — "
                "биржи и регуляторы",
                "После того как Ethereum стал по умолчанию работать в L2, переводы "
                "подешевели в десятки раз — примерно до 0,02 доллара. Биржи за считанные дни "
                "добавили вывод через новые сети, и комиссии на биржевых кошельках "
                "сблизились с сетевыми. Одновременно регуляторы нескольких стран "
                "запросили у операторов отчёт о рисках концентрации и о порядке "
                "восстановления при остановке секвенсора.",
            ),
        ],
    ),
    (
        "science",
        "Исследование на 12 400 пациентов",
        [
            (
                "who_news",
                0.82,
                "Исследование на 12 400 пациентов дало снижение сердечно-сосудистого "
                "риска на 21% — итоги наблюдения",
                "Клиническое наблюдение за 12 400 пациентами показало снижение "
                "сердечно-сосудистого риска примерно на 21% при регулярном приёме "
                "препарата. Авторы подчёркивают, что эффект не зависит от исходного "
                "давления, и публикуют полную протокольную документацию вместе с кодом "
                "анализа. Первичный endpoint достигнут раньше запланированного срока.",
            ),
            (
                "sciencedaily_health",
                0.72,
                "Исследование на 12 400 пациентов дало снижение сердечно-сосудистого "
                "риска на 21% — возрастных порогов не найдено",
                "Результат крупного наблюдения (12 400 пациентов) не зависит от возраста: "
                "сердечно-сосудистый риск падал примерно на 21% и у пациентов старше 60 "
                "лет, и в группе 45-59 лет. Побочные точки исследования остались статистически "
                "нейтральными, что авторы считают главным аргументом в пользу широкого "
                "применения.",
            ),
            (
                "nature",
                0.70,
                "Исследование на 12 400 пациентов дало снижение сердечно-сосудистого "
                "риска на 21% — сомнения редакторов",
                "Клиническое наблюдение за 12 400 пациентами показало снижение "
                "сердечно-сосудистого риска примерно на 21% при регулярном приёме "
                "препарата. Несколько независимых статистиков ставят вывод под вопрос: "
                "часть пациентов выбыла из наблюдения до конца срока, и анализ с "
                "дополнением пропущенных случаев даёт меньше 21%. Авторы отвечают, что "
                "первичный анализ зарегистрирован заранее и опубликован вместе с кодом.",
            ),
        ],
    ),
    (
        "health",
        "ВОЗ снизила норму физической активности",
        [
            (
                "who_news",
                0.80,
                "ВОЗ снизила норму физической активности до 150 минут в неделю — "
                "обновлённая рекомендация",
                "Обновлённая рекомендация ВОЗ держит нижнюю границу пользы на уровне 150 "
                "минут умеренной нагрузки в неделю вместо прежних 300. Отдельным пунктом "
                "идёт сокращение сидячего времени до шести часов в сутки. Документ "
                "адресован взрослым без противопоказаний, и в нём отдельно оговорены "
                "беременность и пожилой возраст.",
            ),
            (
                "sciencedaily_health",
                0.72,
                "ВОЗ снизила норму физической активности до 150 минут в неделю — главный "
                "фактор сместился",
                "Обновлённая рекомендация ВОЗ держит нижнюю границу пользы на уровне 150 "
                "минут умеренной нагрузки в неделю вместо прежних 300. Главным "
                "независимым фактором риска названо сидячее время свыше шести часов: "
                "прежняя формулировка про 300 минут оставалась недостижимой для многих "
                "групп, а счёт сидячих часов не требует спортивного оборудования.",
            ),
            (
                "gnews_health_ru",
                0.60,
                "ВОЗ снизила норму физической активности до 150 минут в неделю — клиники "
                "готовятся",
                "Норма физической активности от ВОЗ теперь звучит так: 150 минут умеренной "
                "нагрузки в неделю вместо прежних 300. Терапевтические "
                "клиники переписывают памятки пациентам, кардиологи добавляют, что "
                "главное в новой формулировке — счёт сидячих часов, а не темп шагов. "
                "Страховщики пока не меняли условия программ профилактики.",
            ),
        ],
    ),
    (
        "software",
        "Figma выпустила ИИ-редактор",
        [
            (
                "techcrunch_ai",
                0.70,
                "Figma выпустила ИИ-редактор: прототип интерфейса из текста за 90 секунд — "
                "интервью с командой",
                "Черновик интерфейса за полторы минуты из одного текстового описания — так "
                "работает новый ИИ-редактор Figma, показанный на релизе. Команда "
                "показывает на демо, как текст доходит до макета, но честно отказывается "
                "генерировать продакшен-код. Экспорт в код остаётся в закрытой бете и "
                "выдаётся по заявкам продуктовых команд.",
            ),
            (
                "gnews_tech_ru",
                0.62,
                "Figma выпустила ИИ-редактор: прототип интерфейса из текста за 90 секунд — "
                "кто попал в бету",
                "Новый ИИ-редактор Figma превращает текстовое описание в макет примерно за "
                "полторы минуты, не выходя за рамки базовой сетки. Закрытая "
                "бета оставила за бортом часть русскоязычных команд: заявки из "
                "отдельных регионов не проходят модерацию. Обсуждение идёт уже не про "
                "90 секунд, а про то, кто получает доступ первым.",
            ),
            (
                "designweek_ru",
                0.66,
                "Figma выпустила ИИ-редактор: прототип интерфейса из текста за 90 секунд — "
                "реакция дизайнеров",
                "Figma показала генератор прототипов: черновик интерфейса собирается из "
                "текстового описания примерно за полторы минуты. Дизайнеры "
                "на форумах спорят о том, что черновик макета перестал быть ручной "
                "работой: ценность смещается в проверку и правку. Разработчики плагинов "
                "просят открыть бета-API раньше публичного релиза.",
            ),
        ],
    ),
]


def _corpus(now: datetime) -> list[RawItem]:
    """The synthetic corpus as S1 would hand it over: one `RawItem` per outlet.

    Separate from the write because the corpus is a claim about production
    behaviour, and a claim nobody can measure is a claim nobody can keep: the
    tests read it with the ingest module's own predicates instead of re-typing
    the headlines, and the store sees exactly the same documents.
    """
    items: list[RawItem] = []
    for story_index, (niche, _label, outlets) in enumerate(SEED_STORIES):
        for i, (source_id, weight, title, body) in enumerate(outlets):
            items.append(
                RawItem(
                    source_id=source_id,
                    external_id=f"demo-{niche}-{source_id}-{i}",
                    url=f"https://demo.invalid/{niche}/{source_id}/{i}",
                    title=title,
                    text=body,
                    lang="ru",
                    # every outlet of a story sits inside the current interest
                    # window, so the demo is judged on the shape a fresh
                    # multi-outlet story has in production; the per-outlet offset
                    # fixes the walk order instead of leaving it to insertion time
                    published_at=now - timedelta(minutes=11 * i + story_index),
                    niche_hint=[niche],
                    source_weight=weight,
                )
            )
    return items


def _seed_items() -> int:
    """Write synthetic items straight into the store, mirroring what S1 produces.

    "Mirroring S1" is the load-bearing part: the fingerprint and the embedding are
    written with the ingest module's own recipes (`fingerprint_of` over title +
    text, and `RawItem.blend`), not with ad-hoc ones. A demo that fingerprints
    differently from the farm tests a different farm: the old corpus hashed only
    the headline, which is exactly the half the reprint gate compares.
    """
    from tgfarm.s1_sources.base import fingerprint_of

    embedder = get_embedder()
    index = get_index(embedder.dim)
    written = 0

    with session_scope() as session:
        # external ids are deterministic, so a second demo run would violate the
        # uniqueness constraint; seeding has to be idempotent
        already = {
            (source_id, external_id)
            for source_id, external_id in session.execute(
                select(RawItemRow.source_id, RawItemRow.external_id).where(
                    RawItemRow.external_id.like("demo-%")
                )
            )
        }

        for item in _corpus(datetime.now(UTC)):
            if (item.source_id, item.external_id) in already:
                continue
            vec = embedder.embed(item.blend)
            row = RawItemRow(
                source_id=item.source_id,
                external_id=item.external_id,
                url=item.url,
                title=item.title,
                text=item.text,
                lang=item.lang,
                published_at=item.published_at,
                niche_hint=item.niche_hint,
                source_weight=item.source_weight,
                embedding=pack(vec),
                simhash=fingerprint_of(item.title, item.text),
            )
            session.add(row)
            session.flush()
            index.add(row.id, vec)
            written += 1

    log.info("demo: seeded %d synthetic items across %d stories", written, len(SEED_STORIES))
    return written


def _stored_demo_items() -> int:
    """How many synthetic documents the store holds, seeded now or earlier.

    `seeded_items` alone cannot tell an operator what the gate had to read from:
    a second run on the same database seeds 0 because seeding is idempotent, and
    `seeded=0` next to `drafts=0` reads like an empty store.
    """
    from sqlalchemy import func

    with session_scope() as session:
        return int(
            session.execute(
                select(func.count()).select_from(RawItemRow).where(
                    RawItemRow.external_id.like("demo-%")
                )
            ).scalar_one()
        )


def _fresh_demo_items() -> int:
    """Demo documents S2 can still act on, before this cycle runs.

    `drafts=0` has two causes that look identical from the outside and mean
    opposite things: the chain is broken, or the store holds a demo corpus an
    earlier run already consumed. Clustering only takes items with no
    `cluster_id` and no `duplicate_of`, and seeding is idempotent, so a repeat
    run on the same database has nothing to work on. This count tells them apart.
    """
    from sqlalchemy import func

    with session_scope() as session:
        return int(
            session.execute(
                select(func.count()).select_from(RawItemRow).where(
                    RawItemRow.external_id.like("demo-%"),
                    RawItemRow.cluster_id.is_(None),
                    RawItemRow.duplicate_of.is_(None),
                )
            ).scalar_one()
        )


class DemoGateError(RuntimeError):
    """A requested demo cycle did not reach a draft, a gate verdict and a slot.

    The demo is the release gate named in `docs/AUTONOMY_ACCEPTANCE.md`, so its
    exit status is part of its output: a run that seeded items, clustered them
    and then produced nothing must not read as a successful cycle. Raising makes
    the CLI exit non-zero and puts the stage trail it really reached in front of
    the operator; the full report travels on `.report`.
    """

    def __init__(self, message: str, *, report: dict) -> None:
        super().__init__(message)
        self.report = report


def _stage_counts(
    *,
    seeded: int,
    stored: int,
    fresh: int,
    trends: dict,
    plan: dict,
    ready_clusters: int,
    drafts: int,
    quality: dict,
    visual: dict,
    scheduled: int,
    schedule_holds: dict,
    publish: dict,
    crosspost: dict,
) -> dict:
    """The numbers each stage really reached; none of them is a default."""
    return {
        "seeded_items": seeded,
        "stored_items": stored,
        "fresh_items": fresh,
        "duplicates": int(trends.get("duplicates") or 0),
        "clusters": int(trends.get("clusters") or 0),
        "promoted": int(trends.get("promoted") or 0),
        # the plan's counters arrive already judged — None where the stage
        # measured nothing — so they are copied, not coerced into zeros here
        **plan,
        "ready_clusters": ready_clusters,
        "drafts": drafts,
        "gated": int(quality.get("passed") or 0),
        "rejected": int(quality.get("rejected") or 0),
        "deferred": int(quality.get("deferred") or 0),
        "covers": int(visual.get("rendered") or 0),
        "inline_images": int(visual.get("inline") or 0),
        "scheduled": scheduled,
        "schedule_holds": dict(schedule_holds),
        "published": int(publish.get("published") or 0),
        "dry_run": int(publish.get("dry_run") or 0),
        "publish_holds": dict(publish.get("gates") or {}),
        "crosspost": dict(crosspost),
    }


def _named_holds(holds: dict) -> dict:
    """Only the gates that actually held something back, with their counts."""
    return {reason: int(count) for reason, count in (holds or {}).items() if count}


def _hold_note(holds: dict) -> str:
    named = _named_holds(holds)
    if not named:
        return "no hold reason reported"
    return "held: " + ", ".join(f"{reason}={count}" for reason, count in sorted(named.items()))


# ---------------------------------------------------------------- editorial plan
#: The two ways the demo can meet the plan, named after the store it stands on.
PLAN_EXECUTED = "executed"
PLAN_READ_ONLY = "read-only"
#: The endings a plan look can have. They ride in the trail because "the plan
#: judged nothing" and "the plan was never asked" both print as a zero and mean
#: opposite things — the distinction P34 was filed for.
PLAN_BUILT = "built"
PLAN_BUILT_EMPTY = "built-empty"
PLAN_READ_ACTIVE = "read-active"
PLAN_READ_NONE = "read-none"
PLAN_SKIPPED = "skipped"
PLAN_ERROR = "error"
#: statuses that reached the plan itself; every other one says the plan was not
#: read, and then no counter below may carry a number.
PLAN_CONSULTED = frozenset({PLAN_BUILT, PLAN_BUILT_EMPTY, PLAN_READ_ACTIVE, PLAN_READ_NONE})
#: a status that printed a zero because it measured one, not because it skipped.
PLAN_MEASURED_ZEROS = frozenset({PLAN_BUILT, PLAN_BUILT_EMPTY, PLAN_READ_ACTIVE})


def _plan_int(value: object) -> int | None:
    """The number the plan recorded, or None — never a defaulted 0."""
    return None if value is None else int(value)


async def _plan_look(*, writes: bool, moment: datetime | None = None) -> dict:
    """Ask the editorial plan what it makes of the queue, and keep what it said.

    Two modes, because two stores. On the throwaway file the demo owns — what
    `tgfarm demo` runs by default — it drives the scheduler's own jobs in their
    own order: `build_plan_tick` persists the snapshot and journals it, then
    `apply_plan_tick` judges every entry and hands the qualified ones to the
    scheduling transition, journalling the reasons. The executor's switch is
    forced on for that run only (by `_run_demo_workflow`), because a rehearsal
    that never asks the editor proves a pipeline with no editorial decision in it.

    On a store the demo does not own the writes are withheld — no snapshot that
    supersedes the operator's active plan, no gated draft moved by a demo — and
    the read is the honest one instead: `get_active_plan` for what the store
    really holds, `evaluate_plan` for the judgement it would reach. `booked`
    stays None there, because a reading books nothing.
    """
    from tgfarm import plan_execution
    from tgfarm.core.settings import get_settings
    from tgfarm.editorial_plan import get_active_plan

    look: dict = {
        "mode": PLAN_EXECUTED if writes else PLAN_READ_ONLY,
        "status": PLAN_SKIPPED,
        "flag": bool(getattr(get_settings(), "plan_execution_enabled", False)),
        "public_id": None,
        "entries": None,
        "produce": None,
        "schedule": None,
        "hold": None,
        "scanned": None,
        "booked": None,
        "held": None,
        "reasons": None,
        "note": "",
        "build": None,
        "judgement": None,
    }
    try:
        if writes:
            built = await plan_execution.build_plan_tick(now=moment)
            look["build"] = {key: built.get(key) for key in ("skipped", "error") if key in built}
            if "error" in built:
                look.update(status=PLAN_ERROR, note=str(built["error"])[:200])
                return look
            if "skipped" in built:
                look.update(status=PLAN_SKIPPED, note=f"build skipped: {built['skipped']}")
                return look
            summary = built.get("summary") or {}
            look.update(
                status=PLAN_BUILT if summary.get("total") else PLAN_BUILT_EMPTY,
                public_id=built.get("public_id"),
                entries=_plan_int(summary.get("total")),
                produce=_plan_int(summary.get("produce")),
                schedule=_plan_int(summary.get("schedule")),
                hold=_plan_int(summary.get("hold")),
            )
            judged = await plan_execution.apply_plan_tick(now=moment)
            look["judgement"] = {
                key: judged.get(key) for key in ("skipped", "error") if key in judged
            }
            if "error" in judged:
                look.update(status=PLAN_ERROR, note=f"apply raised: {judged['error']}"[:200])
                return look
            if "skipped" in judged:
                look.update(status=PLAN_SKIPPED, note=f"apply skipped: {judged['skipped']}")
                return look
            # a real apply carries no `scanned` key — the number of entries it
            # looked at is the length of its `entries` list, exactly the way
            # `apply_plan_tick` journals it; reading a missing key as a zero was
            # the old bug this line refuses to repeat.
            look.update(
                scanned=_plan_int(len(judged.get("entries") or [])),
                booked=_plan_int(judged.get("applied")),
                held=_plan_int(judged.get("held")),
                reasons=dict(judged.get("reasons") or {}),
            )
            return look

        active = await asyncio.to_thread(get_active_plan)
        if active is None:
            # None is the plan's own way of saying "never built here"; the caller
            # must show that, and must not turn it into a plan of zero entries.
            look.update(status=PLAN_READ_NONE, note="this store holds no active plan to read")
            return look
        summary = active.get("summary") or {}
        look.update(
            status=PLAN_READ_ACTIVE,
            public_id=active.get("public_id"),
            entries=_plan_int(summary.get("total")),
            produce=_plan_int(summary.get("produce")),
            schedule=_plan_int(summary.get("schedule")),
            hold=_plan_int(summary.get("hold")),
        )
        judged = await asyncio.to_thread(plan_execution.evaluate_plan)
        look["judgement"] = {"dry_run": True}
        look.update(
            scanned=_plan_int(judged.get("scanned")),
            held=_plan_int(judged.get("held")),
            reasons=dict(judged.get("reasons") or {}),
        )
        return look
    except Exception as exc:  # noqa: BLE001 - a plan failure is a stage outcome, not a crash
        message = f"{type(exc).__name__}: {exc}"
        log.warning("demo: the editorial plan raised %s", message[:200])
        look.update(status=PLAN_ERROR, note=message[:200])
        return look


def _plan_note(look: dict) -> str:
    """One line saying what the plan stage actually was, in the report's words."""
    status = look["status"]
    if status == PLAN_BUILT:
        base = (
            f"{look['entries']} entries judged ({look['produce']} produce, "
            f"{look['schedule']} schedule, {look['hold']} hold)"
        )
        if look["booked"]:
            return f"{base}; {look['booked']} booked to scheduling"
        return (
            f"{base}; booked nothing here — a produce entry is S4's to generate, "
            "so this is a measured zero, not a failure"
        )
    if status == PLAN_BUILT_EMPTY:
        return "the plan was consulted over an empty queue: nothing to plan"
    if status == PLAN_READ_ACTIVE:
        return (
            f"{look['entries']} entries read from this store's active plan "
            f"({look['produce']} produce, {look['schedule']} schedule, "
            f"{look['hold']} hold); judgement only, no booking"
        )
    if status == PLAN_READ_NONE:
        return (
            "the plan was read and this store holds none: it was never built here, "
            "and the demo does not build a plan on a store it does not own"
        )
    if status == PLAN_SKIPPED:
        return f"the plan declined to run: {look['note'] or 'the executor switch was off'}"
    return f"the plan raised: {look['note']}"


def _plan_counters(look: dict) -> dict:
    """The plan's numbers for the stage trail; None where nothing was measured."""
    consulted = look["status"] in PLAN_CONSULTED
    measured = look["status"] in PLAN_MEASURED_ZEROS
    counts: dict = {
        "plan_mode": look["mode"],
        "plan_status": look["status"],
        "plan_consulted": consulted,
        "plan_flag": look["flag"],
        "plan_note": look["note"] or _plan_note(look),
    }
    for key, name in (
        ("plan_entries", "entries"),
        ("plan_produce", "produce"),
        ("plan_schedule", "schedule"),
        ("plan_hold", "hold"),
        ("plan_scanned", "scanned"),
        ("plan_booked", "booked"),
        ("plan_held", "held"),
        ("plan_reasons", "reasons"),
    ):
        counts[key] = look[name] if measured else None
    return counts


def _plan_verdict(cycle: int, look: dict) -> str:
    """The sentence the verdict carries for this cycle's plan stage."""
    return f"cycle {cycle}: editorial plan {_plan_note(look)}"


def _trail_count(value: object) -> str:
    """A counter in the failure trail: the number, or `?` for an unmeasured one.

    The trail is what an operator reads when the gate fails; printing `0` for a
    stage that never ran would put a measurement in front of them that was never
    taken — the exact thing this file's plan keys refuse to do.
    """
    return "?" if value is None else str(value)


def _log_stages(cycle: int, stages: dict) -> None:
    """One line per stage with the count it reached.

    A zero is only a warning when nothing explains it. `duplicates: 0` is a
    measurement, and a publish stage that sent nothing because the farm is not
    live is a decision with a name attached — both read differently from a stage
    that quietly did no work, and the demo must not blur them into "ok".
    """
    schedule_holds = _named_holds(stages["schedule_holds"])
    publish_holds = _named_holds(stages["publish_holds"])
    publish_detail = f"dry_run={stages['dry_run']} {_hold_note(stages['publish_holds'])}"
    rows = (
        (
            "corpus",
            stages["stored_items"],
            f"{stages['seeded_items']} seeded this run, {stages['fresh_items']} new for S2",
            False,
        ),
        ("s2 reprints", stages["duplicates"], "documents excluded as copies", True),
        ("s2 clusters", stages["clusters"], "stories grouped", False),
        ("s2 promoted", stages["promoted"], "clusters above the heat threshold", False),
        (
            "plan",
            stages["plan_entries"] or 0,
            stages["plan_note"],
            stages["plan_status"] in PLAN_CONSULTED,
        ),
        ("s4 drafts", stages["drafts"], f"from {stages['ready_clusters']} ready clusters", False),
        ("s5 gated", stages["gated"], "drafts through the quality gate", False),
        ("s6 covers", stages["covers"], "posts with primary media", False),
        ("s7 scheduled", stages["scheduled"], _hold_note(stages["schedule_holds"]),
         bool(schedule_holds)),
        ("s7 published", stages["published"], publish_detail,
         bool(publish_holds) or stages["dry_run"] > 0),
    )
    for name, count, detail, zero_explained in rows:
        if count:
            log.info("demo c%d %-16s %d  (%s)", cycle, name, count, detail)
        elif zero_explained:
            log.info("demo c%d %-16s 0  (%s)", cycle, name, detail)
        else:
            log.warning("demo c%d %-16s NOTHING  (%s)", cycle, name, detail)


def _gate_failures(cycle: int, stages: dict) -> list[str]:
    """Where this cycle stopped doing work, stated as counts.

    Three things must hold for a demo cycle to count as a passed gate: S4
    produced a draft, S5 gave it a verdict and left something alive, and S7 made
    a named scheduling decision — a slot, or a hold reason that says why there
    is no slot. Zero drafts is always fatal; so are zero survivors and a
    scheduler that says nothing, because both are indistinguishable from a
    healthy queue from the outside. The editorial plan adds one failure of its
    own: a plan that raised, or a plan the writing demo forced the switch on for
    and that still declined to run. A plan that found nothing to plan, or booked
    nothing because its entries belong to S4, is *not* a failure — an empty
    reading is a reading, and a produce entry that was never the plan's to run is
    a measurement of zero, not a break.
    """
    trail = (
        f"cycle {cycle}: seeded={stages['seeded_items']} stored={stages['stored_items']} "
        f"fresh={stages['fresh_items']} dup={stages['duplicates']} "
        f"clusters={stages['clusters']} promoted={stages['promoted']} "
        f"ready={stages['ready_clusters']} "
        f"plan={stages['plan_status']} plan_entries={_trail_count(stages['plan_entries'])} "
        f"plan_booked={_trail_count(stages['plan_booked'])} "
        f"drafts={stages['drafts']} "
        f"gated={stages['gated']} covers={stages['covers']} "
        f"scheduled={stages['scheduled']}"
    )
    # The plan's own failures come first and are kept whatever the draft count
    # says: a cycle whose plan crashed is broken for a different reason than a
    # cycle that had no material, and the second must not hide the first.
    plan_failures: list[str] = []
    if stages["plan_status"] == PLAN_ERROR:
        plan_failures.append(
            f"{trail} -> DEMO GATE FAILED: the editorial plan raised: {stages['plan_note']}"
        )
    elif stages["plan_status"] == PLAN_SKIPPED and stages["plan_mode"] == PLAN_EXECUTED:
        plan_failures.append(
            f"{trail} -> DEMO GATE FAILED: the editorial plan declined to run "
            f"({stages['plan_note'] or 'the executor switch was off'}), so the cycle "
            "judged no editorial decision"
        )
    failures: list[str] = list(plan_failures)
    if not stages["drafts"]:
        # Two different zeroes. Material to work on and no draft is a broken
        # chain; so is a store that holds no demo corpus at all. A spent corpus
        # (stored but nothing new for S2) is not a failure — it is a run that
        # judged nothing, and it must be named as that rather than as a break.
        if stages["fresh_items"] or not stages["stored_items"]:
            return plan_failures + [f"{trail} -> DEMO GATE FAILED: no draft was generated"]
        return plan_failures + [
            f"{trail} -> DEMO GATE NOT PROVEN: the store held no new demo material "
            "for S2, so this cycle had nothing to judge (run against an empty store)"
        ]
    if not stages["gated"]:
        failures.append(f"{trail} -> DEMO GATE FAILED: no draft passed the quality gate")
    if not stages["scheduled"] and not _named_holds(stages["schedule_holds"]):
        failures.append(
            f"{trail} -> DEMO GATE FAILED: scheduling decided nothing "
            "and named no hold reason"
        )
    return failures


def _enforce_gate(report: dict) -> dict:
    """Turn a failed verdict into a raised named error; the caller exits non-zero."""
    verdict = report.get("verdict") or {}
    if verdict.get("ok") is False:
        raise DemoGateError(verdict.get("reason") or "demo gate failed", report=report)
    return report


@contextmanager
def _throwaway_store() -> Iterator[Path]:
    """Point the engine at a private SQLite file for the duration of the run.

    Only `DATABASE_URL` moves: everything else (including a live `.env` next to
    the working directory) is read exactly as the farm reads it, which is what
    the acceptance row "offline demo in a live-configured environment" asks for.
    The store itself has to move, because seeding is idempotent and a repeat run
    on the same file has no material left — a gate that judges nothing must not
    be run against a spent corpus.

    The vector index is one process-wide object keyed by row id, and demo ids
    collide with the real store's, so it is emptied on the way in and on the way
    out. That is what a CLI process wants; a test that needs the shared index
    should point at its own database instead of isolating here.
    """
    from tgfarm.core import settings as settings_mod
    from tgfarm.db import session as session_mod
    from tgfarm.llm import embedding as embedding_mod

    def _swap(url: str | None) -> None:
        if url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = url
        settings_mod.reset_settings_cache()
        session_mod.dispose_engine()
        embedding_mod.reset_embedder()
        get_index().clear()

    directory = Path(tempfile.mkdtemp(prefix="tgfarm-demo-"))
    previous_url = os.environ.get("DATABASE_URL")
    _swap(f"sqlite:///{(directory / 'tgfarm-demo.db').as_posix()}")
    try:
        yield directory
    finally:
        _swap(previous_url)


async def run_demo(
    cycles: int = 1,
    offline: bool = True,
    strict: bool = True,
    isolated: bool = False,
) -> dict:
    """Run the synthetic end-to-end demo without publishing when offline=True.

    The offline guard temporarily overrides the cached Settings instance, so
    every publishing adapter and network-aware component sees mode="off".
    The caller's original mode is restored even if a pipeline stage raises.

    `strict=True` (what the CLI uses) turns a cycle that reached no draft into a
    `DemoGateError`, so the release gate cannot exit 0 while having produced
    nothing. `strict=False` returns the same honest report for callers that want
    to read the stage counts themselves.

    `isolated=True` (also what the CLI uses by default) runs against a throwaway
    database under the system temp dir and reports where it lives as
    `report["store"]`, so an invocation is repeatable and synthetic rows never
    enter the operator's store. `--configured-store` on the CLI opts out, and
    with it the editorial plan is read rather than rewritten: the demo only
    persists and books on a store it owns.
    """

    async def _workflow() -> dict:
        def _judge(report: dict) -> dict:
            return _enforce_gate(report) if strict else report

        if not offline:
            return _judge(await _run_demo_workflow(cycles=cycles, plan_writes=isolated))

        from tgfarm.core.settings import get_settings

        settings = get_settings()
        previous_mode = settings.mode
        settings.mode = "off"
        try:
            report = await _run_demo_workflow(cycles=cycles, plan_writes=isolated)
        finally:
            settings.mode = previous_mode
        return _judge(report)

    if not isolated:
        return await _workflow()

    with _throwaway_store() as directory:
        store = {"kind": "throwaway", "path": str(directory)}
        try:
            report = await _workflow()
        except DemoGateError as exc:
            exc.report["store"] = store
            raise
    report["store"] = store
    return report


async def _run_demo_workflow(cycles: int = 1, plan_writes: bool = False) -> dict:
    """Drive the full synthetic pipeline. Caller controls the runtime mode.

    `plan_writes=True` says the store under this run belongs to the demo, so the
    editorial plan may be persisted and acted on for real; with it False the plan
    is only read. The executor's switch is forced on for the duration of a
    writing run and given back afterwards, exactly as the offline run gives back
    `mode` — a rehearsal that quietly skips the editor is not a rehearsal. On the
    read-only run the switch is left exactly as the operator set it, because the
    demo has no business rewriting a plan it does not own.
    """
    from tgfarm.core.settings import get_settings

    settings = get_settings()
    previous_plan_flag = settings.plan_execution_enabled
    if plan_writes and not previous_plan_flag:
        settings.plan_execution_enabled = True
    try:
        return await _run_demo_cycles(cycles=cycles, plan_writes=plan_writes)
    finally:
        settings.plan_execution_enabled = previous_plan_flag


async def _run_demo_cycles(cycles: int, plan_writes: bool) -> dict:
    """The walk itself, on the mode and the plan switch the caller arranged."""
    from tgfarm import (
        pipeline,
        s2_trends,
        s4_generation,
        s5_quality,
        s6_visual,
        s7_publish,
        s8_analytics,
        s9_growth,
    )

    await asyncio.to_thread(ensure_bootstrapped)
    seeded = await asyncio.to_thread(_seed_items)
    report: dict = {"seeded_items": seeded}
    failures: list[str] = []
    plan_lines: list[str] = []

    for cycle in range(cycles):
        # measured before S2 runs: this is the material the chain is being
        # judged on, and after the pass it is gone (items get a cluster_id).
        fresh = await asyncio.to_thread(_fresh_demo_items)
        # --- S2: trends
        trends = await s2_trends.run()
        report[f"cycle{cycle}_trends"] = trends

        # --- the editorial plan, between the ranked queue and the draft.
        # The scheduler's own two jobs in its own order, on one instant, so the
        # booking decision is judged against the same clock the entries were
        # planned against. `plan_writes` decides whether this run may persist
        # and book at all; the reading it withholds is reported as such.
        moment = datetime.now(UTC).replace(microsecond=0)
        plan_look = await _plan_look(writes=plan_writes, moment=moment)
        report[f"cycle{cycle}_plan"] = plan_look

        # --- S4: generate for every ready cluster
        clusters = await asyncio.to_thread(s2_trends.ready_clusters, 2)
        drafts: list[str] = []
        for public_id, _label, _heat, _ in clusters:
            drafts.extend(await s4_generation.run(public_id))
        report[f"cycle{cycle}_generated"] = {"clusters": len(clusters), "drafts": len(drafts)}

        # --- S5: quality
        quality = await s5_quality.run(drafts or None)
        report[f"cycle{cycle}_quality"] = quality

        # --- S6: covers for the survivors
        passed = await asyncio.to_thread(_passed_ids)
        visual = await s6_visual.run(passed)
        report[f"cycle{cycle}_visual"] = visual

        # --- S7: schedule, keeping the reason when it schedules nothing.
        # `schedule_ready_posts` names its gates through the `blocked`
        # out-parameter; without it `scheduled: 0` is the same blind spot the
        # publish stage already closed.
        schedule_holds: dict[str, int] = {}
        scheduled = await asyncio.to_thread(
            s7_publish.schedule_ready_posts, 200, schedule_holds
        )
        report[f"cycle{cycle}_schedule"] = {"scheduled": scheduled, "holds": schedule_holds}

        # --- S7: publish everything (dry-run unless mode=live)
        await asyncio.to_thread(_backdate_all)
        publish = await s7_publish.publish_due(limit=50)
        report[f"cycle{cycle}_publish"] = publish

        # --- S9: crosspost (dry-run unless mode=live)
        await asyncio.to_thread(_backdate_published, minutes=30)
        crosspost = await s9_growth.run_crossposts(limit=20)
        report[f"cycle{cycle}_crosspost"] = crosspost

        # --- S8: analytics
        report[f"cycle{cycle}_analytics"] = await s8_analytics.run()

        stages = _stage_counts(
            seeded=seeded,
            stored=await asyncio.to_thread(_stored_demo_items),
            fresh=fresh,
            trends=trends,
            plan=_plan_counters(plan_look),
            ready_clusters=len(clusters),
            drafts=len(drafts),
            quality=quality,
            visual=visual,
            scheduled=scheduled,
            schedule_holds=schedule_holds,
            publish=publish,
            crosspost=crosspost,
        )
        report[f"cycle{cycle}_stages"] = stages
        _log_stages(cycle, stages)
        plan_lines.append(_plan_verdict(cycle, plan_look))
        failures.extend(_gate_failures(cycle, stages))

    report["final"] = await asyncio.to_thread(_final_stats)
    report["status"] = pipeline.status()
    report["verdict"] = {
        "ok": not failures,
        "cycles": cycles,
        "failures": failures,
        "plan": "; ".join(plan_lines),
        "reason": "; ".join(failures)[:400],
    }
    return report


def _passed_ids() -> list[str]:
    from sqlalchemy import select

    from tgfarm.db.models import Post

    with session_scope() as session:
        rows = session.execute(
            select(Post.public_id).where(Post.status == "gated")
        ).scalars().all()
        return list(rows)


def _final_stats() -> dict:
    from tgfarm.db.models import Media, Post

    with session_scope() as session:
        posts = session.execute(select(Post)).scalars().all()
        media_post_ids = {
            row[0]
            for row in session.execute(
                select(Media.post_id).where(Media.is_primary.is_(True))
            ).all()
        }
        return {
            "posts_total": len(posts),
            "posts_published": sum(1 for p in posts if p.status == "published"),
            "posts_rejected": sum(1 for p in posts if p.status == "rejected"),
            "posts_with_cover": sum(1 for p in posts if p.id in media_post_ids),
            "sample": [
                {
                    "id": p.public_id,
                    "channel": p.channel_id,
                    "angle": p.angle,
                    "status": p.status,
                    "chars": p.char_len,
                    "hook": p.hook[:90],
                    "reason": (p.quality or {}).get("reason"),
                }
                for p in posts[:12]
            ],
        }


def _backdate_all() -> None:
    """Make every scheduled post due, so the demo exercises the publish path."""
    from sqlalchemy import select

    from tgfarm.db.models import Post

    with session_scope() as session:
        rows = session.execute(select(Post).where(Post.status == "scheduled")).scalars().all()
        for row in rows:
            row.scheduled_at = datetime.now(UTC) - timedelta(minutes=1)


def _backdate_published(minutes: int = 30) -> None:
    """Age published posts past the crosspost delay so S9 has work to do."""
    from sqlalchemy import select

    from tgfarm.db.models import Post

    with session_scope() as session:
        rows = session.execute(select(Post).where(Post.status == "published")).scalars().all()
        for row in rows:
            row.published_at = datetime.now(UTC) - timedelta(minutes=minutes)


__all__ = ["DemoGateError", "RawItem", "run_demo", "SEED_STORIES"]
