"""
Worker process — the main function run in each spawned worker process.

Each worker:
1. Connects to the DB
2. Loads models once (warm for all tasks)
3. Claims tasks in a tight loop
4. Runs the stage function
5. Commits artifacts atomically
6. Heartbeats every ``heartbeat_s`` seconds

Workers are spawned by the supervisor with ``multiprocessing.spawn`` (safe for
CUDA/Torch). They communicate only via the SQLite DB and the filesystem.
"""

from __future__ import annotations

import os
import signal
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dabook.core.scheduler.eta import EtaEngine
from dabook.core.store.db import connect
from dabook.core.store.settings import SettingsCache
from dabook.core.store.tasks import (
    claim_task,
    deregister_worker,
    heartbeat,
    log_event,
    mark_done,
    mark_failed,
    mark_worker_models_loaded,
    register_worker,
    release_task_unspent,
    set_worker_idle,
)

# ---------------------------------------------------------------------------
# Stop flags (set by signal handlers)
# ---------------------------------------------------------------------------


@dataclass
class StopFlags:
    soft: bool = False  # finish current task then exit (Ctrl+C once)
    hard: bool = False  # abort current task at next checkpoint (Ctrl+C 2×)


# ---------------------------------------------------------------------------
# Heartbeat thread
# ---------------------------------------------------------------------------


class HeartbeatThread(threading.Thread):
    """Sends heartbeats to the DB every ``interval`` seconds."""

    def __init__(
        self,
        db_path: Path,
        worker_id: str,
        task_id: int,
        lease_s: float,
        interval: float = 15.0,
    ) -> None:
        super().__init__(daemon=True, name=f"hb-{worker_id}")
        self._db_path = db_path
        self._worker_id = worker_id
        self._task_id = task_id
        self._lease_s = lease_s
        self._interval = interval
        self._stop_evt = threading.Event()
        self._units_done = 0
        self._substep: str | None = None
        self._lock = threading.Lock()

    def progress(self, units_done: int, substep: str | None = None) -> None:
        with self._lock:
            self._units_done = units_done
            self._substep = substep

    def stop(self) -> None:
        self._stop_evt.set()

    def run(self) -> None:
        con = connect(self._db_path)
        try:
            while not self._stop_evt.wait(self._interval):
                with self._lock:
                    ud = self._units_done
                    ss = self._substep
                try:
                    import psutil

                    rss = psutil.Process().memory_info().rss / 1024 / 1024
                except Exception:
                    rss = 0.0
                try:
                    heartbeat(
                        con,
                        self._worker_id,
                        self._task_id,
                        self._lease_s,
                        rss_mb=rss,
                        units_done=ud,
                        substep=ss,
                    )
                except Exception:
                    pass
        finally:
            con.close()


# ---------------------------------------------------------------------------
# Fake stage runners (M1 infrastructure test)
# ---------------------------------------------------------------------------


