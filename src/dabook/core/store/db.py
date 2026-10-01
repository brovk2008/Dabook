"""
SQLite connection factory and write-transaction helper (§5.1).

All connections apply the mandatory PRAGMAs:
    journal_mode=WAL, synchronous=NORMAL, busy_timeout=10000, foreign_keys=ON

Write transactions use ``BEGIN IMMEDIATE`` to serialize writers without
starvation, with exponential-backoff retry on OperationalError("locked").

Dashboard connections are read-only (mode=ro URI) and never block workers.
"""

from __future__ import annotations

import random
import sqlite3
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

_MANDATORY_PRAGMAS = [
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA busy_timeout=10000",
    "PRAGMA foreign_keys=ON",
]


def connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    """
    Open a SQLite connection with all mandatory PRAGMAs applied.

    ``readonly=True`` opens the database in read-only mode (mode=ro URI) —
    use this for dashboard/SSE connections so they never block workers.
    """
    mode = "ro" if readonly else "rwc"
    uri = f"file:{path}?mode={mode}"
    con = sqlite3.connect(
        uri,
        uri=True,
        timeout=10,
        isolation_level=None,  # autocommit; we manage transactions explicitly
        check_same_thread=False,
    )
    con.row_factory = sqlite3.Row
    for pragma in _MANDATORY_PRAGMAS:
        if readonly and pragma.startswith("PRAGMA journal_mode"):
            continue  # can't set journal_mode on read-only connection
        con.execute(pragma)
    return con


@contextmanager
def write_txn(
    con: sqlite3.Connection,
    retries: int = 8,
) -> Generator[sqlite3.Connection, None, None]:
    """
    Context manager that wraps a write in ``BEGIN IMMEDIATE … COMMIT``.

    On ``OperationalError("locked")`` retries up to *retries* times with
    exponential backoff + jitter (compatible with ``busy_timeout``, which
    handles Python-level retries as well).

    Usage::

        with write_txn(con) as c:
            c.execute("UPDATE tasks SET state=? WHERE id=?", ...)
    """
    for attempt in range(retries):
        try:
            con.execute("BEGIN IMMEDIATE")
            break
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == retries - 1:
                raise
            sleep = 0.05 * (2**attempt) + random.random() * 0.05
            time.sleep(sleep)

    try:
        yield con
        con.execute("COMMIT")
    except BaseException:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        raise


def apply_schema(con: sqlite3.Connection, sql_path: Path) -> None:
    """Execute the schema SQL file (idempotent: uses CREATE IF NOT EXISTS)."""
    sql = sql_path.read_text(encoding="utf-8")
    con.executescript(sql)


def get_schema_version(con: sqlite3.Connection) -> int:
    try:
        row = con.execute("SELECT v FROM schema_meta WHERE k='schema_version'").fetchone()
        return int(row["v"]) if row else 0
    except sqlite3.OperationalError:
        return 0


def integrity_check(con: sqlite3.Connection) -> bool:
    """Quick integrity check; returns True if OK."""
    rows = con.execute("PRAGMA quick_check").fetchall()
    return len(rows) == 1 and rows[0][0] == "ok"
