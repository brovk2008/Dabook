"""
Database migration system (forward-only, versioned).

Each migration is a Python function that receives a connection and applies
the necessary schema changes.  The baseline migration uses executescript()
(which auto-commits), so it must run outside any explicit transaction.
The current version is stored in ``schema_meta(k='schema_version')``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# ---------------------------------------------------------------------------
# Migration registry
# ---------------------------------------------------------------------------


def _m001_baseline(con: sqlite3.Connection) -> None:
    """
    Apply the baseline schema from schema.sql.

    executescript() commits any pending transaction, then runs the SQL,
    so this function must be called outside a BEGIN block.
    """
    sql_path = Path(__file__).parent / "schema.sql"
    con.executescript(sql_path.read_text(encoding="utf-8"))


# Register migrations in order: (version, fn_that_runs_outside_txn, fn_that_runs_inside_txn)
# For the baseline we run outside a transaction; later deltas can run inside.
MIGRATIONS: list[tuple[int, callable, bool]] = [  # type: ignore[type-arg]
    (1, _m001_baseline, False),  # False = run outside explicit txn
]


# ---------------------------------------------------------------------------
# Migration runner
# ---------------------------------------------------------------------------


def migrate(db_path: Path) -> int:
    """
    Open the database, detect current version, apply pending migrations.

    Returns the new schema version.
    """
    con = sqlite3.connect(str(db_path), isolation_level=None)
    # These PRAGMAs are safe outside a transaction
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=OFF")  # OFF during schema creation to avoid FK order issues
    con.row_factory = sqlite3.Row

    # Detect current version (table may not exist yet)
    try:
        row = con.execute("SELECT v FROM schema_meta WHERE k='schema_version'").fetchone()
        current = int(row["v"]) if row else 0
    except sqlite3.OperationalError:
        current = 0

    applied = 0
    for version, fn, inside_txn in MIGRATIONS:
        if version <= current:
            continue

        if inside_txn:
            con.execute("BEGIN IMMEDIATE")
            try:
                fn(con)
                con.execute(
                    "INSERT OR REPLACE INTO schema_meta (k, v) VALUES ('schema_version', ?)",
                    (str(version),),
                )
                con.execute("COMMIT")
            except Exception:
                try:
                    con.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                con.close()
                raise
        else:
            # Runs outside a transaction (executescript handles its own commits)
            fn(con)
            # Verify the version row was inserted by the script; if not, add it
            try:
                row = con.execute("SELECT v FROM schema_meta WHERE k='schema_version'").fetchone()
                if row is None or int(row["v"]) < version:
                    con.execute("BEGIN IMMEDIATE")
                    con.execute(
                        "INSERT OR REPLACE INTO schema_meta (k, v) VALUES ('schema_version', ?)",
                        (str(version),),
                    )
                    con.execute("COMMIT")
            except Exception:
                pass

        applied += 1

    # Re-enable FK enforcement for normal operation
    con.execute("PRAGMA foreign_keys=ON")
    con.close()
    return current + applied
