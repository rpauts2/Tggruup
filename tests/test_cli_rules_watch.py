"""`tgfarm rules-status` — the read half of the destination-rules story.

`tgfarm/maintenance.py::watch_destination_rules` can only answer "under which
rules did we publish" by *asking the platforms* (it calls
`reddit_scout.inspect_community` / `community_targets.revalidate_pikabu_target`)
and by recording a snapshot through `community_policy.record_policy_snapshot`.
Both are side effects, so a read command must not call it. What is printed here
is what the deployment already stores: `community_policy_snapshots`
newest-per-destination, inert the same way `policy_history` hands it to the
panel, plus the platform switches — and the state words are that step's own
vocabulary, never a new dialect.

The live database held **0 rows** in `community_policy_snapshots` when this was
written (measured read-only 2026-11-05), so the empty path *is* the production
path: every test here pins that it says a state and a reason, never an empty
table, never a `0 h` age, and never a `watched` nobody ran.

Exact-string tests fake the stored-history read with `monkeypatch` so the shared
session database cannot leak another test's rows into an expected line; the real
read path, the inert treatment and the no-write guarantee are tested against real
rows.
"""

from __future__ import annotations

import inspect
import re
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, event, func, select
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from tgfarm import cli, community_policy, community_targets, maintenance, platforms
from tgfarm.db.models import CommunityPolicySnapshot as Snapshot
from tgfarm.db.session import get_engine, session_scope
from tgfarm.maintenance import RULES_WATCH_PLATFORMS
from tgfarm.s9_growth import reddit_scout

PREFIX = "p78cli"
NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
WATCH = tuple(sorted(RULES_WATCH_PLATFORMS))
#: A platform id the code has no rules reader for at all. It used to be `vc_ru`,
#: which `maintenance._RULES_READER_FOR` now covers; this synthetic id keeps the
#: `no_rules_reader` path testable whatever the watch set grows next.
READERLESS = "p78noreader"

NO_SNAPSHOT = cli.NO_RULES_SNAPSHOT
DISABLED_REASON = (
    "это решение оператора: площадка выключена в центре площадок, поэтому её "
    "правила не читаются и история не пополняется"
)
NO_TARGET_REASON = (
    "решения ещё нет: площадка включена и читатель правил есть, но история не "
    "называет ни одного назначения, поэтому перечитывать нечего; первое "
    "назначение появится из выбора цели или публикации"
)
WATCHED_REASON = (
    "площадка включена, читатель правил есть, история называет назначения; "
    "показаны сохранённые снимки, этим прогоном ничего не перечитывалось"
)
NO_READER_REASON = (
    "читателя правил этой площадки в коде TgFarm ещё нет — это наша лакуна, а "
    "не отсутствие правил у площадки; назначения из истории не перечитываются"
)
NOTHING_REASON = (
    "ни одна площадка не настроена для наблюдения: переключатели выключены либо "
    "история правил не называет ни одного назначения"
)
HASH = "a" * 64
HEAD = ["читаем сохранённое: сеть не тронута, база не изменена"]
MD_HEAD = [
    "| Площадка | Состояние | Назначение | Хеш правил | Возраст снимка | Причина |",
    "|---|---|---|---|---|---|",
]

#: `vc_ru` has a rules reader now, so the read side no longer calls it
#: `no_rules_reader`; it asks `tgfarm/platforms.is_platform_enabled` and nothing
#: else (tgfarm/cli.py:746-753, printed with the switch name at :769-770), while
#: `maintenance._rules_watch_switch` additionally watches a draft-only destination
#: that has no `enabled.*` key at all. So this command still answers `disabled`
#: here. The lines are pinned as cli's own read-side answer, not as maintenance's;
#: aligning the two predicates is a change to cli.py, which is not this file's.
VCRU_DISABLED_TEXT = [
    "vc_ru: disabled (переключатель enabled.vc_ru)",
    f"  назначений с сохранённым снимком правил: {NO_SNAPSHOT}",
    f"  причина: {DISABLED_REASON}",
]
VCRU_DISABLED_ROW = (
    f"| vc_ru | disabled | {NO_SNAPSHOT} | {NO_SNAPSHOT} | {NO_SNAPSHOT} | {DISABLED_REASON} |"
)


