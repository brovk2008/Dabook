"""
Task store — atomic claim, heartbeat, progress, completion, and failure operations.

All write operations use ``BEGIN IMMEDIATE`` transactions (§5.1).
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from dabook.core.store.db import write_txn

# ---------------------------------------------------------------------------
# Task claim (§5.3, Appendix B.3)
# ---------------------------------------------------------------------------

_CLAIM_SQL = """
UPDATE tasks
   SET state='running',
       worker_id=:w,
       attempts=attempts+1,
       lease_expires=:now+:lease,
       started_at=COALESCE(started_at, :now),
       error_class=NULL,
       error_msg=NULL,
       substep=NULL
 WHERE id = (
   SELECT t.id
     FROM tasks t
     JOIN books b ON b.id = t.book_id
    WHERE t.state = 'pending'
      AND t.resource_class = :rc
      AND t.not_before <= :now
      AND b.paused = 0
      AND b.state IN ('active', 'queued')
      -- only books that are active or can become active (within book_concurrency)
      AND b.id IN (
            SELECT id FROM books WHERE state = 'active'
            UNION ALL
            SELECT id FROM (
                SELECT id FROM books
                 WHERE state = 'queued'
                 ORDER BY priority DESC, added_at
                 LIMIT :free_slots
            )
          )
      -- all dependencies must be done/skipped
      AND NOT EXISTS (
            SELECT 1
              FROM task_deps d
              JOIN tasks p ON p.id = d.depends_on
             WHERE d.task_id = t.id
               AND p.state NOT IN ('done', 'skipped')
          )
    ORDER BY b.priority DESC, b.added_at, t.stage_order, t.shard_idx
    LIMIT 1
 )
RETURNING id, book_id, stage, shard_idx, page_start, page_end, stage_key, attempts,
          units_total, resource_class;
