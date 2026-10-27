"""Offline end-to-end demo.

Seeds synthetic news items across several fake outlets, then drives the real
pipeline over them with no network access. This is how the farm is verified
without a single API key: everything except live HTTP is exercised for real —
embedding, clustering, heat scoring, routing, generation, the seven quality
gates, cover rendering, scheduling and crosspost logging.
"""

from __future__ import annotations

import asyncio
import random
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from tgfarm.core.logging import get_logger
from tgfarm.core.schemas import RawItem
from tgfarm.core.text import simhash
from tgfarm.db.bootstrap import ensure_bootstrapped
from tgfarm.db.models import RawItem as RawItemRow
from tgfarm.db.session import session_scope
from tgfarm.db.vector import get_index, pack
from tgfarm.llm.embedding import get_embedder

log = get_logger(__name__)

# (niche, story seed, outlet, weight, extra headline suffix)
SEED_STORIES: list[tuple[str, str, list[tuple[str, float]]]] = [
    (
        "tech",
        "OpenAI выпустила GPT-5.5: 40% задач инженеров выполняются без человека, "
        "модель дешевле на 60%, но тесты на длинных контекстах провалились",
        [
            ("techcrunch_ai", 0.70),
            ("verge_ai", 0.70),
            ("gnews_tech_ru", 0.62),
            ("mittr_ai", 0.72),
        ],
    ),
    (
        "business",
        "ЦБ поднял ключевую ставку до 19% и упростил правила для малого бизнеса: "
        "экспортные контракты теперь согласуются за 3 дня вместо 14",
        [
            ("tass_economics", 0.75),
            ("vedomosti_all", 0.68),
            ("gnews_business_ru", 0.58),
        ],
    ),
    (
        "crypto",
        "Ethereum перешёл на L2 по умолчанию: комиссия упала с $4 до $0.02, "
        "но централизация узлов выросла на 18%",
        [
            ("coindesk", 0.68),
            ("theblock", 0.60),
            ("gnews_crypto_ru", 0.60),
        ],
    ),
    (
        "science",
        "Клиническое исследование на 12 400 пациентах подтвердило снижение "
        "сердечно-сосудистого риска на 21% при регулярном приёме препарата",
        [
            ("who_news", 0.82),
            ("sciencedaily_health", 0.72),
            ("who_news", 0.80),
        ],
    ),
    (
        "health",
        "ВОЗ обновила рекомендации по физической активности: 150 минут в неделю "
        "умеренной нагрузки вместо 300, сокращение сидячего времени до 6 часов",
        [
            ("who_news", 0.80),
            ("sciencedaily_health", 0.72),
        ],
    ),
    (
        "software",
        "Figma выпустила ИИ-редактор: прототип интерфейса собирается из текстового "
        "описания за 90 секунд, экспорт в код пока в закрытой бете",
        [
            ("techcrunch_ai", 0.70),
            ("gnews_tech_ru", 0.62),
        ],
    ),
]


def _seed_items() -> int:
    """Write synthetic items straight into the store, mirroring what S1 produces."""
    embedder = get_embedder()
    index = get_index(embedder.dim)
    rng = random.Random(20261012)
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

        for niche, story, outlets in SEED_STORIES:
            for i, (source_id, weight) in enumerate(outlets):
                # each outlet rewrites the headline slightly — the clusterer must
                # still collapse them, which is the whole point of S2
                suffix = rng.choice(
                    [
                        ", разбор и цифры",
                        " — что это значит",
                        ". Подробности",
                        "",
                    ]
                )
                title = story.split(":")[0][:150] + suffix
                body = (
                    f"{story}. По данным источника, изменение затронуло основную "
                    f"часть пользователей сегмента. Эксперты отмечают, что эффект "
                    f"сильнее всего заметен в первых неделях после запуска. "
                    f"Отдельные пользователи сообщают о смешанных результатах и "
                    f"просят уточнить условия применения. Разработчики говорят, что "
                    f"инструмент требует ручной проверки на финальном этапе. "
                    f"Источник: {source_id}. "
                ) * 2
                url = f"https://demo.invalid/{niche}/{source_id}/{i}"

                external = f"demo-{niche}-{source_id}-{i}"
                if (source_id, external) in already:
                    continue
                vec = embedder.embed(f"{title}\n{body}")
                row = RawItemRow(
                    source_id=source_id,
                    external_id=external,
                    url=url,
                    title=title,
                    text=body,
                    lang="ru",
                    published_at=now - timedelta(minutes=7 * i),
                    niche_hint=[niche],
                    source_weight=weight,
                    embedding=pack(vec),
                    simhash=simhash(title),
                )
                session.add(row)
                session.flush()
                index.add(row.id, vec)
                written += 1

    log.info("demo: seeded %d synthetic items across %d stories", written, len(SEED_STORIES))
    return written


async def run_demo(cycles: int = 1, offline: bool = True) -> dict:
    """Drive the full pipeline over synthetic data. Returns a stage-by-stage report."""
    from sqlalchemy import select

    from tgfarm import (
        pipeline,
        s1_sources,
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
        _ = s1_sources  # keep the import meaningful: S1 is exercised by `collect`

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
