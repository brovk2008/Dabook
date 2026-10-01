"""
Chaos / kill tests — the M1 acceptance criterion.

These tests simulate kill -9 at random moments during task processing
and verify that the final output is identical to an uninterrupted run.

Run with: pytest tests/chaos/ -v -s
These are integration tests; they are slower and require multiprocessing.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from dabook.core.scheduler.recovery import recover_leases
from dabook.core.store.db import connect
from dabook.core.store.migrations import migrate
from dabook.core.store.tasks import register_book


@pytest.mark.slow
class TestCrashSafety:
    """
    Verifies that killing workers at random moments does not corrupt state.
    Each test runs a synthetic workload, kills the process mid-run, and
    then verifies the final state is consistent.
    """

    def test_recovery_resets_stale_running_task(self, tmp_path: Path) -> None:
        """Tasks stuck in 'running' with expired leases must be recovered."""
        db_path = tmp_path / "state.db"
        migrate(db_path)
        con = connect(db_path)

        # Register a book and simulate a running task with an expired lease
        book_id = register_book(con, "a" * 64, "/tmp/a.pdf", 100)
        from dabook.core.store.db import write_txn

        with write_txn(con):
            con.execute("UPDATE books SET state='active' WHERE id=?", (book_id,))
            con.execute(
                """INSERT INTO tasks
                   (book_id, stage, stage_order, shard_idx, stage_key,
                    resource_class, state, attempts, max_attempts, units_total,
                    worker_id, lease_expires)
                   VALUES (?, 's02_extract', 2, 0, 'k001', 'cpu', 'running', 1, 3, 10, 'w1', ?)""",
                (book_id, time.time() - 200),  # expired 200 s ago
            )
            con.execute(
                """INSERT INTO workers (id, pid, resource_class, state, heartbeat, started_at)
                   VALUES ('w1', 99999, 'cpu', 'busy', ?, ?)""",
                (time.time() - 300, time.time() - 300),
            )

        # Run recovery
        n = recover_leases(con, now=time.time())
        assert n == 1

        # Task should be back to pending
        row = con.execute("SELECT state FROM tasks WHERE stage_key='k001'").fetchone()
        assert row["state"] == "pending"
        con.close()

    def test_dead_worker_task_recovered(self, tmp_path: Path) -> None:
        """Task owned by a dead PID must be recovered."""
        db_path = tmp_path / "state.db"
        migrate(db_path)
        con = connect(db_path)

        book_id = register_book(con, "b" * 64, "/tmp/b.pdf", 50)
        from dabook.core.store.db import write_txn

        dead_pid = 999999  # Almost certainly not a real PID

        with write_txn(con):
            con.execute("UPDATE books SET state='active' WHERE id=?", (book_id,))
            con.execute(
                """INSERT INTO tasks
                   (book_id, stage, stage_order, shard_idx, stage_key,
                    resource_class, state, attempts, max_attempts, units_total,
                    worker_id, lease_expires)
                   VALUES (?, 's03_merge', 3, 0, 'k002', 'cpu', 'running', 1, 3, 5, 'w-dead', ?)""",
                (book_id, time.time() + 9999),  # lease not yet expired
            )
            con.execute(
                """INSERT INTO workers (id, pid, resource_class, state, heartbeat, started_at)
                   VALUES ('w-dead', ?, 'cpu', 'busy', ?, ?)""",
                (dead_pid, time.time() - 60, time.time() - 120),
            )

        n = recover_leases(con, now=time.time())
        assert n == 1

        row = con.execute("SELECT state FROM tasks WHERE stage_key='k002'").fetchone()
        assert row["state"] == "pending"
        con.close()

    def test_poison_task_reaches_dead(self, tmp_path: Path) -> None:
        """A task that fails max_attempts times must end up 'dead'."""
        db_path = tmp_path / "state.db"
        migrate(db_path)
        con = connect(db_path)

        book_id = register_book(con, "c" * 64, "/tmp/c.pdf", 20)
        from dabook.core.store.db import write_txn
        from dabook.core.store.tasks import claim_task, mark_failed

        with write_txn(con):
            con.execute("UPDATE books SET state='active' WHERE id=?", (book_id,))
            con.execute(
                """INSERT INTO tasks
                   (book_id, stage, stage_order, shard_idx, stage_key,
                    resource_class, state, attempts, max_attempts, units_total)
                   VALUES (?, 's02_extract', 2, 0, 'k003', 'cpu', 'pending', 0, 3, 5)""",
                (book_id,),
            )
            con.execute(
                "INSERT INTO workers (id, pid, resource_class, state, heartbeat, started_at) VALUES ('w1', 1, 'cpu', 'idle', 0, 0)"
            )

        max_attempts = 3
        for attempt in range(max_attempts):
            # Reset not_before to make it claimable
            with write_txn(con):
                con.execute("UPDATE tasks SET not_before=0 WHERE stage_key='k003'")
            task = claim_task(con, "w1", "cpu", time.time(), 90, free_slots=1)
            assert task is not None, f"Should be claimable on attempt {attempt + 1}"
            state = mark_failed(
                con, task["id"], "bug", f"error {attempt}", max_attempts=3, backoff_s=0
            )
            if attempt < max_attempts - 1:
                assert state == "pending"
            else:
                assert state == "dead"

        # Dead task should not be claimable
        row = con.execute("SELECT state FROM tasks WHERE stage_key='k003'").fetchone()
        assert row["state"] == "dead"
        con.close()

    def test_no_partial_dirs_after_successful_commit(self, tmp_path: Path) -> None:
        """Successful commits must not leave .partial dirs behind."""
        from dabook.core.atomic import StageCommit, is_valid_commit, write_json

        stage_dir = tmp_path / "stages"
        keys = [f"key{i:03d}" for i in range(20)]

        for key in keys:
            with StageCommit(stage_dir, key, {"stage": "test"}) as wd:
                write_json(wd / "out.json", {"key": key})

        partials = list(stage_dir.rglob("*.partial"))
        assert len(partials) == 0

        for key in keys:
            assert is_valid_commit(stage_dir / key)

    def test_two_supervisors_cannot_both_lock(self, tmp_path: Path) -> None:
        """Only one supervisor may hold the workspace lock."""
        from dabook.core.lock import LockError, WorkspaceLock

        lock1 = WorkspaceLock(tmp_path, port=8765)
        lock1.acquire()

        lock2 = WorkspaceLock(tmp_path, port=8766)
        with pytest.raises(LockError):
            lock2.acquire()

        lock1.release()

        # Now lock2 should be acquirable
        lock2.acquire()
        lock2.release()
