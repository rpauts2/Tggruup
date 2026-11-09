""""Does this publication have evidence?" — one answer, proved over every shape.

The defect (block P74): the maintenance sweep that moves a `published` row with no
proof behind it to `publish_unknown` restated the evidence question instead of asking
the owner of it. Three copies existed:

1. `tgfarm/s7_publish.has_publication_evidence` — the predicate the publisher applies
   *before* it writes `published`. Three facts OR'd: a telegram message id; a remote
   id that is not the dry-run placeholder the off-mode adapters invent; an accepted
   `crosspost_logs` row for the pair. One carve-out, added 2026-11-06: on telegram the
   first fact is the *only* fact — the owner returns `False` for a row without a
   message id before it reads the other two;
2. `tgfarm/s8_analytics/formats.EVIDENCE_RULES` — per platform, *which* of those facts
   is that platform's own proof;
3. `tgfarm/maintenance.publication_evidence_repair` — a paraphrase: for telegram only
   fact 1, for dzen/vk only fact 3.

A copy one fact narrower than the write it cleans up after rewrites real work into
`publish_unknown`, which is the destruction this sweep's own docstring was written to
avoid.

What this file proves, in order:

* **the row set** — a fixture over every combination of `tg_message_id` set/unset,
  journal entry absent / posted / scheduled / failed / dry_run and a recorded
  identifier NULL / empty / this platform's placeholder / another platform's
  placeholder / real, on every platform `EVIDENCE_RULES` rules plus one it does not
  (`max`, which the live journal does write). The baseline paraphrase — reproduced
  here as a local function, never in production code — and the delegated predicate are
  run over it and the disagreement is asserted cell by cell, by name. The delegate is
  only asked on platforms the table rules, because that is how the sweep enumerates
  them; the unruled platform has its own assertion that neither copy claims it.
  `tests/test_maintenance_publication_evidence.py` holds the operator-facing
  behaviour; this file holds the equivalence argument;
* **the consent gate** — without `PUBLICATION_EVIDENCE_REPAIR_APPLY` the step cannot
  write, whatever the new selection looks like;
* **the drift guard** — the paraphrase cannot come back: `tgfarm/maintenance.py` is
  scanned for restatements of the three facts, and the scanner is aimed at the
  baseline source itself so it cannot quietly stop finding anything.

Two cells are unrepresentable *by schema*, and that is a finding rather than an
assumption: `posts` has no platform-identifier column except Telegram's
`tg_message_id`, so an identifier only exists on a `crosspost_logs` row. The brief's
"fact 2 may be unreachable for Dzen/VK" is therefore half true — unreachable from the
post, reachable from the journal — and the reachability matters:
`tgfarm/s9_growth/__init__.py:610` still journals a dry-run crosspost with
`remote_id = uuid4().hex[:8]`, which is not the placeholder the owner recognises, and
the live base holds 6 such rows. Offered to fact 2 they would spare a phantom
publication, so the identifier read excludes the entries the journal's *own* vocabulary
classifies as never-sent (`crosspost_journal.JournalStatusClass.REOPEN`) instead of
spelling a status.

The matrix is 168 cells — 4 platforms x 42 — and the two predicates disagree on 16 of
them: 0 telegram, 8 dzen, 8 vk. None of the 16 is the copy ignoring an accepted journal
entry; all 16 are the copy ignoring a recorded identifier (8 dzen, 8 vk: the owner's
second fact, which neither branch of the paraphrase ever weighed). These counts are
measured on the seeded matrix, not estimated. The telegram half of the previous count
— 9 cells, 5 an ignored accepted entry and 4 an ignored identifier, 25 divergences in
total — vanished when the owner was narrowed to the message id alone; the dated note
below says what that narrowing does to the historical measurement.

Read-only live measurement (2026-11-05, `mode=ro`; 477 `published` rows over telegram
15 / dzen 57 / vk 405; no `history_import` publications at all): the baseline paraphrase
and the delegated predicate select **the same 37 rows** — telegram 15, dzen 22, vk 0 —
so none of the divergent cells above had been reached by the farm's own writes at that
date. The same read-only session counts the shapes behind that: 0 telegram published
rows with a journal entry (`crosspost_logs` names only dzen/max/vk/x), and 413 journal
rows carrying a non-`dry-*` identifier, every one of them a `posted` entry belonging to
a dzen or vk post, so no journal-ruled published row had yet reached the
`scheduled`/`failed` + real-identifier cell either. Same session, same counters:
`posted_without_id` 27, and 0.16 s to classify all 477 rows with the delegated
predicate. This paragraph is a past observation; it is not re-measured here.

Dated note (2026-11-06, the owner was narrowed): the telegram half of that measurement
now means something different, and the difference is stated rather than quietly
re-dating the numbers. On 2026-11-05 the two copies agreed on those 15 telegram rows
*because* the live journal held no telegram entry at all, so the owner's second and
third facts never fired — the agreement was contingent on an empty journal. The owner
now returns `False` for a telegram row with no message id without consulting the
journal, so the same 15 rows would still be selected and the 37 would still be 37, but
the agreement is structural: a telegram `posted` entry appearing in the live base
would have changed the 2026-11-05 answer and cannot change it now. The dzen/vk half is
untouched by the narrowing. What the narrowing does change is the claim the sweep may
make about a telegram row — an accepted journal entry carrying a real identifier is no
longer allowed to spare one — and that is pinned positively by
:func:`test_a_telegram_journal_entry_cannot_vouch_for_a_missing_message_id`.
"""