"""


def claim_task(
    con: sqlite3.Connection,
    worker_id: str,
    resource_class: str,
    now: float,
    lease_s: float,
    free_slots: int,
) -> dict[str, Any] | None:
    """
    Atomically claim one pending task for *worker_id*.

    Returns the claimed task row as a dict, or None if nothing is available.
    Also transitions the book from ``queued → active`` if first task claimed.
    """
    with write_txn(con):
        row = con.execute(
            _CLAIM_SQL,
            dict(w=worker_id, rc=resource_class, now=now, lease=lease_s, free_slots=free_slots),
        ).fetchone()
        if row is None:
            return None
        row = dict(row)
        # Activate the book if it was still queued
        con.execute(
            """UPDATE books
                  SET state='active', started_at=COALESCE(started_at, :n)
                WHERE id=:b AND state='queued'""",
            dict(n=now, b=row["book_id"]),
        )
        return row


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


def heartbeat(
    con: sqlite3.Connection,
    worker_id: str,
    task_id: int | None,
    lease_s: float,
    rss_mb: float = 0.0,
    units_done: int = 0,
    substep: str | None = None,
) -> None:
    """Extend task lease and update worker telemetry."""
    now = time.time()
    with write_txn(con):
        con.execute(
            """UPDATE workers
                  SET heartbeat=:now, rss_mb=:rss, state='busy', current_task=:tid
                WHERE id=:wid""",
            dict(now=now, rss=rss_mb, tid=task_id, wid=worker_id),
        )
        if task_id is not None:
            con.execute(
                """UPDATE tasks
                      SET lease_expires=:exp, units_done=:ud, substep=:ss
                    WHERE id=:tid""",
                dict(exp=now + lease_s, ud=units_done, ss=substep, tid=task_id),
            )


def set_worker_idle(con: sqlite3.Connection, worker_id: str) -> None:
    now = time.time()
    with write_txn(con):
        con.execute(
            "UPDATE workers SET state='idle', heartbeat=:now, current_task=NULL WHERE id=:wid",
            dict(now=now, wid=worker_id),
        )


# ---------------------------------------------------------------------------
# Task completion
# ---------------------------------------------------------------------------


def mark_done(
    con: sqlite3.Connection,
    task_id: int,
    output_path: str | None = None,
    output_sha256: str | None = None,
    duration_ms: int | None = None,
    skipped: bool = False,
) -> None:
    now = time.time()
    state = "skipped" if skipped else "done"
    with write_txn(con):
        con.execute(
            """UPDATE tasks
                  SET state=:s, finished_at=:now, duration_ms=:dur,
                      output_path=:op, output_sha256=:os2, substep=NULL
                WHERE id=:tid""",
            dict(s=state, now=now, dur=duration_ms, op=output_path, os2=output_sha256, tid=task_id),
        )


def mark_failed(
    con: sqlite3.Connection,
    task_id: int,
    error_class: str,
    error_msg: str,
    max_attempts: int,
    backoff_s: float,
) -> str:
    """
    Fail a task.  If attempts < max_attempts, reschedule with exponential backoff.
    Returns new state: ``'pending'``, ``'dead'``, or ``'failed'``.
    """
    now = time.time()
    row = con.execute("SELECT attempts, max_attempts FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        return "failed"

    attempts = row["attempts"]
    effective_max = max(max_attempts, row["max_attempts"])

    if attempts >= effective_max:
        new_state = "dead"
        not_before = now
    else:
        new_state = "pending"
        jitter = (time.time() % 1.0) * backoff_s
        not_before = now + backoff_s * (2 ** (attempts - 1)) + jitter

    with write_txn(con):
        con.execute(
            """UPDATE tasks
                  SET state=:s, error_class=:ec, error_msg=:em,
                      not_before=:nb, worker_id=NULL, lease_expires=NULL, finished_at=:now
                WHERE id=:tid""",
            dict(
                s=new_state,
                ec=error_class,
                em=error_msg[:2000],
                nb=not_before,
                now=now,
                tid=task_id,
            ),
        )
    return new_state


def release_task_unspent(con: sqlite3.Connection, task_id: int) -> None:
    """
    Release a task back to ``pending`` without burning an attempt (graceful abort).
    Used when a worker receives a stop signal mid-task.
    """
    with write_txn(con):
        con.execute(
            """UPDATE tasks
                  SET state='pending', attempts=MAX(0, attempts-1),
                      worker_id=NULL, lease_expires=NULL, substep=NULL
                WHERE id=?""",
            (task_id,),
        )


# ---------------------------------------------------------------------------
# Worker registration / deregistration
# ---------------------------------------------------------------------------


def register_worker(
    con: sqlite3.Connection,
    worker_id: str,
    pid: int,
    resource_class: str,
    gpu_index: int | None = None,
) -> None:
    now = time.time()
    with write_txn(con):
        con.execute(
            """INSERT OR REPLACE INTO workers
               (id, pid, resource_class, state, heartbeat, started_at, gpu_index)
               VALUES (?, ?, ?, 'starting', ?, ?, ?)""",
            (worker_id, pid, resource_class, now, now, gpu_index),
        )


def mark_worker_models_loaded(con: sqlite3.Connection, worker_id: str) -> None:
    with write_txn(con):
        con.execute("UPDATE workers SET models_loaded=1, state='idle' WHERE id=?", (worker_id,))


def deregister_worker(con: sqlite3.Connection, worker_id: str) -> None:
    with write_txn(con):
        con.execute("UPDATE workers SET state='dead' WHERE id=?", (worker_id,))


# ---------------------------------------------------------------------------
# Book registration
# ---------------------------------------------------------------------------


def register_book(
    con: sqlite3.Connection,
    sha256: str,
    path: str,
    size_bytes: int,
    priority: int = 0,
    profile: str | None = None,
) -> int:
    """Insert or retrieve a book.  Returns the book id."""
    now = time.time()
    with write_txn(con):
        existing = con.execute("SELECT id FROM books WHERE sha256=?", (sha256,)).fetchone()
        if existing:
            return int(existing["id"])
        cur = con.execute(
            """INSERT INTO books (sha256, path, size_bytes, priority, profile, added_at)
               VALUES (?, ?, ?, ?, ?, ?) RETURNING id""",
            (sha256, path, size_bytes, priority, profile, now),
        )
        return int(cur.fetchone()["id"])


# ---------------------------------------------------------------------------
# Event logging
# ---------------------------------------------------------------------------


def log_event(
    con: sqlite3.Connection,
    level: str,
    kind: str,
    msg: str,
    book_id: int | None = None,
    task_id: int | None = None,
    worker_id: str | None = None,
    data: Any | None = None,
) -> None:
    import json

    now = time.time()
    with write_txn(con):
        con.execute(
            """INSERT INTO events (ts, level, book_id, task_id, worker_id, kind, msg, data)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                now,
                level,
                book_id,
                task_id,
                worker_id,
                kind,
                msg,
                json.dumps(data) if data else None,
            ),
        )