# --------------------------------------------------------------------- fixtures
@pytest.fixture(autouse=True)
def _drop_p78_rows():
    """Leave the shared database exactly as we found it.

    Deleted by the `p78cli` target-id pattern rather than by id: the shared
    session database gives a test no way to know the ids it just created.
    """
    yield
    with session_scope() as session:
        session.execute(delete(Snapshot).where(Snapshot.target_id.like(f"{PREFIX}%")))


@pytest.fixture()
def probe(monkeypatch):
    """Build a report from a stated history and stated switches, offline and exactly."""

    def _probe(
        rows=(),
        *,
        enabled=(),
        platform=None,
        target_id=None,
    ) -> dict:
        chosen = {str(name) for name in enabled}
        stored = list(rows)
        monkeypatch.setattr(platforms, "is_platform_enabled", lambda name: name in chosen)
        monkeypatch.setattr(
            cli,
            "_rules_snapshot_destinations",
            lambda *, history_limit: (stored, len(stored)),
        )
        return cli.rules_status_report(
            platform=platform, target_id=target_id, now=NOW
        )

    return _probe


# ----------------------------------------------------------------------- helpers
def _stored(
    platform: str,
    target_id: str,
    *,
    name: str = "",
    target_type: str = "community",
    rules_hash: str = HASH,
    verdict: str = "ok",
    checked_at: datetime | None = NOW - timedelta(hours=7),
) -> dict:
    """One row in the exact shape `_rules_snapshot_destinations` returns (post-inert)."""
    return {
        "platform": platform,
        "target_type": target_type,
        "target_id": target_id,
        "target_name": name,
        "rules_hash": rules_hash,
        "verdict_status": verdict,
        "checked_at": checked_at,
    }


def _text(report: dict) -> list[str]:
    return cli.render_rules_status(report, fmt="text")


def _entry(report: dict, platform: str) -> dict:
    """One platform's entry, by name: the report now answers for three readers."""
    return next(item for item in report["platforms"] if item["platform"] == platform)


def _markdown(report: dict) -> list[str]:
    return cli.render_rules_status(report, fmt="markdown")


def _summary(count: int, scanned: int, truncated: bool = False) -> str:
    return (
        f"источник: community_policy_snapshots | назначений в истории: {count} (посчитано)"
        f" | просмотрено: {scanned}" + (" | срезано лимитом" if truncated else "")
    )


def _seed(platform: str, target_id: str, *, name: str, rules_hash: str, hours_ago: float) -> None:
    moment = datetime.now(UTC) - timedelta(hours=hours_ago)
    with session_scope() as session:
        session.add(
            Snapshot(
                platform=platform,
                target_type="subreddit",
                target_id=target_id,
                target_name=name,
                rules_source="https://reddit.com/r/python/about",
                rules_hash=rules_hash,
                rules_available=True,
                rules_excerpt="Stay on topic.",
                verdict_status="ok",
                confidence=0.6,
                requirements_json=["Stay on topic."],
                checked_at=moment,
                recorded_at=moment,
            )
        )


def _count(target_id: str) -> int:
    """Count through a bare connection: the ORM session is what must stay untouched."""
    with get_engine().connect() as conn:
        return int(
            conn.execute(
                select(func.count()).select_from(Snapshot).where(Snapshot.target_id == target_id)
            ).scalar_one()
            or 0
        )