from __future__ import annotations

import ast
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from tgfarm.db.models import Channel, CrosspostLog, OperatorLog, Post
from tgfarm.db.session import session_scope
from tgfarm.maintenance import (
    PUBLICATION_EVIDENCE_APPLY_ENV,
    _journal_recorded_identifiers,
    _row_has_publication_evidence,
    publication_evidence_repair,
    run_maintenance,
)
from tgfarm.s7_publish import _dry_run_remote_id, has_publication_evidence
from tgfarm.s8_analytics.formats import EVIDENCE_RULES, RULE_JOURNAL, RULE_MESSAGE_ID

REPO_ROOT = Path(__file__).resolve().parents[1]
MAINTENANCE_PATH = REPO_ROOT / "tgfarm" / "maintenance.py"

#: A destination the live journal writes to but no evidence rule covers.
UNRULED_PLATFORM = "max"

#: The journal statuses a fixture may leave behind. `None` is "no entry at all".
JOURNAL_STATUSES = (None, "posted", "scheduled", "failed", "dry_run")

#: Which identifier that entry carries. `canonical_placeholder` is exactly what the
#: off-mode adapters journal for *this* platform and post, `other_placeholder` is the
#: same shape for a different platform, `real` is what a platform returns when it
#: accepts something.
REMOTE_KINDS = ("null", "empty", "canonical_placeholder", "other_placeholder", "real")

#: The other platform whose placeholder shape the fixture borrows for each cell.
_OTHER_PLATFORM_FOR = {"telegram": "dzen", "dzen": "vk", "vk": "dzen", UNRULED_PLATFORM: "dzen"}

#: The repair step and the helpers it owns the decision with — the region the drift
#: guard reads. Named here so a rename trips the anchor, not a silent pass.
REPAIR_REGION = (
    "publication_evidence_repair",
    "_publication_evidence_repair",
    "_row_has_publication_evidence",
    "_journal_recorded_identifiers",
    "_posted_without_reference",
)


def _slug() -> str:
    return uuid4().hex[:8]


def _cells() -> list[dict]:
    """Every representable combination, as the plan the two predicates share.

    `no journal entry` and `identifier on an entry` cannot coexist: an identifier is
    a column of `crosspost_logs`, and `posts` has none for anything but Telegram.
    Those combinations are skipped rather than faked, and
    :func:`test_no_post_level_identifier_column_exists` pins why.
    """
    plan: list[dict] = []
    for platform in (*EVIDENCE_RULES, UNRULED_PLATFORM):
        for message_id in (None, 4242):
            for status in JOURNAL_STATUSES:
                for kind in REMOTE_KINDS:
                    if status is None and kind != "null":
                        continue
                    plan.append(
                        {
                            "platform": platform,
                            "message_id": message_id,
                            "status": status,
                            "kind": kind,
                            "name": f"{platform}|msg={message_id}|journal={status}|id={kind}",
                        }
                    )
    return plan


CELLS = _cells()

_seeded_channels: list[str] = []
_seeded_public_ids: list[str] = []


@pytest.fixture(autouse=True)
def _drop_own_rows():
    """Leave no residue: the suite shares one session database."""
    yield
    with session_scope() as session:
        if _seeded_public_ids:
            session.execute(
                delete(CrosspostLog).where(CrosspostLog.post_public_id.in_(_seeded_public_ids))
            )
            post_ids = list(
                session.scalars(select(Post.id).where(Post.public_id.in_(_seeded_public_ids))).all()
            )
            if post_ids:
                session.execute(delete(Post).where(Post.id.in_(post_ids)))
        if _seeded_channels:
            session.execute(delete(Channel).where(Channel.id.in_(_seeded_channels)))
    _seeded_channels.clear()
    _seeded_public_ids.clear()


def _remote_id(cell: dict, post_id: int) -> str | None:
    """The identifier the fixture asks the journal to hold for this cell."""
    kind = cell["kind"]
    if kind == "null":
        return None
    if kind == "empty":
        return ""
    if kind == "canonical_placeholder":
        return _dry_run_remote_id(cell["platform"], post_id)
    if kind == "other_placeholder":
        return _dry_run_remote_id(_OTHER_PLATFORM_FOR[cell["platform"]], post_id)
    return f"real-{post_id}-id"


