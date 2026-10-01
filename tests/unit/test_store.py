"""
Unit tests for the SQLite store: connections, write transactions, task lifecycle.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from dabook.core.store.db import connect, integrity_check, write_txn
from dabook.core.store.migrations import migrate
from dabook.core.store.settings import load_settings, update_setting
from dabook.core.store.tasks import (
    claim_task,
    heartbeat,
    log_event,
    mark_done,
    mark_failed,
    register_book,
    register_worker,
    release_task_unspent,
)


@pytest.fixture
def db(tmp_path: Path):
    """Freshly migrated DB for each test."""
    db_path = tmp_path / "state.db"
    migrate(db_path)
    con = connect(db_path)
    yield con, db_path
    con.close()


def test_migrate_creates_tables(db):
    con, _ = db
    tables = {
        r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert {"books", "tasks", "workers", "settings", "events", "samples"}.issubset(tables)


def test_integrity_check_passes(db):
    con, _ = db
    assert integrity_check(con)


def test_register_book_idempotent(db):
    con, _ = db
    sha = "a" * 64
    id1 = register_book(con, sha, "/tmp/book.pdf", 1234)
    id2 = register_book(con, sha, "/tmp/book.pdf", 1234)
    assert id1 == id2
    count = con.execute("SELECT COUNT(*) FROM books WHERE sha256=?", (sha,)).fetchone()[0]
    assert count == 1


def test_claim_returns_none_when_empty(db):
    con, _ = db
    task = claim_task(con, "w1", "cpu", time.time(), 90, free_slots=2)
    assert task is None


def _insert_test_task(con, book_id: int, stage_key: str = "k001", state: str = "pending") -> int:
    with write_txn(con):
        cur = con.execute(
            """INSERT INTO tasks
               (book_id, stage, stage_order, shard_idx, stage_key,
                resource_class, state, max_attempts, units_total)
               VALUES (?, 's02_extract', 2, 0, ?, 'cpu', ?, 3, 10)
               RETURNING id""",
            (book_id, stage_key, state),
        )
        return int(cur.fetchone()["id"])


def test_claim_and_heartbeat(db):
    con, _ = db
    book_id = register_book(con, "b" * 64, "/tmp/b.pdf", 1000)
    # Activate the book
    with write_txn(con):
        con.execute("UPDATE books SET state='active' WHERE id=?", (book_id,))
    task_id = _insert_test_task(con, book_id)

    register_worker(con, "w1", 999, "cpu")

    task = claim_task(con, "w1", "cpu", time.time(), lease_s=90, free_slots=1)
    assert task is not None
    assert task["id"] == task_id
    assert task["attempts"] == 1

    # Heartbeat extends lease
    heartbeat(con, "w1", task_id, lease_s=90, rss_mb=100, units_done=5)
    row = con.execute(
        "SELECT lease_expires, units_done FROM tasks WHERE id=?", (task_id,)
    ).fetchone()
    assert row["units_done"] == 5
    assert row["lease_expires"] > time.time()


def test_claim_is_exclusive(db):
    """Two claim calls must not return the same task."""
    con, _ = db
    book_id = register_book(con, "c" * 64, "/tmp/c.pdf", 500)
    with write_txn(con):
        con.execute("UPDATE books SET state='active' WHERE id=?", (book_id,))
    _insert_test_task(con, book_id, "k001")
    register_worker(con, "w1", 1, "cpu")
    register_worker(con, "w2", 2, "cpu")

    t1 = claim_task(con, "w1", "cpu", time.time(), 90, free_slots=2)
    t2 = claim_task(con, "w2", "cpu", time.time(), 90, free_slots=2)
    assert t1 is not None
    assert t2 is None  # only one task available


def test_mark_done(db):
    con, _ = db
    book_id = register_book(con, "d" * 64, "/tmp/d.pdf", 100)
    with write_txn(con):
        con.execute("UPDATE books SET state='active' WHERE id=?", (book_id,))
    task_id = _insert_test_task(con, book_id)
    register_worker(con, "w1", 1, "cpu")
    claim_task(con, "w1", "cpu", time.time(), 90, free_slots=1)

    mark_done(con, task_id, output_path="/tmp/out", duration_ms=500)
    row = con.execute("SELECT state, duration_ms FROM tasks WHERE id=?", (task_id,)).fetchone()
    assert row["state"] == "done"
    assert row["duration_ms"] == 500


def test_mark_failed_retries(db):
    con, _ = db
    book_id = register_book(con, "e" * 64, "/tmp/e.pdf", 100)
    with write_txn(con):
        con.execute("UPDATE books SET state='active' WHERE id=?", (book_id,))
    task_id = _insert_test_task(con, book_id)
    register_worker(con, "w1", 1, "cpu")

    max_attempts = 3
    for attempt in range(max_attempts):
        # Reset not_before so the task is immediately claimable
        with write_txn(con):
            con.execute("UPDATE tasks SET not_before=0 WHERE id=?", (task_id,))
        task = claim_task(con, "w1", "cpu", time.time(), 90, free_slots=1)
        assert task is not None, f"Should be claimable on attempt {attempt + 1}"
        state = mark_failed(
            con, task["id"], "bug", f"error {attempt}", max_attempts=max_attempts, backoff_s=0.0
        )
        if attempt < max_attempts - 1:
            assert state == "pending", f"Attempt {attempt + 1}: expected pending, got {state}"
        else:
            assert state == "dead", f"Attempt {attempt + 1}: expected dead, got {state}"

    # Dead task should not be claimable even after resetting not_before
    with write_txn(con):
        con.execute("UPDATE tasks SET not_before=0 WHERE id=?", (task_id,))
    t = claim_task(con, "w1", "cpu", time.time(), 90, free_slots=1)
    assert t is None  # dead tasks are not claimable


def test_release_task_unspent(db):
    con, _ = db
    book_id = register_book(con, "f" * 64, "/tmp/f.pdf", 100)
    with write_txn(con):
        con.execute("UPDATE books SET state='active' WHERE id=?", (book_id,))
    task_id = _insert_test_task(con, book_id)
    register_worker(con, "w1", 1, "cpu")
    task = claim_task(con, "w1", "cpu", time.time(), 90, free_slots=1)
    assert task["attempts"] == 1

    release_task_unspent(con, task_id)
    row = con.execute("SELECT state, attempts FROM tasks WHERE id=?", (task_id,)).fetchone()
    assert row["state"] == "pending"
    assert row["attempts"] == 0  # refunded


def test_settings_load_defaults(db):
    con, _ = db
    s = load_settings(con)
    assert s.workers_cpu >= 1
    assert s.lease_s >= 30
    assert 0 < s.quality_verified <= 1


def test_settings_update_live(db):
    con, _ = db
    update_setting(con, "workers.cpu", 12)
    s = load_settings(con)
    assert s.workers_cpu == 12


def test_log_event(db):
    con, _ = db
    log_event(con, "INFO", "test_event", "hello world")
    row = con.execute("SELECT kind, msg FROM events ORDER BY id DESC LIMIT 1").fetchone()
    assert row["kind"] == "test_event"
    assert row["msg"] == "hello world"


def test_no_database_locked_on_concurrent_writes(db):
    """Multiple write_txn calls on the same connection must not lock."""
    con, _ = db
    book_id = register_book(con, "g" * 64, "/tmp/g.pdf", 100)
    # Repeated rapid writes
    for i in range(20):
        with write_txn(con):
            con.execute("UPDATE books SET priority=? WHERE id=?", (i, book_id))
    row = con.execute("SELECT priority FROM books WHERE id=?", (book_id,)).fetchone()
    assert row["priority"] == 19