# ------------------------------------------------------------------ empty history
def test_no_stored_snapshot_prints_states_and_reasons_never_an_empty_table(probe):
    """Today's live shape: both community switches off and 0 rows in history.

    The answer is a state word plus its reason, and «нет снимка правил» in every
    value slot — not a row, not a hash, not an age of `0 h` (P34).
    """
    report = probe(rows=(), enabled=())

    assert report["state"] == "nothing_to_watch"
    assert report["detail"] == NOTHING_REASON
    assert report["destinations_stored"] == 0, "counted and empty, and it says so"
    assert report["truncated"] is False
    assert report["blocked"] == []
    assert _text(report) == [
        *HEAD,
        _summary(0, 0),
        f"всего: nothing_to_watch — {NOTHING_REASON}",
        "pikabu: disabled (переключатель enabled.pikabu)",
        f"  назначений с сохранённым снимком правил: {NO_SNAPSHOT}",
        f"  причина: {DISABLED_REASON}",
        "reddit: disabled (переключатель enabled.reddit)",
        f"  назначений с сохранённым снимком правил: {NO_SNAPSHOT}",
        f"  причина: {DISABLED_REASON}",
        *VCRU_DISABLED_TEXT,
    ]
    rendered = "\n".join(_text(report))
    assert "0 ч" not in rendered
    assert NO_SNAPSHOT in rendered
    # The reader gap this command used to name for vc.ru is closed: no line here
    # says `no_rules_reader`, because a reader for vc_ru exists now.
    assert "no_rules_reader" not in rendered
    assert "vc_ru" in WATCH


def test_the_empty_history_still_renders_a_table_not_a_blank(probe):
    rows = _markdown(probe(rows=(), enabled=()))

    assert rows == [
        *MD_HEAD,
        f"| pikabu | disabled | {NO_SNAPSHOT} | {NO_SNAPSHOT} | {NO_SNAPSHOT} | "
        f"{DISABLED_REASON} |",
        f"| reddit | disabled | {NO_SNAPSHOT} | {NO_SNAPSHOT} | {NO_SNAPSHOT} | "
        f"{DISABLED_REASON} |",
        VCRU_DISABLED_ROW,
    ]


# ---------------------------------------------------------- the five state names
def test_disabled_reads_as_a_decision(probe):
    report = probe(rows=(), enabled=["reddit"])
    entry = next(item for item in report["platforms"] if item["platform"] == "pikabu")

    assert entry["state"] == "disabled"
    assert entry["switch"] == "enabled.pikabu"
    assert entry["enabled"] is False
    assert entry["reason"] == DISABLED_REASON
    assert "pikabu: disabled (переключатель enabled.pikabu)" in _text(report)


def test_enabled_without_a_known_destination_is_its_own_line(probe):
    report = probe(rows=(), enabled=["reddit"])
    entry = next(item for item in report["platforms"] if item["platform"] == "reddit")

    assert entry["state"] == "enabled_no_known_destination"
    assert entry["enabled"] is True
    assert entry["destinations"] == 0
    assert entry["reason"] == NO_TARGET_REASON
    assert _text(report) == [
        *HEAD,
        _summary(0, 0),
        f"всего: nothing_to_watch — {NOTHING_REASON}",
        "pikabu: disabled (переключатель enabled.pikabu)",
        f"  назначений с сохранённым снимком правил: {NO_SNAPSHOT}",
        f"  причина: {DISABLED_REASON}",
        "reddit: enabled_no_known_destination (переключатель enabled.reddit)",
        f"  назначений с сохранённым снимком правил: {NO_SNAPSHOT}",
        f"  причина: {NO_TARGET_REASON}",
        *VCRU_DISABLED_TEXT,
    ]


def test_watched_shows_the_stored_hash_and_the_age_of_that_snapshot(probe):
    row = _stored("reddit", "python", name="r/python")
    report = probe(rows=[row], enabled=["reddit"])
    entry = _entry(report, "reddit")

    assert entry["state"] == "watched"
    assert entry["destinations"] == 1
    assert entry["known"][0]["age_hours"] == 7.0
    assert entry["known"][0]["rules_hash"] == HASH
    lines = _text(report)
    start = lines.index("reddit: watched (переключатель enabled.reddit)")
    assert lines[start:start + 4] == [
        "reddit: watched (переключатель enabled.reddit)",
        (
            f"  r/python (python) | правила: {HASH} | возраст снимка: 7.0 ч | "
            "срез: 2026-11-05T05:00:00+00:00 | вердикт: ok"
        ),
        "  назначений с сохранённым снимком правил: 1",
        f"  причина: {WATCHED_REASON}",
    ]
    assert _markdown(report)[3] == (
        f"| reddit | watched | r/python (python) | {HASH} | 7.0 ч | {WATCHED_REASON} |"
    )