def _seed() -> dict[str, dict]:
    """Create every cell in one transaction; hand back what each row became."""
    seeded: dict[str, dict] = {}
    with session_scope() as session:
        made: list[tuple[dict, Post]] = []
        for cell in CELLS:
            suffix = _slug()
            channel_id = f"ev_{cell['platform']}_{suffix}"
            public_id = f"ev{suffix}"
            _seeded_channels.append(channel_id)
            _seeded_public_ids.append(public_id)
            post = Post(
                public_id=public_id,
                channel_id=channel_id,
                hook="Заголовок",
                body="Тело поста.",
                status="published",
                published_at=datetime.now(UTC) - timedelta(minutes=90),
                tg_message_id=cell["message_id"],
                generation_provider="offline",
            )
            session.add(Channel(id=channel_id, title="Evidence matrix", niche="test",
                                platform=cell["platform"]))
            session.add(post)
            made.append((cell, post))
        session.flush()
        for cell, post in made:
            post_id = int(post.id)
            if cell["status"] is not None:
                session.add(
                    CrosspostLog(
                        post_public_id=str(post.public_id),
                        platform=cell["platform"],
                        status=cell["status"],
                        remote_id=_remote_id(cell, post_id),
                        url=None,
                    )
                )
            seeded[cell["name"]] = {**cell, "post_id": post_id, "public_id": str(post.public_id)}
    return seeded


# ------------------------------------------------------------- the two predicates
def _paraphrase_candidate(cell: dict) -> bool:
    """The baseline selection rule of `publication_evidence_repair`, verbatim.

    Reproduced here and nowhere else — this is the copy that had to stop existing in
    production code. It swept the three platforms it named itself, judged a telegram
    row from the message-id column alone, and a dzen/vk row from the journal's
    accepted status alone.
    """
    if cell["platform"] not in ("telegram", "dzen", "vk"):
        return False
    if cell["platform"] == "telegram":
        return cell["message_id"] is None
    return cell["status"] != "posted"


def _evaluate_matrix(seeded: dict[str, dict]) -> dict[str, tuple[bool, bool | None, bool]]:
    """For every cell: `(paraphrase, delegated, owner_vouches)`.

    The journal read is batched per platform — the same shape the sweep itself uses —
    so this proves the production code path, not a per-row re-implementation of it.

    `delegated` is `None` for a platform `EVIDENCE_RULES` does not rule, because that
    is the one question the sweep does not ask: the candidate set is built by
    iterating the table, so :func:`_row_has_publication_evidence` is never reached
    for such a row. Inventing a verdict here would compare the two copies on a cell
    neither of them owns; :func:`test_the_unruled_platform_is_never_swept_by_either_copy`
    is where that platform is actually asserted on.
    """
    with session_scope() as session:
        identifiers: dict[str, dict[str, list[object]]] = {}
        for platform in {cell["platform"] for cell in seeded.values()}:
            public_ids = [cell["public_id"] for cell in seeded.values() if cell["platform"] == platform]
            identifiers[platform] = _journal_recorded_identifiers(session, platform, public_ids)
        verdicts: dict[str, tuple[bool, bool | None, bool]] = {}
        for cell in seeded.values():
            platform = cell["platform"]
            recorded = identifiers[platform].get(cell["public_id"])
            rule = EVIDENCE_RULES.get(platform)
            delegated = (
                None
                if rule is None
                else not _row_has_publication_evidence(
                    session,
                    platform=platform,
                    rule=rule,
                    post_id=int(cell["post_id"]),
                    public_id=cell["public_id"],
                    message_id=cell["message_id"],
                    identifiers=recorded,
                )
            )
            owner = has_publication_evidence(
                session,
                public_id=cell["public_id"],
                platform=platform,
                post_id=int(cell["post_id"]),
                message_id=cell["message_id"],
                remote_id=next(iter(recorded), None) if recorded else None,
            )
            verdicts[cell["name"]] = (_paraphrase_candidate(cell), delegated, owner)
    return verdicts


