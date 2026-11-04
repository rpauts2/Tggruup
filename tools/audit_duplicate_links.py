"""Read-only audit of the `duplicate_of` links already written by the old contract.

Opens the live database in immutable/readonly mode and changes nothing: it only
counts how far the two-writer damage reached, which is the number the operator
has to see before any repair is allowed to write.
"""

from __future__ import annotations

import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")

DB = "tgfarm.db"

SQL_TOTAL_FLAGGED = "select count(*) from raw_items where duplicate_of is not null"

SQL_CHAINS = """
select count(*)
from raw_items r
join raw_items p on p.id = r.duplicate_of
where r.duplicate_of is not null and p.duplicate_of is not null
"""

SQL_MUTUAL = """
select count(*)
from raw_items r
join raw_items p on p.id = r.duplicate_of
where r.duplicate_of is not null and p.duplicate_of = r.id and r.id < p.id
"""

SQL_SELF = "select count(*) from raw_items where duplicate_of = id"

SQL_FORWARD = """
select count(*)
from raw_items r
join raw_items p on p.id = r.duplicate_of
where r.duplicate_of is not null
  and (coalesce(p.published_at, p.fetched_at), p.id)
      > (coalesce(r.published_at, r.fetched_at), r.id)
"""

#: The narrower reading of "backwards in time": a strictly later `published_at`,
#: ignoring the id tiebreak. Bulk imports share one second, so most of the count
#: above is ties — both numbers are reported rather than one of them chosen.
SQL_FORWARD_STRICT = """
select count(*)
from raw_items r
join raw_items p on p.id = r.duplicate_of
where r.duplicate_of is not null and p.published_at > r.published_at
"""

SQL_UNRESOLVED_TARGETS = """
select count(*)
from raw_items r
join raw_items p on p.id = r.duplicate_of
where r.duplicate_of is not null and p.duplicate_of is not null
"""

SQL_DISTINCT_TARGETS = """
select count(distinct r.duplicate_of)
from raw_items r
where r.duplicate_of is not null
"""

SQL_DANGLING = """
select count(*)
from raw_items r
left join raw_items p on p.id = r.duplicate_of
where r.duplicate_of is not null and p.id is null
"""

#: How many of the damaged rows are inside the window S2 will re-derive by itself
#: on the next cycle, i.e. would heal without a repair at all.
WINDOW_HOURS = 48
SQL_IN_WINDOW = f"""
select count(*)
from raw_items r
join raw_items p on p.id = r.duplicate_of
where r.duplicate_of is not null
  and p.duplicate_of is not null
  and coalesce(r.published_at, r.fetched_at) >= datetime('now', '-{WINDOW_HOURS} hours')
"""

#: What a repair would actually rewrite: a link is wrong when its target is
#: itself flagged (so the row does not point at the root) or when the target is
#: later than the row (the old newest-first pass wrote copies backwards).
#: `chains` and `forward_links` overlap, so the repair budget is this union.
SQL_REPAIR_ROWS = """
select count(*)
from raw_items r
join raw_items p on p.id = r.duplicate_of
where r.duplicate_of is not null
  and (
      p.duplicate_of is not null
      or (coalesce(p.published_at, p.fetched_at), p.id)
         > (coalesce(r.published_at, r.fetched_at), r.id)
  )
"""

#: Rows that are the root of at least one wrong link: the distinct places a
#: repair has to resolve to, and therefore the number of `resolve_duplicate_root`
#: walks the bounded pass will pay for.
SQL_REPAIR_ROOTS = """
select count(distinct r.duplicate_of)
from raw_items r
join raw_items p on p.id = r.duplicate_of
where r.duplicate_of is not null
  and (
      p.duplicate_of is not null
      or (coalesce(p.published_at, p.fetched_at), p.id)
         > (coalesce(r.published_at, r.fetched_at), r.id)
  )
"""

#: A cycle is not a chain: walking it never reaches a root, so the repair has to
#: name these rows and refuse them rather than follow them forever.
SQL_CYCLE_MEMBERS = """
with recursive walk(start, cur, depth) as (
    select r.id, r.duplicate_of, 1
    from raw_items r
    where r.duplicate_of is not null
    union all
    select w.start, n.duplicate_of, w.depth + 1
    from walk w
    join raw_items n on n.id = w.cur
    where n.duplicate_of is not null and w.depth < 64
)
select count(distinct start)
from walk
where cur = start and depth > 1
"""


def main() -> int:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cur = con.cursor()
    facts = {
        "rows_total": cur.execute("select count(*) from raw_items").fetchone()[0],
        "flagged": cur.execute(SQL_TOTAL_FLAGGED).fetchone()[0],
        "distinct_targets": cur.execute(SQL_DISTINCT_TARGETS).fetchone()[0],
        "chains": cur.execute(SQL_CHAINS).fetchone()[0],
        "mutual_pairs": cur.execute(SQL_MUTUAL).fetchone()[0],
        "self_links": cur.execute(SQL_SELF).fetchone()[0],
        "dangling_targets": cur.execute(SQL_DANGLING).fetchone()[0],
        "forward_links": cur.execute(SQL_FORWARD).fetchone()[0],
        f"in_window_{WINDOW_HOURS}h": cur.execute(SQL_IN_WINDOW).fetchone()[0],
        "repair_rows": cur.execute(SQL_REPAIR_ROWS).fetchone()[0],
        "repair_roots": cur.execute(SQL_REPAIR_ROOTS).fetchone()[0],
    }
    try:
        facts["rows_inside_a_cycle"] = cur.execute(SQL_CYCLE_MEMBERS).fetchone()[0]
    except sqlite3.Error as exc:
        facts["rows_inside_a_cycle"] = f"query failed: {exc}"
    facts["forward_links_strictly_later"] = cur.execute(SQL_FORWARD_STRICT).fetchone()[0]
    con.close()

    for key, value in facts.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