def test_no_rules_reader_reads_as_our_gap_not_the_platforms(probe):
    row = _stored(READERLESS, "p78cli_topic", name="Тема")
    report = probe(rows=[row], enabled=[READERLESS])
    entry = next(item for item in report["platforms"] if item["platform"] == READERLESS)
    lines = _text(report)

    assert entry["state"] == "no_rules_reader"
    assert entry["rules_reader"] is False
    # Our missing reader is not a switch the operator forgot: no switch claim at all.
    assert "switch" not in entry and "enabled" not in entry
    assert entry["reason"] == NO_READER_REASON
    assert report["blocked"] == [f"{READERLESS}:no_rules_reader"]
    assert f"{READERLESS}: no_rules_reader" in lines
    assert lines[-1] == f"пробел в коде TgFarm: {READERLESS}:no_rules_reader"


def test_vc_ru_is_no_longer_reported_as_a_missing_reader(probe):
    """The gap this block closes, seen from the read side.

    A stored vc.ru destination now has a reader in `maintenance._RULES_READER_FOR`,
    so `no_rules_reader` and the «пробел в коде TgFarm» line must stop being
    produced for it — while a platform with no reader at all keeps both, which the
    test above pins with a synthetic id.
    """
    row = _stored("vc_ru", "p78cli_topic", name="Финансы", target_type="topic")
    report = probe(rows=[row], enabled=["vc_ru"])
    entry = _entry(report, "vc_ru")
    rendered = "\n".join(_text(report))

    assert entry["rules_reader"] is True
    assert entry["state"] == "watched"
    assert entry["destinations"] == 1
    assert entry["reason"] == WATCHED_REASON
    assert "vc_ru: no_rules_reader" not in rendered
    assert "no_rules_reader" not in rendered, "the reader gap is closed for every state here"


def test_nothing_to_watch_is_the_summary_when_no_destination_is_stored(probe):
    assert probe(rows=(), enabled=list(WATCH))["state"] == "nothing_to_watch"
    assert probe(rows=(), enabled=[])["state"] == "nothing_to_watch"
    assert probe(
        rows=[_stored(WATCH[0], f"{PREFIX}_one")], enabled=list(WATCH)
    )["state"] == "watched"


# ------------------------------------------------------------- null stored values
def test_a_snapshot_without_a_timestamp_has_no_invented_age(probe):
    row = _stored("reddit", "python", name="r/python", checked_at=None)
    report = probe(rows=[row], enabled=["reddit"])
    known = _entry(report, "reddit")["known"][0]

    assert known["age_hours"] is None
    assert known["age_text"] == NO_SNAPSHOT
    assert (
        f"  r/python (python) | правила: {HASH} | возраст снимка: {NO_SNAPSHOT} | "
        f"срез: {NO_SNAPSHOT} | вердикт: ok" in _text(report)
    )


def test_a_snapshot_without_a_hash_says_so(probe):
    row = _stored("reddit", "python", name="r/python", rules_hash="")
    report = probe(rows=[row], enabled=["reddit"])

    assert _entry(report, "reddit")["known"][0]["rules_hash"] == ""
    assert any(f"правила: {NO_SNAPSHOT}" in line for line in _text(report)), _text(report)


def test_a_destination_without_a_name_falls_back_to_its_id(probe):
    row = _stored("reddit", "python", name="")
    report = probe(rows=[row], enabled=["reddit"])

    assert _entry(report, "reddit")["known"][0]["label"] == "python"
    assert any("  python | правила:" in line for line in _text(report)), _text(report)