def _expected_divergences() -> set[str]:
    """The cells where the paraphrase dropped a fact the owner still weighs.

    Read cell by cell against both predicates, and none of it is this file's opinion:
    :func:`test_the_two_predicates_differ_exactly_where_the_paraphrase_dropped_a_fact`
    re-asks the owner for every row listed here.

    * **no entry at all, or an entry the journal classifies as never-sent**
      (`dry_run`): the identifier is not evidence to the owner either, so the sweep
      keeps repairing the row exactly as the paraphrase did. That is the
      `tests/test_maintenance_publication_evidence.py::test_dry_run_journal_is_not_evidence`
      case, and it is why the identifier read filters on
      `crosspost_journal`'s class instead of trusting the column;
    * **an unruled platform**: the sweep does not ask about it at all, from either
      copy, so it has no divergence to declare —
      :func:`test_the_unruled_platform_is_never_swept_by_either_copy` holds that;
    * **telegram** (`RULE_MESSAGE_ID`) — no divergence is possible any more, and
      that is derived from the rule rather than from a list of names. The owner
      holds this platform to the message id *alone*: `has_publication_evidence`
      returns `False` for a telegram row without one before it ever reads
      `remote_id` or the journal, because `s9_growth.adapters.ADAPTERS` has no
      `telegram` key and `tg_sync.collect_post_stats` pairs `(chat id,
      tg_message_id)`, so such a row stays unmeasurable whatever a journal entry
      asserts. The paraphrase weighed that same single fact, so the two agree on
      every telegram cell. Until the owner was narrowed on 2026-11-06 an accepted
      entry or a recorded identifier did vouch here and 9 cells diverged;
      :func:`test_a_telegram_journal_entry_cannot_vouch_for_a_missing_message_id`
      pins the new rule positively, so re-widening the owner fails there and in
      the matrix equality alike;
    * **dzen / vk** (`RULE_JOURNAL`) — the paraphrase weighed the accepted entry and
      nothing else. An entry the journal does not disclaim but that never reached
      `posted` — a `scheduled` or `failed` row that still carries the identifier the
      adapter parsed — is evidence to the owner only, whatever the message id holds.
      `posted` itself the two agree on, and a Telegram message id is not proof of a
      Dzen article, which is the `EVIDENCE_RULES` half of the delegation.
    """
    expected: set[str] = set()
    for cell in CELLS:
        rule = EVIDENCE_RULES.get(cell["platform"])
        if rule is None or cell["status"] in (None, "dry_run"):
            continue
        accepted_entry = cell["status"] == "posted"
        # This platform's own placeholder is the adapter saying it invented the value;
        # another platform's placeholder shape is not a placeholder here.
        recorded_identifier = cell["kind"] in ("real", "other_placeholder")
        if rule == RULE_MESSAGE_ID:
            # The owner's fact set on this platform is exactly the paraphrase's:
            # the message id, and nothing else. Neither `accepted_entry` nor
            # `recorded_identifier` can vouch for it, so no cell here diverges.
            # Stated as a property of the rule, not as nine deleted names: if the
            # owner is ever loosened to consult the journal again, the matrix grows
            # telegram divergences this set does not contain and the equality
            # assertion in the deliverable below fails loudly.
            continue
        if not accepted_entry and recorded_identifier:
            expected.add(cell["name"])
    return expected


# ----------------------------------------------------------------------- the matrix
def test_the_matrix_covers_every_ruled_platform_and_one_that_is_not():
    platforms = {cell["platform"] for cell in CELLS}
    assert platforms == set(EVIDENCE_RULES) | {UNRULED_PLATFORM}
    assert len(CELLS) == len(platforms) * 2 * (1 + (len(JOURNAL_STATUSES) - 1) * len(REMOTE_KINDS))
    assert RULE_JOURNAL != RULE_MESSAGE_ID
    assert EVIDENCE_RULES["telegram"] == RULE_MESSAGE_ID
    assert {EVIDENCE_RULES[p] for p in ("dzen", "vk")} == {RULE_JOURNAL}


def test_no_post_level_identifier_column_exists():
    """Why "identifier on an entry" and "no entry at all" cannot coexist.

    The brief's caveat was that the owner's second fact may be unreachable for
    Dzen/VK. It is unreachable *from the post* — there is no column — and reachable
    *from the journal*, which is where those publishers leave their identifier.
    """
    post_columns = {column.name for column in Post.__table__.columns}
    assert "tg_message_id" in post_columns
    assert not {name for name in post_columns if "remote" in name or "dzen" in name or "vk" in name}
    assert "remote_id" in {column.name for column in CrosspostLog.__table__.columns}


def test_the_two_predicates_differ_exactly_where_the_paraphrase_dropped_a_fact():
    """The deliverable: which rows the paraphrase got wrong, and who is right."""
    seeded = _seed()
    verdicts = _evaluate_matrix(seeded)
    asked = {name for name, (_, delegated, _) in verdicts.items() if delegated is not None}
    assert asked == {name for name, cell in seeded.items() if cell["platform"] in EVIDENCE_RULES}
    divergent = sorted(
        name
        for name, (old, delegated, _) in verdicts.items()
        if name in asked and delegated != old
    )
    expected = sorted(_expected_divergences())
    assert divergent == expected, set(divergent) ^ set(expected)
    assert divergent, "the paraphrase and the owner agreed everywhere; the fix proved nothing"
    # Every cell the paraphrase picked and the owner released is a row the sweep used
    # to rewrite into `publish_unknown` while the publisher's own predicate vouched
    # for it. That is the operator-visible cost of the copy, and the owner is right.
    assert all(verdicts[name][2] for name in divergent), divergent