def _run_fake_stage(
    task: dict[str, Any],
    work_dir: Path,
    progress_fn: Callable[[int, str | None], None],
    should_abort: Callable[[], bool],
    eta: EtaEngine,
) -> dict[str, Any]:
    """
    Fake stage: sleeps proportionally to shard size, emits progress,
    writes a stub output file.  Used in M0/M1 before real stages exist.
    """
    import random

    from dabook.core.atomic import StageCommit, write_json

    stage = task["stage"]
    units = task.get("units_total") or 1
    ms_per_unit = eta.ms_per_unit(stage)
    sleep_total = (ms_per_unit * units / 1000) * random.uniform(0.5, 1.5)
    sleep_total = min(sleep_total, 30.0)  # cap for sanity in tests

    step = max(1, units // 10)
    done = 0
    t0 = time.time()

    while done < units:
        if should_abort():
            raise RuntimeError("Aborted")
        batch = min(step, units - done)
        time.sleep(sleep_total / max(1, units / batch))
        done += batch
        progress_fn(done, f"{stage} page {done}/{units}")

    elapsed_ms = int((time.time() - t0) * 1000)
    eta.record(stage, units, elapsed_ms)

    # Write stub artifact
    meta = {"stage": stage, "task_id": task["id"], "units": units, "impl_version": "0.1.0-fake"}
    stage_dir = work_dir / "stages" / stage
    key = task["stage_key"]

    try:
        with StageCommit(stage_dir, key, meta) as out_dir:
            write_json(out_dir / "output.json", {"status": "ok", **meta})
    except FileExistsError:
        pass  # concurrent worker already committed; fine

    return {
        "output_path": str(stage_dir / key),
        "duration_ms": elapsed_ms,
    }


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


def classify_error(exc: BaseException) -> str:
    msg = str(exc).lower()
    if "cuda out of memory" in msg or "oom" in msg:
        return "oom"
    if "timeout" in msg:
        return "timeout"
    if "no space left" in msg or "enospc" in msg:
        return "disk_full"
    if "pdf" in msg and ("corrupt" in msg or "invalid" in msg):
        return "corrupt_pdf"
    if isinstance(exc, MemoryError):
        return "oom"
    return "bug"


# ---------------------------------------------------------------------------
# Worker main loop
# ---------------------------------------------------------------------------


def worker_main(
    db_path: Path,
    workspace: Path,
    worker_id: str,
    resource_class: str,
    gpu_index: int | None = None,
    log_dir: Path | None = None,
) -> None:
    """Entry point for each spawned worker process."""
    from dabook.core.logging import setup_logging

    log_dir = log_dir or workspace / "logs" / "workers"
    logger = setup_logging(log_dir, worker_id)
    logger.info("Worker starting: %s class=%s gpu=%s", worker_id, resource_class, gpu_index)

    con = connect(db_path)
    settings_cache = SettingsCache(con)
    eta = EtaEngine()

    # Signal handling
    stop = StopFlags()

    def _soft_stop(sig: int, frame: object) -> None:
        logger.info("SIGTERM/SIGINT: finishing current task…")
        stop.soft = True

    def _hard_stop(sig: int, frame: object) -> None:
        logger.warning("Second interrupt: aborting current task")
        stop.hard = True

    signal.signal(signal.SIGTERM, _soft_stop)
    try:
        signal.signal(signal.SIGINT, _soft_stop)
    except (OSError, ValueError):
        pass  # non-main thread on some platforms

    # Register in DB
    register_worker(con, worker_id, os.getpid(), resource_class, gpu_index)

    # Model loading (placeholder; real backends do this in M3)
    logger.info("Models loaded (fake/no-op for M0/M1)")
    mark_worker_models_loaded(con, worker_id)

    hb_thread: HeartbeatThread | None = None

    try:
        while not stop.hard:
            s = settings_cache.get()

            if s.stopping or stop.soft:
                logger.info("Stop flag set; draining")
                break

            if s.queue_paused:
                set_worker_idle(con, worker_id)
                time.sleep(0.5)
                continue

            # Count active books for free_slots calculation
            active_count = con.execute(
                "SELECT COUNT(*) FROM books WHERE state='active'"
            ).fetchone()[0]
            free_slots = s.free_book_slots(active_count)

            task = claim_task(
                con,
                worker_id,
                resource_class,
                now=time.time(),
                lease_s=s.lease_s,
                free_slots=free_slots,
            )

            if task is None:
                set_worker_idle(con, worker_id)
                time.sleep(0.5)
                continue

            book_id = task["book_id"]
            logger.info(
                "Claimed task %d: %s shard=%d", task["id"], task["stage"], task["shard_idx"]
            )

            # Start heartbeat thread
            hb_thread = HeartbeatThread(
                db_path,
                worker_id,
                task["id"],
                lease_s=s.lease_s,
                interval=s.heartbeat_s,
            )
            hb_thread.start()

            t0 = time.time()
            try:
                book_work_dir = workspace / "books" / task["stage_key"][:12]
                pdf_path = None
                # Find actual book dir and source path
                book_rows = con.execute(
                    "SELECT sha256, path FROM books WHERE id=?", (book_id,)
                ).fetchone()
                if book_rows:
                    book_work_dir = workspace / "books" / book_rows["sha256"][:12]
                    pdf_candidate = Path(book_rows["path"])
                    if pdf_candidate.is_file():
                        pdf_path = pdf_candidate

                if pdf_path is not None:
                    from dabook.stages.runner import run_stage

                    result = run_stage(
                        task,
                        work_dir=book_work_dir,
                        pdf_path=pdf_path,
                        progress_fn=hb_thread.progress,
                        should_abort=lambda: stop.hard,
                    )
                else:
                    result = _run_fake_stage(
                        task,
                        work_dir=book_work_dir,
                        progress_fn=hb_thread.progress,
                        should_abort=lambda: stop.hard,
                        eta=eta,
                    )

                duration_ms = int((time.time() - t0) * 1000)
                mark_done(
                    con,
                    task["id"],
                    output_path=result.get("output_path"),
                    duration_ms=result.get("duration_ms", duration_ms),
                )

                # Record stage stats for ETA model
                units = task.get("units_total") or 1
                con.execute(
                    "INSERT INTO stage_stats (stage, units, ms, ts) VALUES (?,?,?,?)",
                    (task["stage"], units, duration_ms, time.time()),
                )

                logger.info(
                    "Task %d done in %.1fs: %s",
                    task["id"],
                    (time.time() - t0),
                    task["stage"],
                )
                log_event(
                    con,
                    "INFO",
                    "task_done",
                    f"Stage {task['stage']} shard {task['shard_idx']} done",
                    book_id=book_id,
                    task_id=task["id"],
                    worker_id=worker_id,
                )

            except (KeyboardInterrupt, SystemExit):
                # Graceful: don't burn the attempt
                release_task_unspent(con, task["id"])
                stop.soft = True
                logger.info("Task %d released (graceful stop)", task["id"])

            except Exception as exc:
                err_class = classify_error(exc)
                err_msg = traceback.format_exc()[-2000:]
                logger.error("Task %d failed (%s): %s", task["id"], err_class, exc)
                new_state = mark_failed(
                    con,
                    task["id"],
                    err_class,
                    err_msg,
                    max_attempts=s.max_attempts,
                    backoff_s=s.backoff_s,
                )
                log_event(
                    con,
                    "ERROR",
                    f"task_{new_state}",
                    f"{err_class}: {exc}",
                    book_id=book_id,
                    task_id=task["id"],
                    worker_id=worker_id,
                    data={"error_class": err_class},
                )

            finally:
                if hb_thread is not None:
                    hb_thread.stop()
                    hb_thread.join(timeout=2.0)
                    hb_thread = None

    except Exception as exc:
        logger.exception("Worker crashed: %s", exc)
    finally:
        deregister_worker(con, worker_id)
        con.close()
        logger.info("Worker exiting: %s", worker_id)