# ------------------------------------------------------------------ mixed reports
def test_a_mixed_report_keeps_every_state_on_its_own_line(probe):
    report = probe(
        rows=[
            _stored("reddit", "python", name="r/python", checked_at=NOW - timedelta(hours=30)),
            _stored(READERLESS, "p78cli_topic", name="Тема", rules_hash="deadbeef"),
        ],
        enabled=["reddit"],
    )

    assert _text(report) == [
        *HEAD,
        _summary(2, 2),
        f"всего: watched — {WATCHED_REASON}",
        f"{READERLESS}: no_rules_reader",
        (
            "  Тема (p78cli_topic) | правила: deadbeef | возраст снимка: 7.0 ч | "
            "срез: 2026-11-05T05:00:00+00:00 | вердикт: ok"
        ),
        "  назначений с сохранённым снимком правил: 1",
        f"  причина: {NO_READER_REASON}",
        "pikabu: disabled (переключатель enabled.pikabu)",
        f"  назначений с сохранённым снимком правил: {NO_SNAPSHOT}",
        f"  причина: {DISABLED_REASON}",
        "reddit: watched (переключатель enabled.reddit)",
        (
            f"  r/python (python) | правила: {HASH} | возраст снимка: 30.0 ч | "
            "срез: 2026-11-04T06:00:00+00:00 | вердикт: ok"
        ),
        "  назначений с сохранённым снимком правил: 1",
        f"  причина: {WATCHED_REASON}",
        *VCRU_DISABLED_TEXT,
        f"пробел в коде TgFarm: {READERLESS}:no_rules_reader",
    ]
    assert [item["state"] for item in report["platforms"]] == [
        "no_rules_reader",
        "disabled",
        "watched",
        "disabled",
    ]
    assert [item["platform"] for item in report["platforms"]] == [
        READERLESS,
        "pikabu",
        "reddit",
        "vc_ru",
    ]


def test_a_platform_missing_from_the_data_is_still_answered(probe):
    """`--platform` for a destination the history never named: a state, not silence."""
    report = probe(rows=[_stored("reddit", "python")], enabled=["reddit"], platform="habr")

    assert [item["platform"] for item in report["platforms"]] == ["habr"]
    assert report["platforms"][0]["state"] == "no_rules_reader"
    assert report["platforms"][0]["destinations"] == 0
    assert report["state"] == "nothing_to_watch"
    assert report["destinations_in_scope"] == 0
    assert report["scope"] == {"platform": "habr", "target_id": ""}
    assert "область запроса: platform=habr target=-" in report["detail"]


def test_a_watch_platform_with_no_rows_under_a_filter_is_named_too(probe):
    report = probe(rows=[_stored("pikabu", "python")], enabled=list(WATCH), platform="pikabu")

    assert [item["platform"] for item in report["platforms"]] == ["pikabu"]
    assert report["platforms"][0]["state"] == "watched"


def test_the_target_filter_narrows_the_scope_and_says_so(probe):
    report = probe(
        rows=[
            _stored("reddit", "python", name="r/python"),
            _stored("reddit", "coolgithub", name="r/coolgithub"),
        ],
        enabled=["reddit"],
        target_id="python",
    )

    assert report["destinations_in_scope"] == 1
    assert report["destinations_scanned"] == 2, "the scan is bounded by the limit, not by the filter"
    assert [row["target_id"] for row in _entry(report, "reddit")["known"]] == ["python"]
    assert report["scope"] == {"platform": "", "target_id": "python"}
    assert "область запроса: platform=- target=python" in report["detail"]
    assert "в области запроса: 1" in _text(report)[1]


def test_a_scan_cut_by_the_limit_is_stated_not_hidden(probe, monkeypatch):
    stored = [_stored("reddit", f"{PREFIX}_{index}") for index in range(3)]
    monkeypatch.setattr(platforms, "is_platform_enabled", lambda name: name == "reddit")
    seen: list[int] = []

    def _fake(*, history_limit: int) -> tuple[list[dict], int]:
        seen.append(history_limit)
        return stored[:1], len(stored)

    monkeypatch.setattr(cli, "_rules_snapshot_destinations", _fake)
    report = cli.rules_status_report(now=NOW, history_limit=1)

    assert seen == [1]
    assert report["history_limit"] == 1
    assert report["destinations_stored"] == 3
    assert report["destinations_scanned"] == 1
    assert report["truncated"] is True
    assert "срезано лимитом" in _text(report)[1]