def _seed_telegram_row_with_accepted_entry(message_id: int | None) -> dict:
    """One `published` telegram row whose journal entry is as strong as it gets.

    Seeded outside the matrix so the rule below is asserted on a single row an
    operator could actually hold, and registered with :func:`_drop_own_rows` so it
    leaves no residue in the shared session database. The entry is `posted` and its
    identifier is the fixture's `real` kind — a value the owner's second fact accepts
    on any platform it is allowed to weigh.
    """
    suffix = _slug()
    channel_id = f"ev_tgrule_{suffix}"
    public_id = f"evt{suffix}"
    _seeded_channels.append(channel_id)
    _seeded_public_ids.append(public_id)
    with session_scope() as session:
        session.add(Channel(id=channel_id, title="Telegram rule", niche="test", platform="telegram"))
        post = Post(
            public_id=public_id,
            channel_id=channel_id,
            hook="Заголовок",
            body="Тело поста.",
            status="published",
            published_at=datetime.now(UTC) - timedelta(minutes=90),
            tg_message_id=message_id,
            generation_provider="offline",
        )
        session.add(post)
        session.flush()
        post_id = int(post.id)
        remote_id = _remote_id({"platform": "telegram", "kind": "real"}, post_id)
        session.add(
            CrosspostLog(
                post_public_id=public_id,
                platform="telegram",
                status="posted",
                remote_id=remote_id,
                url=None,
            )
        )
    return {
        "post_id": post_id,
        "public_id": public_id,
        "message_id": message_id,
        "remote_id": remote_id,
    }


def test_a_telegram_journal_entry_cannot_vouch_for_a_missing_message_id():
    """The narrowed telegram rule, pinned positively rather than merely tolerated.

    The divergence set lost its 9 telegram cells because the owner stopped
    consulting the journal for telegram at all. An empty expectation would go just
    as green if the owner were *deleted*, so this asserts the rule itself: a
    `published` telegram row with no `tg_message_id` has no publication evidence even
    when the journal holds an accepted `posted` entry carrying a real remote id, and
    the same row *with* a message id does. Asked through the production owner and
    through the sweep's delegate, so loosening either fails here loudly.

    Why the owner is right: `s9_growth.adapters.ADAPTERS` has no `telegram` key (the
    bot path is the only Telegram publisher), so a telegram `posted` row can only be
    legacy or hand-written; and `tg_sync.collect_post_stats` pairs `(chat id,
    tg_message_id)`, so a publication with no id stays unmeasurable whatever the
    journal asserts.
    """
    rule = EVIDENCE_RULES["telegram"]
    assert rule == RULE_MESSAGE_ID

    for message_id, evidenced in ((None, False), (4242, True)):
        row = _seed_telegram_row_with_accepted_entry(message_id)
        # The premise, so a False below is the rule and not a row that never existed:
        # the identifier is real, i.e. not the placeholder the owner discounts.
        assert row["remote_id"]
        assert row["remote_id"] != _dry_run_remote_id("telegram", row["post_id"])
        with session_scope() as session:
            stored = session.execute(
                select(Post).where(Post.public_id == row["public_id"])
            ).scalar_one()
            assert stored.status == "published"
            assert stored.tg_message_id == message_id
            recorded = _journal_recorded_identifiers(session, "telegram", [row["public_id"]])
            identifiers = recorded.get(row["public_id"])
            assert identifiers == [row["remote_id"]], identifiers

            owner = has_publication_evidence(
                session,
                public_id=row["public_id"],
                platform="telegram",
                post_id=row["post_id"],
                message_id=message_id,
                remote_id=identifiers[0],
            )
            delegated = _row_has_publication_evidence(
                session,
                platform="telegram",
                rule=rule,
                post_id=row["post_id"],
                public_id=row["public_id"],
                message_id=message_id,
                identifiers=identifiers,
            )
        assert owner is evidenced, (message_id, owner)
        assert delegated is evidenced, (message_id, delegated)
        assert delegated == owner, (message_id, delegated, owner)
        # The two copies agree on this cell now, and that agreement is what removed
        # the telegram divergences — asserted here rather than assumed.
        cell = {"platform": "telegram", "message_id": message_id, "status": "posted"}
        assert _paraphrase_candidate(cell) is (not delegated), message_id


def test_dry_run_entries_are_never_mistaken_for_proof():
    """The one direction where a naive delegation would have been wrong.

    `s9_growth/__init__.py:610` journals a dry-run crosspost with a hash for a
    `remote_id`, which is not the placeholder shape `has_publication_evidence`
    recognises. Handed to its second fact it reads as "the platform returned an
    identifier" and spares a publication the farm knows it never sent — so the
    identifier read asks `crosspost_journal` which entries the pair re-opens from,
    and those never reach the owner.
    """
    seeded = _seed()
    verdicts = _evaluate_matrix(seeded)
    dry = {name: cell for name, cell in seeded.items() if cell["status"] == "dry_run"}
    assert dry
    with session_scope() as session:
        for name, cell in dry.items():
            recorded = _journal_recorded_identifiers(
                session, cell["platform"], [cell["public_id"]]
            )
            assert recorded == {}, name
            old, delegated, _ = verdicts[name]
            if cell["platform"] not in EVIDENCE_RULES:
                # The identifier read is platform-generic, so the exclusion is
                # proved for `max` too; the sweep's verdict is not defined there.
                assert delegated is None, name
                continue
            assert delegated is old, name


