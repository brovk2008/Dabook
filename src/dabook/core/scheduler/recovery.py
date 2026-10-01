"""
Lease recovery — runs at supervisor startup and every 30 s to reclaim
tasks whose workers have died or whose leases have expired (§5.3).
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

from dabook.core.store.db import write_txn
from dabook.core.store.tasks import log_event


def pid_alive(pid: int) -> bool:
    """Cross-platform: return True if *pid* is an alive process."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)  # signal 0 = probe
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but not ours — assume alive
    except OSError:
        return False


def recover_leases(con: sqlite3.Connection, now: float | None = None) -> int:
    """
    Scan ``running`` tasks; reset those whose lease expired or whose worker is dead.

    Returns the number of tasks recovered.
    """
    if now is None:
        now = time.time()

    # Fetch candidates; do PID check in Python (can't call os.kill from SQL)
    rows = con.execute(
        """SELECT t.id, t.attempts, t.max_attempts, t.book_id,
                  w.pid, t.lease_expires
             FROM tasks t
             LEFT JOIN workers w ON w.id = t.worker_id
            WHERE t.state = 'running'""",
    ).fetchall()

    recovered = 0
    for r in rows:
        pid = r["pid"]
        lease_ok = r["lease_expires"] is not None and r["lease_expires"] >= now
        worker_alive = pid is not None and pid_alive(int(pid))

        if lease_ok and worker_alive:
            continue  # task is fine

        attempts = r["attempts"]
        max_att = r["max_attempts"]
        new_state = "dead" if attempts >= max_att else "pending"

        reason = "lease_expired" if not lease_ok else "worker_dead"
        with write_txn(con):
            con.execute(
                """UPDATE tasks
                      SET state=?, worker_id=NULL, lease_expires=NULL,
                          error_class='crash', error_msg=?
                    WHERE id=?""",
                (new_state, reason, r["id"]),
            )
        log_event(
            con,
            level="WARNING",
            kind="task_recovered",
            msg=f"Task {r['id']} recovered ({reason}) → {new_state}",
            book_id=r["book_id"],
            task_id=r["id"],
        )
        recovered += 1

    return recovered


def validate_done_artifacts(
    con: sqlite3.Connection,
    workspace: Path,  # type: ignore[name-defined]
    *,
    spot_check_ratio: float = 0.1,
    full_verify: bool = False,
) -> int:
    """
    Spot-check committed artifacts for ``done`` tasks.

    Repairs task state if _SUCCESS is missing or manifest is invalid
    (e.g., the filesystem has the artifact but DB disagrees due to a crash).

    Returns the number of tasks repaired.
    """
    import random
    from pathlib import Path

    from dabook.core.atomic import is_valid_commit, read_manifest
    from dabook.core.store.db import write_txn

    rows = con.execute(
        "SELECT id, output_path, output_sha256 FROM tasks WHERE state='done'"
    ).fetchall()

    repaired = 0
    for r in rows:
        if r["output_path"] is None:
            continue
        if not full_verify and random.random() > spot_check_ratio:
            continue

        op = Path(r["output_path"])
        if not is_valid_commit(op, deep=full_verify):
            # Artifact is gone or corrupt — reset to pending
            with write_txn(con):
                con.execute(
                    "UPDATE tasks SET state='pending', output_path=NULL, output_sha256=NULL WHERE id=?",
                    (r["id"],),
                )
            repaired += 1
        elif r["output_sha256"] is None and op.is_dir():
            # DB missing sha256; fill it from manifest
            try:
                manifest = read_manifest(op)
                sha = manifest.get("committed_at", "")
                with write_txn(con):
                    con.execute(
                        "UPDATE tasks SET output_sha256=? WHERE id=?",
                        (str(sha), r["id"]),
                    )
            except Exception:
                pass

    return repaired