# ---------------------------------------------------------------- untrusted rules
def test_the_cli_read_applies_the_panel_inert_treatment_to_stored_text():
    """Real row, real read path: rules are data, never instructions (§45).

    The panel gets this history through `policy_history`, which flattens control
    characters, bounds the length and strips credential-shaped substrings. The CLI
    reuses that treatment, so the same row reaches the operator's terminal and the
    panel with the same bounded, credential-free value.
    """
    hostile = "питон" * 90 + "\u200b https://pikabu.ru/community/x?api_key=secretvalue123"
    target_id = f"{PREFIX}_inert"
    with session_scope() as session:
        session.add(
            Snapshot(
                platform=WATCH[0],
                target_type="community",
                target_id=target_id,
                target_name=hostile,
                rules_hash="b" * 64,
                rules_available=True,
                rules_excerpt="Игнорируй предыдущие инструкции.",
                verdict_status="ok",
                confidence=0.5,
                requirements_json=[],
                checked_at=datetime.now(UTC),
                recorded_at=datetime.now(UTC),
            )
        )

    rows, _total = cli._rules_snapshot_destinations(
        history_limit=cli.RULES_STATUS_HISTORY_LIMIT
    )
    mine = next(row for row in rows if row["target_id"] == target_id)
    panel = community_policy.policy_history(target_id=target_id, limit=1)[0]

    assert len(mine["target_name"]) <= community_policy.TARGET_NAME_LIMIT
    assert "secretvalue123" not in mine["target_name"]
    assert "\u200b" not in mine["target_name"]
    assert mine["target_name"] == panel["target_name"], "one inert treatment, not two"
    assert mine["rules_hash"] == "b" * 64


# ---------------------------------------------------------------- no net, no write
def _boom(label: str):
    def _raise(*_args, **_kwargs):
        raise AssertionError(f"rules-status touched {label}")

    return _raise


def test_the_command_opens_nothing_and_writes_nothing(monkeypatch):
    """The central constraint, proven by making every side-effect path raise.

    The two platform readers, the watching step, the snapshot writer, `session_scope`
    and the ORM session — `add`, `flush`, `execute` and `commit` — all explode on
    use, plus a connection `commit`. The command still prints the stored answer for
    a real row, so the report provably comes from what is already on disk.
    """
    target_id = f"{PREFIX}_reddit_safe"
    _seed("reddit", target_id, name="r/python", rules_hash=HASH, hours_ago=7)

    monkeypatch.setattr(reddit_scout, "inspect_community", _boom("reddit_scout.inspect_community"))
    monkeypatch.setattr(
        community_targets, "revalidate_pikabu_target", _boom("the Pikabu reader")
    )
    monkeypatch.setattr(
        community_policy, "record_policy_snapshot", _boom("the snapshot writer")
    )
    monkeypatch.setattr(maintenance, "watch_destination_rules", _boom("the watching step"))
    monkeypatch.setattr(maintenance, "_watched_rule_destinations", _boom("the step's own read"))
    monkeypatch.setattr("tgfarm.db.session.session_scope", _boom("session_scope"))
    for name in ("add", "flush", "execute", "commit"):
        monkeypatch.setattr(Session, name, _boom(f"Session.{name}"))
    monkeypatch.setattr(Connection, "commit", _boom("a connection commit"))
    monkeypatch.setattr(platforms, "is_platform_enabled", lambda name: name == "reddit")

    assert cli.main(["rules-status", "--target", target_id]) == 0
    assert _count(target_id) == 1, "the read left exactly the row it found"

    report = cli.rules_status_report(target_id=target_id, now=datetime.now(UTC))
    lines = _text(report)
    assert any(line.startswith("reddit: watched (переключатель enabled.reddit)") for line in lines)
    assert any(
        f"r/python ({target_id})" in line and f"правила: {HASH}" in line for line in lines
    ), lines
    assert any(re.search(r"возраст снимка: 7\.\d ч", line) for line in lines), lines