def test_the_unruled_platform_is_never_swept_by_either_copy():
    """`max` is in the live journal and outside `EVIDENCE_RULES`.

    A row nobody can disprove is not a row proven false: it stays published and
    stays out of the report entirely. Both the baseline tuple of three names and the
    delegated enumeration of the table are asserted not to claim it.
    """
    base = publication_evidence_repair()
    seeded = _seed()
    report = publication_evidence_repair()
    unruled = [cell for cell in seeded.values() if cell["platform"] == UNRULED_PLATFORM]
    assert unruled
    assert set(report["by_platform"]) == set(EVIDENCE_RULES)
    assert UNRULED_PLATFORM not in report["by_platform"]
    for cell in unruled:
        assert cell["public_id"] not in report["sample"]
    assert int(report["by_platform"]["telegram"]) >= int(base["by_platform"]["telegram"])


def test_the_sweep_selects_what_the_matrix_says_platform_by_platform():
    """The step, not the helper: its per-platform counts are the matrix's counts."""
    base = publication_evidence_repair()
    seeded = _seed()
    verdicts = _evaluate_matrix(seeded)
    report = publication_evidence_repair()

    expected = dict.fromkeys(EVIDENCE_RULES, 0)
    for cell in seeded.values():
        if cell["platform"] in expected and verdicts[cell["name"]][1] is True:
            expected[cell["platform"]] += 1
    for platform, count in expected.items():
        delta = int(report["by_platform"][platform]) - int(base["by_platform"][platform])
        assert delta == count, (platform, delta, count)
    assert report["scanned"] == base["scanned"] + sum(expected.values())
    assert report["sample"] or not expected


def test_the_journal_read_batches_and_keeps_the_placeholder_verbatim():
    """What the sweep hands the owner: every value, deduplicated, in write order.

    The helper is platform-generic — it does not know about the evidence rules, that
    is the sweep's question — so the same expectation holds for `max`, which the
    sweep never asks it about.
    """
    seeded = _seed()
    with session_scope() as session:
        for cell in seeded.values():
            if cell["status"] is None:
                continue
            recorded = _journal_recorded_identifiers(session, cell["platform"], [cell["public_id"]])
            values = recorded.get(cell["public_id"])
            if cell["status"] == "dry_run":
                assert values is None, cell["name"]
                continue
            if cell["kind"] == "null":
                assert values == [None], cell["name"]
            elif cell["kind"] == "empty":
                assert values == [""], cell["name"]
            else:
                assert values == [_remote_id(cell, cell["post_id"])], cell["name"]


def test_imported_history_stays_out_of_the_repair_set(tmp_path, monkeypatch):
    """The exclusion survived the rewrite: archive rows are told, not published."""
    from tgfarm.core import settings as settings_mod
    from tgfarm.maintenance import IMPORTED_HISTORY_PROVIDERS

    suffix = _slug()
    channel_id = f"ev_imported_{suffix}"
    public_id = f"evi{suffix}"
    _seeded_channels.append(channel_id)
    _seeded_public_ids.append(public_id)
    with session_scope() as session:
        session.add(Channel(id=channel_id, title="Imported", niche="test", platform="dzen"))
        session.add(
            Post(
                public_id=public_id,
                channel_id=channel_id,
                hook="Заголовок",
                body="Тело.",
                status="published",
                published_at=datetime.now(UTC) - timedelta(days=400),
                generation_provider=IMPORTED_HISTORY_PROVIDERS[0],
            )
        )

    monkeypatch.setenv(PUBLICATION_EVIDENCE_APPLY_ENV, "1")
    settings = settings_mod.get_settings()
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path), raising=False)
    (tmp_path / f"tgfarm-{_slug()}.db").write_bytes(b"copy")

    report = publication_evidence_repair(apply=True, limit=500)
    assert public_id not in report["sample"]
    with session_scope() as session:
        row = session.execute(select(Post).where(Post.public_id == public_id)).scalar_one()
        assert row.status == "published", "импортированная история не переписывается"
        assert (
            session.execute(
                select(OperatorLog).where(OperatorLog.target == public_id[:64])
            ).scalar_one_or_none()
            is None
        )


