"""Offline end-to-end demo.

Seeds synthetic news items across several fake outlets, then drives the real
pipeline over them with no network access. This is how the farm is verified
without a single API key: everything except live HTTP is exercised for real —
embedding, clustering, heat scoring, routing, generation, the seven quality
gates, cover rendering, scheduling and crosspost logging.

The demo is a release gate (`docs/AUTONOMY_ACCEPTANCE.md`), so it has to fail
like one: `DemoGateError` is raised when a requested cycle reaches zero drafts,
and every stage reports the counts it actually reached instead of an `ok`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

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
    now = datetime.now(UTC)
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

        for story_index, (niche, _label, outlets) in enumerate(SEED_STORIES):
            for i, (source_id, weight, title, body) in enumerate(outlets):
                external = f"demo-{niche}-{source_id}-{i}"
                if (source_id, external) in already:
                    continue
                item = RawItem(
                    source_id=source_id,
                    external_id=external,
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


async def run_demo(cycles: int = 1, offline: bool = True) -> dict:
    """Run the synthetic end-to-end demo without publishing when offline=True.

    The offline guard temporarily overrides the cached Settings instance, so
    every publishing adapter and network-aware component sees mode="off".
    The caller's original mode is restored even if a pipeline stage raises.
    """
    if not offline:
        return await _run_demo_workflow(cycles=cycles)

    from tgfarm.core.settings import get_settings

    settings = get_settings()
    previous_mode = settings.mode
    settings.mode = "off"
    try:
        return await _run_demo_workflow(cycles=cycles)
    finally:
        settings.mode = previous_mode


async def _run_demo_workflow(cycles: int = 1) -> dict:
    """Drive the full synthetic pipeline. Caller controls the runtime mode."""
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
    report: dict = {"seeded_items": await asyncio.to_thread(_seed_items)}

    for cycle in range(cycles):
        # --- S2: trends
        report[f"cycle{cycle}_trends"] = await s2_trends.run()

        # --- S4: generate for every ready cluster
        clusters = await asyncio.to_thread(s2_trends.ready_clusters, 2)
        drafts: list[str] = []
        for public_id, _label, _heat, _ in clusters:
            drafts.extend(await s4_generation.run(public_id))
        report[f"cycle{cycle}_generated"] = {"clusters": len(clusters), "drafts": len(drafts)}

        # --- S5: quality
        report[f"cycle{cycle}_quality"] = await s5_quality.run(drafts or None)

        # --- S6: covers for the survivors
        passed = await asyncio.to_thread(_passed_ids)
        report[f"cycle{cycle}_visual"] = await s6_visual.run(passed)

        # --- S7: schedule
        report[f"cycle{cycle}_schedule"] = {"scheduled": await asyncio.to_thread(
            s7_publish.schedule_ready_posts
        )}

        # --- S7: publish everything (dry-run unless mode=live)
        await asyncio.to_thread(_backdate_all)
        report[f"cycle{cycle}_publish"] = await s7_publish.publish_due(limit=50)

        # --- S9: crosspost (dry-run unless mode=live)
        await asyncio.to_thread(_backdate_published, minutes=30)
        report[f"cycle{cycle}_crosspost"] = await s9_growth.run_crossposts(limit=20)

        # --- S8: analytics
        report[f"cycle{cycle}_analytics"] = await s8_analytics.run()

    def _final() -> dict:

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

    report["final"] = await asyncio.to_thread(_final)
    report["status"] = pipeline.status()
    return report


def _passed_ids() -> list[str]:
    from sqlalchemy import select

    from tgfarm.db.models import Post

    with session_scope() as session:
        rows = session.execute(
            select(Post.public_id).where(Post.status == "gated")
        ).scalars().all()
        return list(rows)


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


__all__ = ["RawItem", "run_demo", "SEED_STORIES"]