def test_the_read_only_emits_no_write_statement(monkeypatch):
    """Every statement this command sends to SQLite is a read, said by the driver."""
    target_id = f"{PREFIX}_statements"
    _seed("reddit", target_id, name="r/python", rules_hash=HASH, hours_ago=2)
    monkeypatch.setattr(platforms, "is_platform_enabled", lambda name: name == "reddit")

    seen: list[str] = []
    engine = get_engine()

    def _watch(_conn, _cursor, statement, *_args):
        seen.append(str(statement))

    event.listen(engine, "before_cursor_execute", _watch)
    try:
        assert cli.main(["rules-status", "--target", target_id]) == 0
    finally:
        event.remove(engine, "before_cursor_execute", _watch)

    writes = [
        statement
        for statement in seen
        if re.search(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|VACUUM|ATTACH)\b", statement)
    ]
    assert writes == [], writes
    assert any("community_policy_snapshots" in statement for statement in seen), seen


# --------------------------------------------------------------------- the wiring
def test_parser_follows_the_capability_flags():
    parser = cli.build_parser()
    args = parser.parse_args(["rules-status", "--format", "markdown", "--platform", "reddit"])

    assert args.func is cli.cmd_rules_status
    assert args.format == "markdown"
    assert args.platform == "reddit"
    assert args.target == ""
    assert args.history_limit == cli.RULES_STATUS_HISTORY_LIMIT
    assert parser.parse_args(["rules-status"]).format == "text"


def test_the_history_limit_is_clamped_like_the_panel_caps_it(monkeypatch):
    monkeypatch.setattr(cli, "_rules_snapshot_destinations", lambda *, history_limit: ([], 0))

    assert cli.rules_status_report(history_limit=10_000)["history_limit"] == 200
    assert cli.rules_status_report(history_limit=0)["history_limit"] == 1


def test_the_new_command_is_documented_where_the_cli_documents_itself():
    assert "rules-status" in cli.__doc__


def test_state_names_are_still_the_ones_the_watching_step_uses():
    """One vocabulary, not a CLI dialect: every state name must still live in the step.

    This is also the guard against the merge that would lose the distinction:
    `disabled` is a decision and `enabled_no_known_destination` is the absence of
    one, so they are two states and two lines.
    """
    source = inspect.getsource(maintenance.watch_destination_rules)

    assert set(cli.RULES_STATE_MEANINGS) == {
        "nothing_to_watch",
        "disabled",
        "enabled_no_known_destination",
        "watched",
        "no_rules_reader",
    }
    for state, reason in cli.RULES_STATE_MEANINGS.items():
        assert f'"{state}"' in source, state
        assert reason.strip(), state
    assert cli.RULES_STATE_MEANINGS["disabled"] != cli.RULES_STATE_MEANINGS[
        "enabled_no_known_destination"
    ]
    assert WATCH == ("pikabu", "reddit", "vc_ru"), (
        "the watch set grew; the expected lines above must follow"
    )
    # `vc_ru` joined it because `maintenance._RULES_READER_FOR` now has a
    # destination-only vc.ru reader, so a stored vc.ru destination is something the
    # maintenance pass can re-read instead of a gap it can only name. The reader
    # itself and its safety properties are pinned in
    # `tests/test_maintenance_vc_rules_watch.py`.
    assert "vc_ru" in maintenance._RULES_READER_FOR


def test_report_carries_no_counters_it_did_not_measure(probe):
    """P34 by construction: a read report has no pass counters to fake."""
    blob = str(probe(rows=[_stored("reddit", "python")], enabled=["reddit"]))

    for counter in ("attempted", "checked", "recorded", "collapsed", "changed", "failed"):
        assert f"'{counter}'" not in blob, counter


def test_watched_is_never_claimed_from_a_switch_alone(probe):
    """`watched` needs a stored destination; an enabled switch is not evidence."""
    report = probe(rows=(), enabled=list(WATCH))

    assert [item["state"] for item in report["platforms"]] == [
        "enabled_no_known_destination",
        "enabled_no_known_destination",
        "enabled_no_known_destination",
    ]
    assert report["state"] == "nothing_to_watch"
