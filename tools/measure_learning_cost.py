"""Measure what S12's format learning actually costs — on a copy of the live DB.

Wave 5 wired `learn_format_lessons(channel_id)` into `_learn_channel`, so it now
runs for every channel on every cycle. Nobody measured the price. This tool does,
against a *copy* of the production database, and prints the numbers needed to
decide whether the step is a risk inside the 2 700 s cycle budget:

* milliseconds per channel, per window and per pass;
* SQL round trips and rows read into each `format_performance` report;
* how many `format_performance` calls the fleet makes, and how many of them are
  the identical platform-scoped query re-run for another channel of that platform;
* the whole `s12_learning.run()` cost, so format learning can be seen in context;
* the same read against a deliberately grown published set, so the slope (not
  just today's constant) is on record;
* a projection to a bigger fleet and to a wider window.

Usage (the copy is never the production file — the script refuses it):

    cp <live>/tgfarm.db /tmp/learn_cost/tgfarm_copy.db
    python tools/measure_learning_cost.py --db /tmp/learn_cost/tgfarm_copy.db

The copy is written to: `run()` upserts learning rows, and the growth phase
appends tagged synthetic posts and deletes them again, so point this at a
throwaway file only. Reads on the production file would have to be `mode=ro`,
and this script never opens one.

What it found on 2026-11-05 (corpus above, two runs, `--passes 3`):

* one channel costs 5.2-6.9 ms, of which the `format_performance` read is 4-7 ms
  and the rest is one write session per learned format; 11 channels = 58-81 ms;
* the read is bounded by *published posts inside the window*, not by the corpus:
  the plan is `SEARCH posts USING INDEX ix_posts_status`, and 20 003 rows of
  history cost nothing while 452 published rows cost 4 ms;
* widening the window does not widen the work today (all 452 published posts are
  nine days old): 5.2 / 5.6 / 6.7 ms at 60 / 180 / 365 days;
* the cost is linear in that published count: 1 015 posts → 10.6 ms,
  5 015 → 29.9 ms per channel, so 100 channels at 5 000 published each ≈ 3.3 s;
* 9 Telegram channels run the byte-identical platform query 9 times — ~33-46 ms
  of duplicate reads per cycle, which against a 2 700 s budget is not a risk;
* above 32 766 published posts for one platform the S8 read dies with
  `sqlite3.OperationalError: too many SQL variables` (the `IN` list of post ids),
  and `_learn_channel` catches it: the step then writes zero lessons and logs at
  debug. That is a correctness cliff in `tgfarm/s8_analytics/formats.py`, not a
  cost problem here.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

#: The whole-cycle watchdog budget, `tgfarm/pipeline.py`.
CYCLE_BUDGET_S = 2700.0


def _guard_the_copy(path: Path) -> Path:
    """Refuse anything that could be the production database.

    The production file is `tgfarm.db` next to the repo root (and the same name in
    the worktree). A copy has to be a *different* file, so a name collision with
    the live database outside a temp dir is a hard stop rather than a warning.
    """
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        raise SystemExit(f"копия базы не найдена: {resolved}")
    forbidden = {
        (ROOT / "tgfarm.db").resolve(),
        (ROOT.parent.parent / "Tggruup" / "tgfarm.db").resolve(),
        Path("C:/Users/Administrator/Desktop/ai/Tggruup/tgfarm.db").resolve(),
    }
    if resolved in forbidden:
        raise SystemExit(
            "это боевая база: копируй tgfarm.db в отдельный файл и меряй на копии"
        )
    if resolved.name == "tgfarm.db":
        print(
            f"! файл называется tgfarm.db ({resolved}); убедись, что это копия, "
            "а не боевая база",
            file=sys.stderr,
        )
    return resolved


def _corpus_context(db: Path) -> dict[str, Any]:
    """Row counts of the copy — the sample size behind every timing below."""
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        info: dict[str, Any] = {}
        for table in ("posts", "post_stats", "channels", "raw_items", "learning_memory"):
            info[table] = conn.execute(f"select count(*) from {table}").fetchone()[0]
        info["published"] = conn.execute(
            "select count(*) from posts where status='published' and published_at is not null"
        ).fetchone()[0]
        info["channels_by_platform"] = conn.execute(
            "select lower(platform), count(*) from channels group by lower(platform)"
        ).fetchall()
        info["published_by_platform"] = conn.execute(
            """
            select lower(c.platform), count(*)
            from posts p join channels c on c.id = p.channel_id
            where p.status='published' and p.published_at is not null
            group by lower(c.platform)
            """
        ).fetchall()
        info["earliest_published"] = conn.execute(
            "select min(published_at) from posts where status='published'"
        ).fetchone()[0]
        return info
    finally:
        conn.close()


def _rows_scanned(db: Path, platform: str, window_days: int) -> dict[str, int]:
    """Rows the report query actually pulls into memory, counted with plain SQL.

    `format_performance` reads two result sets: the published posts inside the
    window for that platform, and the grouped `post_stats` rows for those ids.
    Those two numbers are what a call reads in; the query plan is printed once at
    the top of the report so a full-table scan cannot be mistaken for a seek.
    """
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        where = (
            "from posts p join channels c on c.id = p.channel_id "
            "where p.status='published' and p.published_at is not null "
            "and p.published_at >= datetime('now', ?) and lower(c.platform)=?"
        )
        bound = f"-{int(window_days)} days"
        posts = conn.execute(f"select count(*) {where}", (bound, platform)).fetchone()[0]
        stats = conn.execute(
            f"select count(*) from post_stats s where s.post_id in (select p.id {where})",
            (bound, platform),
        ).fetchone()[0]
        return {"posts_in_window": int(posts), "post_stats_rows": int(stats)}
    finally:
        conn.close()


class _Probe:
    """Count `format_performance` calls, their duration and their SQL round trips."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.statements: list[str] = []
        self.statements_start = 0
        self.label = ""

    def install(self) -> None:
        from sqlalchemy import event
        from sqlalchemy.engine import Engine

        import tgfarm.s12_learning as s12
        from tgfarm.db.session import get_engine

        self.real_format_performance = s12.format_performance

        def instrumented(days: int = 30, **kwargs: Any) -> dict[str, Any]:
            started = time.perf_counter()
            self.statements_start = len(self.statements)
            try:
                return self.real_format_performance(days, **kwargs)
            finally:
                self.calls.append(
                    {
                        "label": self.label,
                        "days": int(days),
                        "platform": str(kwargs.get("platform") or ""),
                        "ms": (time.perf_counter() - started) * 1000.0,
                        "statements": len(self.statements) - self.statements_start,
                    }
                )

        s12.format_performance = instrumented  # type: ignore[assignment]

        engine: Engine = get_engine()
        event.listen(engine, "before_cursor_execute", self._on_execute)

    def _on_execute(self, conn, cursor, statement, parameters, context, executemany) -> None:
        self.statements.append(" ".join(statement.split())[:90])

    def reset(self, label: str) -> None:
        self.label = label
        self.calls.clear()
        self.statements.clear()

    def by_platform(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for call in self.calls:
            out[call["platform"]] = out.get(call["platform"], 0) + 1
        return out


def _measure_single_channel(db: Path, probe: _Probe, channel_id: str, window: int) -> dict[str, Any]:
    from tgfarm.s12_learning import learn_format_lessons

    probe.reset(f"learn_format_lessons[{channel_id}@{window}]")
    started = time.perf_counter()
    error = ""
    try:
        report = learn_format_lessons(channel_id, days=window)
    except Exception as exc:  # noqa: BLE001 - the measurement must report the failure
        report = {}
        error = f"{type(exc).__name__}: {str(exc)[:120]}"
    ms = (time.perf_counter() - started) * 1000.0
    calls = [call for call in probe.calls if call["label"] == probe.label]
    return {
        "channel_id": channel_id,
        "platform": report.get("platform", ""),
        "window": window,
        "total_ms": ms,
        "fp_calls": len(calls),
        "fp_ms": sum(call["ms"] for call in calls),
        "statements": len(probe.statements),
        "written": int(report.get("written", 0)),
        "measured_total": int(report.get("measured_total", 0)),
        "published_total": int(report.get("published_total", 0)),
        "formats": len(report.get("formats") or {}),
        "reason": report.get("reason", ""),
        "error": error,
    }


def _fleet_channels(db: Path, which: str) -> list[str]:
    """Channel ids to measure: the enabled fleet, and the whole DB catalogue."""
    from tgfarm.config import get_config

    enabled = [p.id for p in get_config().enabled_personas]
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        every = [row[0] for row in conn.execute("select id from channels order by id")]
    finally:
        conn.close()
    if which == "enabled":
        return enabled
    return every


def _fmt_row(cells: list[str], widths: list[int]) -> str:
    return "  ".join(str(cell).ljust(w) for cell, w in zip(cells, widths, strict=True))


#: Tag of the synthetic posts the growth phase writes into the copy.
_GROWTH_TAG = "mcost"


def _seed_growth(db: Path, channel_id: str, token: str, count: int) -> None:
    """Write `count` synthetic published posts (one in five measured) into the copy.

    The live corpus has 452 published posts, so window size alone cannot show how
    the cost grows; this appends tagged rows so the measurement can. Nothing here
    touches the production file, and `_clear_growth` removes the rows again.
    """
    now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None, microsecond=0)
    formats = ("news", "article", "flash", "stat", "digest")
    conn = sqlite3.connect(db)
    try:
        conn.execute("begin transaction")
        conn.executemany(
            """
            insert into posts (public_id, channel_id, angle, post_format, hook, body,
                               hashtags, sources, claims, quality, language, status,
                               rewrites_used, char_len, variant, attempts,
                               created_at, updated_at, published_at)
            values (?, ?, 'news', ?, '', '', '[]', '[]', '[]', '{}', 'ru',
                    'published', 0, 0, 'a', 0, ?, ?, ?)
            """,
            [
                (
                    f"{_GROWTH_TAG}{token}{index}"[:32],
                    channel_id,
                    formats[index % len(formats)],
                    now.isoformat(sep=" "),
                    now.isoformat(sep=" "),
                    (now - datetime.timedelta(minutes=index % 5000)).isoformat(sep=" "),
                )
                for index in range(count)
            ],
        )
        conn.execute(
            """
            insert into post_stats (post_id, views, reactions, comments, forwards, collected_at)
            select id, 0, ?, 0, 0, ? from posts
            where public_id like ? and mod(id, 5) = 0
            """,
            (7, now.isoformat(sep=" "), f"{_GROWTH_TAG}{token}%"),
        )
        conn.commit()
    finally:
        conn.close()