# --------------------------------------------------------------------- consent gate
def test_the_step_cannot_write_when_the_consent_flag_is_off(tmp_path, monkeypatch):
    """Whatever the selection looks like, the gate sits in front of it.

    The flag is the operator's consent to rewrite history (§80) and it is still
    pending for a live run. A fresh copy in `backup/` is planted on purpose: it
    satisfies the *other* precondition, so a write here could only have come from a
    missing consent check.
    """
    from tgfarm.core import settings as settings_mod

    monkeypatch.delenv(PUBLICATION_EVIDENCE_APPLY_ENV, raising=False)
    settings = settings_mod.get_settings()
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path), raising=False)
    (tmp_path / f"tgfarm-{_slug()}.db").write_bytes(b"copy")

    seeded = _seed()
    before = publication_evidence_repair()
    report = publication_evidence_repair(apply=True, limit=500)

    assert report["consent"] is False
    assert report["apply"] is True
    assert report["moved"] == 0
    assert report["cleared_chat_ids"] == 0
    assert "refused" in report and PUBLICATION_EVIDENCE_APPLY_ENV in str(report["refused"])
    assert report["by_platform"] == before["by_platform"], "the dry report shape held"
    assert report["scanned"] == before["scanned"]
    for key in (
        "apply",
        "consent",
        "limit",
        "moved",
        "cleared_chat_ids",
        "posted_without_id",
        "imported_history_excluded",
        "placeholder_chat_ids_all",
        "by_platform",
        "sample",
        "remaining",
        "scanned",
    ):
        assert key in report, key

    with session_scope() as session:
        statuses = session.execute(
            select(Post.status).where(
                Post.public_id.in_([cell["public_id"] for cell in seeded.values()])
            )
        ).all()
        assert {str(row[0]) for row in statuses} == {"published"}
        # Scoped to this fixture's own rows: other files in the suite do apply the
        # step under consent, and a repair log of theirs is not this step writing.
        repaired = session.execute(
            select(OperatorLog.target).where(
                OperatorLog.action == "publication-evidence-repair",
                OperatorLog.target.in_([cell["public_id"][:64] for cell in seeded.values()]),
            )
        ).all()
        assert not repaired, repaired


def test_the_cycle_hook_still_returns_zero_without_consent(monkeypatch):
    monkeypatch.delenv(PUBLICATION_EVIDENCE_APPLY_ENV, raising=False)
    _seed()
    assert run_maintenance()["publication_evidence_repair"] == 0


# ---------------------------------------------------------------------- drift guard
def _function_nodes(tree: ast.AST) -> dict[str, ast.AST]:
    return {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}


def _docstring_ids(tree: ast.AST) -> set[int]:
    """Node ids of strings that are documentation rather than a value.

    Prose may *name* the placeholder vocabulary; code may not rebuild it.
    """
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                found.add(id(body[0].value))
    return found


def _mentions_journal_status(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Attribute) and child.attr == "status":
            holder = child.value
            name = getattr(holder, "id", None) or getattr(holder, "attr", None) or ""
            if "rosspost" in str(name):
                return True
    return False


def _mentions_fact(node: ast.AST, banned: tuple[str, ...]) -> bool:
    """Does this subtree read one of the three facts — by column, key or name?"""
    for child in ast.walk(node):
        if isinstance(child, ast.Attribute) and child.attr in banned:
            return True
        if isinstance(child, ast.Name) and child.id in banned:
            return True
        if isinstance(child, ast.Subscript) and isinstance(child.slice, ast.Constant):
            if child.slice.value in banned:
                return True
    return False


def _violations(source: str, *, only: tuple[str, ...] | None = None) -> list[str]:
    """Every restatement of an evidence fact in `tgfarm/maintenance.py`.

    Three rules, chosen so the baseline paraphrase trips all of them and no
    legitimate use of the same columns does:

    * a value-literal, anywhere in the file, that spells a dry-run placeholder
      (`dry-`) or the journal's accepted status on its own — both vocabularies belong
      to another module, and a sweep that has to re-spell one has outgrown the owner;
    * inside the repair region, a comparison (`==`, `is None`, `.is_(None)`,
      `.in_(...)`, `like`, `between`) that reads a platform identifier, a telegram
      message id or the journal's status column: those are the three facts, and only
      their owner may weigh them. Reading one into a variable, a select list or a
      keyword argument is not a comparison — that is exactly the line between
      plumbing and a second opinion;
    * the region is resolved by function name, so a rename fails the anchor below
      rather than shrinking the scan to nothing.

    Why the comparison rule is region-scoped: `requeue_eligibility` also reads a
    message id, and it answers a *different* question ("may this go back in the
    queue") with its own owner. Narrowing it is not this block's decision.

    Known limit, stated on purpose: a placeholder assembled at runtime from parts, or
    a fact weighed in a function outside the region, is beyond static reach — the
    same limitation class the journal-status width scanner declares. The positive
    anchors in :func:`test_the_repair_step_asks_the_owner_by_name` and the matrix
    above are what keep that channel narrow.
    """
    tree = ast.parse(source)
    docstrings = _docstring_ids(tree)
    functions = _function_nodes(tree)
    region = tuple(only) if only is not None else REPAIR_REGION
    missing = sorted(set(region) - set(functions))
    if missing:
        return [f"the scanned region lost {missing}: the guard is aimed at nothing"]

    banned_columns = ("tg_message_id", "remote_id")
    problems: list[str] = []

    def literal_check(node: ast.AST, where: str) -> None:
        for child in ast.walk(node):
            if id(child) in docstrings or not isinstance(child, ast.Constant):
                continue
            value = child.value
            if not isinstance(value, str):
                continue
            if "dry-" in value:
                problems.append(f"{where}: spells the dry-run placeholder vocabulary")
            if value.strip(" `\"'") == "posted":
                problems.append(f"{where}: spells the journal's accepted status")

    def comparison_check(node: ast.AST, where: str) -> None:
        for child in ast.walk(node):
            if isinstance(child, ast.Compare):
                sides = (child.left, *child.comparators)
                if any(_mentions_fact(side, banned_columns) for side in sides):
                    problems.append(f"{where}: weighs a platform identifier or message id")
                if any(_mentions_journal_status(side) for side in sides):
                    problems.append(f"{where}: weighs the journal's status column")
            elif isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
                if child.func.attr not in {"is_", "isnot", "in_", "notin_", "like", "ilike", "between"}:
                    continue
                target = child.func.value
                if _mentions_fact(target, banned_columns) or _mentions_journal_status(target):
                    problems.append(f"{where}: turns a single fact into a predicate")

    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue  # module docstring
        literal_check(node, f"{node.__class__.__name__}@{getattr(node, 'lineno', 0)}")
    for name in region:
        where = f"def {name}()"
        comparison_check(functions[name], where)
        if only is not None:
            literal_check(functions[name], where)
    return problems


