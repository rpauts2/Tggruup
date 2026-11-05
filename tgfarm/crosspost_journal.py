"""The crosspost journal's status vocabulary — one source for every reader.

`crosspost_logs` is append-only history. The newest row of a
(post, platform) pair carries a `status` token, and that token decides one
question that two different components must answer *identically*: may this pair
be delivered again?

The consumers are:

* `tgfarm.distribution.build_distribution_plan` — decides whether the planner
  offers the pair to the platform again (the queue the operator sees);
* `tgfarm.s9_growth.adapters.already_done` and `.begin_crosspost` — the claim
  gate that decides whether an automatic or manual send may take the durable
  `crosspost_claim` row and write a `publishing` journal entry.

The planner is a *view*: it can be wrong and simply stop offering. The claim gate
is the *lock*: if it says "go" while the planner says "no", the pair can still be
sent by whatever path skips the planner (the direct TikTok/Douyin and Reddit
endpoints call `begin_crosspost` without consulting a plan). So the two must not
own two copies of the answer with two different defaults, and this module is the
only place the answer is written down.

Why this module lives here
--------------------------
It is a leaf: it imports nothing from this project except the standard library.
That is the whole point. `distribution` already keeps its dependency on the
adapters function-local (`from tgfarm.s9_growth.adapters import ...` inside
`build_distribution_plan`), and `s9_growth/__init__.py` keeps its dependency on
`distribution` function-local for the same reason — the two would otherwise form
an import cycle. Any home on either side of that pair (or in `tgfarm.core.*`,
which would have to know about delivery semantics) forces at least one consumer
back to a lazy import or a duplicated literal. A neutral leaf lets both
consumers import the vocabulary at module level and keeps the sets, the
normalisation and the default in one file.

The re-offer rule
-----------------
The vocabulary below is not aspirational: every string is a status a writer in
this repository can actually put in the column. Three writers exist —
`s9_growth.adapters.log_result`, whose status comes from the `CrosspostResult`
model and is validated against
`posted | scheduled | skipped | failed | permanent | unknown | dry_run`;
direct row inserts `adapters.begin_crosspost` (writes `publishing` before any
network call) and `api._reconcile_crosspost_outcome` (writes `posted`, operator
confirmed, or `retry_authorized`, operator inspected the destination and
authorized a retry). A status outside that set therefore means a row written by
something this codebase has never met, not a typo in a test.

SUPPRESSED — the pair stays out of the queue indefinitely, until an operator
    reconciles it. `posted`/`scheduled` mean the destination has it or will have
    it; `publishing` means a claim is in flight (a crash leaves this row as the
    newest one, and re-offering would double-send); `unknown` means the request
    may have been accepted before the answer was lost; `permanent` means the
    destination refused this exact content.
TIME-BOUNDED — `failed` re-opens once its cooldown passed *and* the attempt
    ceiling is not reached; `skipped` re-opens once `retry_after_at` passed.
    A skipped row with no `retry_after_at` is unmeasured, not immediate: the
    journal never promised a moment, so the pair is offered again.
RE-OPEN — `dry_run` re-opens when the operator switches to LIVE (a dry-run row
    was never sent anywhere); `retry_authorized` re-opens immediately because
    re-opening *is* the action the operator authorized.
UNKNOWN — anything else. A row whose meaning this code does not know may already
    be an accepted publication, so both consumers fail closed: the planner stops
    offering the pair and says so in the queue reason, and the claim gate
    refuses the claim and logs why. A silently refused claim is how this class
    of defect hides, so the refusal is never quiet.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

# Status tokens named by the vocabulary, so a consumer that must branch on a
# single status can compare against the same string the sets below are built
# from instead of writing the literal a second time.
JOURNAL_STATUS_FAILED = "failed"
JOURNAL_STATUS_SKIPPED = "skipped"
JOURNAL_STATUS_DRY_RUN = "dry_run"
JOURNAL_STATUS_RETRY_AUTHORIZED = "retry_authorized"
JOURNAL_STATUS_PUBLISHING = "publishing"
#: The one journal status that *proves* a publication, so the code that decides
#: whether a post may be called `published` names it rather than respelling it.
JOURNAL_STATUS_POSTED = "posted"

JOURNAL_SUPPRESSED_STATUSES = frozenset(
    {JOURNAL_STATUS_POSTED, "scheduled", "unknown", JOURNAL_STATUS_PUBLISHING, "permanent"}
)
JOURNAL_TIME_BOUNDED_STATUSES = frozenset({JOURNAL_STATUS_FAILED, JOURNAL_STATUS_SKIPPED})
JOURNAL_REOPEN_STATUSES = frozenset({JOURNAL_STATUS_DRY_RUN, JOURNAL_STATUS_RETRY_AUTHORIZED})

#: Every status the readers below understand. A status outside this set is
#: deliberately treated as a publication that may already have happened.
JOURNAL_KNOWN_STATUSES = (
    JOURNAL_SUPPRESSED_STATUSES | JOURNAL_TIME_BOUNDED_STATUSES | JOURNAL_REOPEN_STATUSES
)

JOURNAL_UNKNOWN_STATUS_REASON = (
    "В журнале есть запись со статусом, который планировщик не распознаёт; "
    "материал не предлагается повторно, чтобы не опубликовать дубль."
)


class JournalStatusClass(Enum):
    """What a journal row's status says about re-offering the pair.

    The class is the decision; the per-status timing (a cooldown timestamp, an
    attempt ceiling, the LIVE switch, an adapter finally being configured) stays
    with the consumer, because the planner and the claim gate measure it with
    different clocks. Nothing outside this enum may decide the default.
    `NO_HISTORY` is not a status at all: it is the absence of a journal row, and
    only `journal_row_decision` can produce it.
    """

    SUPPRESSED = "suppressed"
    TIME_BOUNDED = "time_bounded"
    REOPEN = "reopen"
    UNKNOWN = "unknown"
    NO_HISTORY = "no_history"


def normalize_journal_status(raw: Any) -> str:
    """The status token of a journal row, normalised for the re-offer decision.

    One normalisation for every reader: an empty/missing status collapses to
    `""` and casing or padding cannot smuggle a known status past the
    fail-closed default in one component while the other one sees it.
    """
    return str(raw or "").strip().lower()


def journal_status_of(latest_log: Any) -> str:
    """The normalised status token of a journal row (or row-like object)."""
    return normalize_journal_status(getattr(latest_log, "status", latest_log))


def journal_status_class(status: Any) -> JournalStatusClass:
    """Classify a journal status; accepts a raw column value or a token."""
    normalised = normalize_journal_status(status)
    if normalised in JOURNAL_SUPPRESSED_STATUSES:
        return JournalStatusClass.SUPPRESSED
    if normalised in JOURNAL_TIME_BOUNDED_STATUSES:
        return JournalStatusClass.TIME_BOUNDED
    if normalised in JOURNAL_REOPEN_STATUSES:
        return JournalStatusClass.REOPEN
    return JournalStatusClass.UNKNOWN


def journal_row_decision(latest_log: Any) -> tuple[JournalStatusClass, str]:
    """Classify the newest journal row of a pair as `(class, normalised status)`.

    Both consumers call this instead of re-deriving it: `None` (the pair has no
    journalled history at all) is `NO_HISTORY`, *never* `UNKNOWN`. A fresh pair
    is claimable; a journalled pair nobody can explain is not, and conflating
    the two would block every first delivery.
    """
    if latest_log is None:
        return JournalStatusClass.NO_HISTORY, ""
    status = journal_status_of(latest_log)
    return journal_status_class(status), status


#: The two ways a journal row can be refused: nobody declared its status, or the
#: declaration and the consumer's dispatch disagree (a status was added to a
#: class without the timing branch being revisited). Both fail closed.
JOURNAL_REFUSAL_UNDECLARED_STATUS = "is not in the declared re-offer vocabulary"
JOURNAL_REFUSAL_UNCLASSIFIED_STATUS = (
    "belongs to a declared class that no consumer branch describes"
)


def journal_claim_refusal(
    post_public_id: str,
    platform: str,
    status: Any,
    cause: str,
) -> str:
    """The refusal text a claim gate logs instead of silently returning `None`.

    A silently refused claim is how this class of defect hides: the operator sees
    a destination that never sends and no record of why.
    """
    return (
        f"crosspost claim refused for {post_public_id}/{platform}: journal status "
        f"{normalize_journal_status(status)!r} {cause} "
        f"(declared: {sorted(JOURNAL_KNOWN_STATUSES)}); the row may already be an "
        "accepted publication, so the pair is not claimed. Give the status an "
        "explicit re-offer class in tgfarm/crosspost_journal.py and teach both "
        "the planner and the claim gate about it."
    )