def _clear_growth(db: Path, token: str) -> None:
    conn = sqlite3.connect(db)
    try:
        ids = [row[0] for row in conn.execute(
            "select id from posts where public_id like ?", (f"{_GROWTH_TAG}{token}%",)
        )]
        for chunk_start in range(0, len(ids), 500):
            chunk = ids[chunk_start:chunk_start + 500]
            marks = ",".join("?" * len(chunk))
            conn.execute(f"delete from post_stats where post_id in ({marks})", chunk)
            conn.execute(f"delete from posts where id in ({marks})", chunk)
        conn.commit()
    finally:
        conn.close()


def _growth_phase(
    db: Path, probe: _Probe, window: int, points: list[int], channel_id: str
) -> list[dict[str, Any]]:
    """Cost of ONE channel's format lesson as its platform's published set grows."""
    print(f"\n=== рост числа публикаций на площадке (канал {channel_id}, окно {window} дн.) ===")
    print("  синтетические строки пишутся только в копию и удаляются после замера")
    widths3 = [14, 12, 12, 11, 8, 9, 9, 30]
    print(_fmt_row(["seeded", "per_ch_ms", "fp_ms", "sql", "written",
                    "posts", "stats", "note"], widths3))
    measured: list[dict[str, Any]] = []
    for count in points:
        token = f"g{count}"
        if count:
            _seed_growth(db, channel_id, token, count)
        probe.reset(f"growth[{count}]")
        row = _measure_single_channel(db, probe, channel_id, window)
        row["seeded"] = count
        note = row["error"][:60]
        counts = _rows_scanned(db, row["platform"] or "telegram", window)
        print(_fmt_row([str(count), f"{row['total_ms']:.2f}", f"{row['fp_ms']:.2f}",
                        str(row["statements"]), str(row["written"]),
                        str(counts["posts_in_window"]), str(counts["post_stats_rows"]),
                        note], widths3))
        measured.append(row)
        if count:
            _clear_growth(db, token)
    return measured


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="path to a COPY of the live database")
    parser.add_argument(
        "--windows",
        default="60,180,365",
        help="comma-separated window sizes in days (default: 60,180,365)",
    )
    parser.add_argument("--passes", type=int, default=2, help="repeats per channel (default 2)")
    parser.add_argument(
        "--scale",
        default="1000,5000,40000",
        help="synthetic published posts to add to the COPY (empty string skips it)",
    )
    args = parser.parse_args()

    db = _guard_the_copy(Path(args.db))
    windows = [int(item) for item in args.windows.split(",") if item.strip()]
    os.environ["DATABASE_URL"] = f"sqlite:///{db.as_posix()}"

    from tgfarm.core.settings import reset_settings_cache
    from tgfarm.db.session import dispose_engine

    reset_settings_cache()
    dispose_engine()

    from tgfarm import s12_learning
    from tgfarm.core.settings import get_settings

    settings = get_settings()
    if not str(settings.database_url).startswith("sqlite"):
        raise SystemExit("ожидается sqlite-копия, а не сетевая база")

    context = _corpus_context(db)
    print("=== копия базы, на которой измеряем (боевая файл не открывается) ===")
    print(f"  file                : {db}")
    print(f"  size                : {db.stat().st_size / 1024 / 1024:.1f} MiB")
    print(f"  posts               : {context['posts']}")
    print(f"  post_stats          : {context['post_stats']}")
    print(f"  published posts     : {context['published']}")
    print(f"  raw_items           : {context['raw_items']}")
    print(f"  channels (DB)       : {context['channels']} {context['channels_by_platform']}")
    print(f"  published/platform  : {context['published_by_platform']}")
    print(f"  window setting      : FORMAT_PERFORMANCE_WINDOW_DAYS="
          f"{settings.format_performance_window_days}")

    probe = _Probe()
    probe.install()

    enabled = _fleet_channels(db, "enabled")
    all_channels = _fleet_channels(db, "all")
    print("\n=== размер выборки ===")
    print(f"  enabled personas    : {len(enabled)} {enabled}")
    print(f"  channels in DB      : {len(all_channels)}")

    # --- 1. per channel, default window, several passes --------------------
    default_window = int(settings.format_performance_window_days)
    print(f"\n=== learn_format_lessons по каналам, окно {default_window} дн. ===")
    widths = [22, 9, 8, 10, 9, 9, 8, 8, 9, 9]
    print(_fmt_row(["channel", "platform", "pass", "total_ms", "fp_calls", "fp_ms",
                    "sql", "written", "measured", "published"], widths))
    per_channel: dict[int, list[dict[str, Any]]] = {}
    for window in windows:
        rows: list[dict[str, Any]] = []
        for attempt in range(max(1, args.passes)):
            for cid in all_channels:
                row = _measure_single_channel(db, probe, cid, window)
                row["pass"] = attempt + 1
                rows.append(row)
                print(_fmt_row([
                    row["channel_id"], row["platform"] or "-", str(row["pass"]),
                    f"{row['total_ms']:.2f}", str(row["fp_calls"]), f"{row['fp_ms']:.2f}",
                    str(row["statements"]), str(row["written"]), str(row["measured_total"]),
                    str(row["published_total"]),
                ], widths))
        per_channel[window] = rows
        if window != windows[-1]:
            print(f"  --- окно {window} дн. готово ---")

    # --- 2. scaling with the window ---------------------------------------
    print("\n=== масштабирование по окну (медиана 2-го прохода, все каналы БД) ===")
    widths2 = [8, 12, 11, 12, 13, 14, 16]
    print(_fmt_row(["window", "per_ch_ms", "fp_ms", "channels", "fp_calls",
                    "posts_scan", "stats_scan"], widths2))
    for window in windows:
        steady = [row for row in per_channel[window] if row["pass"] == max(1, args.passes)]
        med = sorted(row["total_ms"] for row in steady)[len(steady) // 2] if steady else 0.0
        fp_ms = sum(row["fp_ms"] for row in steady) / max(1, len(steady))
        calls = sum(row["fp_calls"] for row in steady)
        scanned_posts = 0
        scanned_stats = 0
        seen: set[tuple[str, int]] = set()
        for row in steady:
            key = (row["platform"], window)
            if row["platform"] and key not in seen:
                seen.add(key)
                counts = _rows_scanned(db, row["platform"], window)
                scanned_posts += counts["posts_in_window"]
                scanned_stats += counts["post_stats_rows"]
        print(_fmt_row([f"{window}d", f"{med:.2f}", f"{fp_ms:.2f}", str(len(steady)),
                        str(calls), str(scanned_posts), str(scanned_stats)], widths2))

    # --- 3. how many calls are the same query ------------------------------
    print("\n=== дублирование чтения (окно по умолчанию) ===")
    steady = [row for row in per_channel[default_window] if row["pass"] == max(1, args.passes)]
    by_platform: dict[str, list[dict[str, Any]]] = {}
    for row in steady:
        by_platform.setdefault(row["platform"] or "-", []).append(row)
    for platform, rows in sorted(by_platform.items()):
        identical = sum(row["fp_ms"] for row in rows) - (rows[0]["fp_ms"] if rows else 0.0)
        print(f"  {platform:<10} channels={len(rows):<3} fp_calls={len(rows):<3} "
              f"идеентичных повторов={len(rows) - 1:<3} wasted_ms={identical:.2f}")
    total_calls = sum(row["fp_calls"] for row in steady)
    distinct = len({row["platform"] for row in steady if row["platform"]})
    print(f"  всего вызовов format_performance: {total_calls}, разных площадок: {distinct}")

    # --- 4. the whole s12 run() --------------------------------------------
    print("\n=== s12_learning.run() целиком (enabled-флот) ===")
    run_ms: list[float] = []
    for attempt in range(3):
        probe.reset(f"run[{attempt}]")
        started = time.perf_counter()
        result = asyncio.run(s12_learning.run())
        run_ms.append((time.perf_counter() - started) * 1000.0)
        fp_ms = sum(call["ms"] for call in probe.calls)
        print(f"  pass {attempt + 1}: run_ms={run_ms[-1]:.2f} "
              f"fp_calls={len(probe.calls)} fp_ms={fp_ms:.2f} "
              f"sql={len(probe.statements)} result={result}")
    learn_ms = [row["total_ms"] for row in steady]
    enabled_ms = [row["total_ms"] for row in steady if row["channel_id"] in enabled]
    print(f"  learn_format_lessons за канал: медиана="
          f"{sorted(learn_ms)[len(learn_ms) // 2]:.2f} мс ({len(learn_ms)} каналов), "
          f"сумма по всем {len(learn_ms)}={sum(learn_ms):.2f} мс, "
          f"сумма по {len(enabled_ms)} включённым={sum(enabled_ms):.2f} мс")
    print(f"  доля формат-обучения в {min(run_ms):.0f} мс этапа s12 = "
          f"{sum(enabled_ms) / max(min(run_ms), 1.0) * 100:.1f}% "
          f"(считается по тем же {len(enabled)} включённым каналам, что и run())")

    # --- 5. growth of the published corpus ---------------------------------
    growth_rows: list[dict[str, Any]] = []
    scale_points = [int(item) for item in args.scale.split(",") if item.strip()]
    if scale_points:
        growth_channel = next(
            (row["channel_id"] for row in steady if row["platform"] == "telegram"),
            all_channels[0] if all_channels else "",
        )
        if growth_channel:
            growth_rows = _growth_phase(db, probe, default_window, scale_points, growth_channel)

    # --- 6. projection -----------------------------------------------------
    print(f"\n=== проекция на цикл (бюджет {CYCLE_BUDGET_S:.0f} с) ===")
    for fleet in (len(all_channels), 30, 100):
        worst_window = max(windows)
        rows_w = [row for row in per_channel[worst_window] if row["pass"] == max(1, args.passes)]
        med_w = sorted(row["total_ms"] for row in rows_w)[len(rows_w) // 2] if rows_w else 0.0
        projected = med_w * fleet / 1000.0
        print(f"  fleet={fleet:<4} окно={worst_window}дн.: формат-обучение ≈ "
              f"{projected:.2f} с = {projected / CYCLE_BUDGET_S * 100:.3f}% бюджета")
    for row in growth_rows:
        if not row["total_ms"]:
            continue
        for fleet in (len(all_channels), 100):
            projected = row["total_ms"] * fleet / 1000.0
            print(f"  fleet={fleet:<4} при {row['seeded']:<6} дописанных публикациях: ≈ "
                  f"{projected:.2f} с = {projected / CYCLE_BUDGET_S * 100:.3f}% бюджета")
    print(f"  измерено целиком s12_learning.run() = {min(run_ms):.0f} мс "
          f"(флот из {len(enabled)} включённых каналов)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
