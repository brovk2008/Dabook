"""
Single-instance workspace lock.

Prevents two supervisors from running simultaneously in the same workspace.
Uses ``portalocker`` for cross-platform file locking (Windows + POSIX).
"""

from __future__ import annotations

import os
from pathlib import Path

import portalocker


class WorkspaceLock:
    """
    Acquire and hold a workspace-level lock for the lifetime of the supervisor.

    Usage::

        lock = WorkspaceLock(workspace)
        lock.acquire()          # raises LockError if already locked
        ...
        lock.release()          # or use as context manager

    The lock file stores the current PID and port so the error message can tell
    the user where the existing instance is running.
    """

    def __init__(self, workspace: Path, port: int = 8765) -> None:
        self._lock_path = workspace / ".lock"
        self._pid_path = workspace / ".lock.pid"
        self._port = port
        self._fh: portalocker.Lock | None = None

    # ------------------------------------------------------------------
    def acquire(self) -> None:
        """Acquire the lock.  Raises ``LockError`` if another process holds it."""
        workspace = self._lock_path.parent
        workspace.mkdir(parents=True, exist_ok=True)

        lock = portalocker.Lock(
            str(self._lock_path),
            mode="w",
            flags=portalocker.LOCK_EX | portalocker.LOCK_NB,
            timeout=0,
            fail_when_locked=True,
        )
        try:
            lock.acquire()
        except portalocker.LockException:
            # Try to give a helpful error message
            existing = self._read_pid_file()
            if existing:
                pid, port = existing
                raise LockError(
                    f"DABOOK is already running (PID {pid}, dashboard at "
                    f"http://127.0.0.1:{port}). "
                    f"Open that tab or run `dabook stop` first."
                ) from None
            raise LockError(
                "Workspace is locked by another process. "
                "Run `dabook stop` or delete the `.lock` file."
            ) from None

        self._fh = lock
        self._write_pid_file()

    def release(self) -> None:
        if self._fh is not None:
            try:
                self._fh.release()
            except Exception:
                pass
            self._fh = None
        try:
            self._pid_path.unlink(missing_ok=True)
            self._lock_path.unlink(missing_ok=True)
        except Exception:
            pass

    # ------------------------------------------------------------------
    def __enter__(self) -> WorkspaceLock:
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()

    # ------------------------------------------------------------------
    def _write_pid_file(self) -> None:
        self._pid_path.write_text(f"{os.getpid()}\n{self._port}\n")

    def _read_pid_file(self) -> tuple[int, int] | None:
        try:
            lines = self._pid_path.read_text().strip().splitlines()
            return int(lines[0]), int(lines[1])
        except Exception:
            return None


class LockError(RuntimeError):
    pass