def test_maintenance_no_longer_restates_the_evidence_predicate():
    """The paraphrase cannot come back: no facts of its own, only the owner's answer."""
    source = MAINTENANCE_PATH.read_text(encoding="utf-8")
    assert _violations(source) == []


def test_the_repair_region_carries_no_restatement_of_its_own():
    """Same scan, narrowed to the five functions that answer the evidence question."""
    source = MAINTENANCE_PATH.read_text(encoding="utf-8")
    assert _violations(source, only=REPAIR_REGION) == []


def test_the_repair_step_asks_the_owner_by_name():
    """The anchor: the guard above is not passing because it found nothing.

    Each name below is the single owner of one decision the repair step used to make
    for itself, and the scan requires the call *and* the platform table, so a
    deletion that leaves the file "clean" fails here instead.
    """
    source = MAINTENANCE_PATH.read_text(encoding="utf-8")
    functions = _function_nodes(ast.parse(source))
    assert set(REPAIR_REGION) <= set(functions), "the guard's region lost a function"

    def body(name: str) -> str:
        return ast.get_source_segment(source, functions[name]) or ""

    assert "has_publication_evidence" in body("_row_has_publication_evidence")
    assert "EVIDENCE_RULES" in body("publication_evidence_repair")
    assert "RULE_JOURNAL" in body("publication_evidence_repair")
    assert "RULE_MESSAGE_ID" in body("_row_has_publication_evidence")
    assert "journal_status_class" in body("_journal_recorded_identifiers")
    assert "_proof_condition" in body("_posted_without_reference")
    assert "_attributed_condition" in body("_posted_without_reference")
    # The platform list the file used to own is gone, in any spelling.
    assert '"telegram", "dzen", "vk"' not in source
    assert not re.search(r"for platform in \(", source)


def test_the_guard_is_aimed_at_the_paraphrase_it_replaces():
    """The baseline code fails its own guard, so the guard has teeth.

    Quoted from `tgfarm/maintenance.py` at `19d1b09` — the statements that answered
    the evidence question inside the sweep.
    """
    baseline = '''
def publication_evidence_repair():
    if platform == "telegram":
        rows = session.execute(base.where(PostRow.tg_message_id.is_(None))).all()
    else:
        journaled = (
            select(CrosspostLog.id)
            .where(
                CrosspostLog.post_public_id == PostRow.public_id,
                CrosspostLog.platform == platform,
                CrosspostLog.status == "posted",
            )
            .exists()
        )
        rows = session.execute(base.where(not_(journaled))).all()
        posted_without_ref = (
            select(func.count(func.distinct(PostRow.id)))
            .where(
                CrosspostLog.status == "posted",
                or_(CrosspostLog.remote_id.is_(None), CrosspostLog.remote_id == ""),
            )
        )
'''
    problems = _violations(baseline, only=("publication_evidence_repair",))
    assert any("spells the journal's accepted status" in text for text in problems), problems
    assert any("weighs a platform identifier" in text for text in problems), problems
    assert any("weighs the journal's status column" in text for text in problems), problems
    assert any("turns a single fact into a predicate" in text for text in problems), problems
    assert len(problems) >= 4, problems


def test_the_guard_reports_a_renamed_region_instead_of_passing():
    """A guard whose region disappears must fail, not go green."""
    problems = _violations("def something_else():\n    return 1\n", only=REPAIR_REGION)
    assert problems and "lost" in problems[0]


def test_the_shared_owner_is_the_only_copy_of_the_decision_in_the_package():
    """One definition of the predicate, one definition of the platform table."""
    predicate: list[str] = []
    table: list[str] = []
    for path in sorted((REPO_ROOT / "tgfarm").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "has_publication_evidence":
                predicate.append(path.relative_to(REPO_ROOT).as_posix())
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name) and target.id == "EVIDENCE_RULES":
                        table.append(path.relative_to(REPO_ROOT).as_posix())
    assert predicate == ["tgfarm/s7_publish/__init__.py"], predicate
    assert table == ["tgfarm/s8_analytics/formats.py"], table
