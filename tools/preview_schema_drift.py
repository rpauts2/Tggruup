"""What the next boot would write to a database, computed without writing it.

Restarting the farm onto newer code silently runs `apply_additive_migrations`
first, so the operator is asked to accept a schema change they cannot see. This
tool shows it: it opens the database through a read-only sqlite connection, so a
write is not merely unplanned, it fails at the driver level.

    python tools/preview_schema_drift.py [sqlite:///path.db]

Default target is the configured `DATABASE_URL`. Exit 0 when the preview ran, 2
when the target is unusable.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, inspect  # noqa: E402

from tgfarm.core.settings import get_settings  # noqa: E402
from tgfarm.db.bootstrap import Base, plan_additive_migrations  # noqa: E402


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else get_settings().database_url
    if not url.startswith("sqlite:///"):
        print(f"только sqlite умеет показывать план: {url}")
        return 2
    path = url.replace("sqlite:///", "", 1)
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        print(f"не открылось для чтения: {path} ({exc})")
        return 2

    engine = create_engine("sqlite://", creator=lambda: con)
    try:
        statements = plan_additive_migrations(engine)
        live_tables = set(inspect(engine).get_table_names())
        new_tables = sorted(set(Base.metadata.tables) - live_tables)
    except sqlite3.Error as exc:
        print(f"план не снялся: {exc}")
        return 2
    finally:
        con.close()

    print(f"db: {path}")
    print(f"statements: {len(statements)}")
    for statement in statements:
        print(f"  {statement}")
    for table in new_tables:
        print(f"  (новая таблица, её создаст create_all: {table})")
    if not statements and not new_tables:
        print("  схема совпадает с моделями: рестарт ничего не допишет")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
