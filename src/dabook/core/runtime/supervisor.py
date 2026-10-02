"""
Supervisor process — orchestrates workers, runs recovery, serves the dashboard.

The supervisor is the single owner of the workspace lock. It spawns worker
processes (using ``multiprocessing.spawn``), reconciles them against the
desired worker count in the settings table every second, and runs the
telemetry sampler + FastAPI dashboard in threads.
"""

from __future__ import annotations

import logging
import multiprocessing
import signal
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dabook.core.atomic import sweep_partials
from dabook.core.runtime.telemetry import TelemetrySampler
from dabook.core.scheduler.eta import EtaEngine
from dabook.core.scheduler.recovery import recover_leases
from dabook.core.store.db import connect, write_txn
from dabook.core.store.settings import SettingsCache, set_stopping
from dabook.core.store.tasks import log_event, release_task_unspent

log = logging.getLogger("dabook.supervisor")


# ---------------------------------------------------------------------------
# Worker process descriptor
# ---------------------------------------------------------------------------


@dataclass
class ManagedWorker:
    worker_id: str
    resource_class: str
    gpu_index: int | None
    process: multiprocessing.Process
    draining: bool = False
    crash_count: int = 0
    last_crash: float = 0.0


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------


class Supervisor:
    """
    The main supervisor object.

    Usage::

        sup = Supervisor(workspace, db_path)
        sup.start()         # blocks until stopped
    """

    RECOVERY_INTERVAL = 30.0
    RECONCILE_INTERVAL = 1.0
    CRASH_LOOP_WINDOW = 300.0  # 5 min
    CRASH_LOOP_LIMIT = 3

    def __init__(
        self,
        workspace: Path,
        db_path: Path,
        port: int = 8765,
        open_browser: bool = True,
        start_ui: bool = True,
    ) -> None:
        self.workspace = workspace
        self.db_path = db_path
        self.port = port
        self.open_browser = open_browser
        self.start_ui = start_ui

        self._con = connect(db_path)
        self._settings = SettingsCache(self._con)
        self._eta = EtaEngine()
        self._pool: dict[str, list[ManagedWorker]] = {"cpu": [], "gpu": [], "io": [], "llm": []}
        self._death_history: dict[str, list[float]] = {"cpu": [], "gpu": [], "io": [], "llm": []}
        self._spawn_cooldown: dict[str, float] = {"cpu": 0.0, "gpu": 0.0, "io": 0.0, "llm": 0.0}
        self._stop_evt = threading.Event()
        self._telemetry: TelemetrySampler | None = None
        self._next_recovery = time.time()
        self._worker_seq: dict[str, int] = {}

    # ------------------------------------------------------------------
    def start(self) -> None:
        log.info("Supervisor starting (workspace=%s)", self.workspace)

        # Clear any stale stopping flag from previous runs
        set_stopping(self._con, False)
        self._settings.invalidate()

        # Startup recovery
        self._startup_recovery()

        # Telemetry sampler
        self._telemetry = TelemetrySampler(self.db_path, self.workspace)
        self._telemetry.start()

        # Dashboard
        if self.start_ui:
            self._start_dashboard()

        # Signal handlers
        signal.signal(signal.SIGTERM, self._handle_sigterm)
        try:
            signal.signal(signal.SIGINT, self._handle_sigint)
        except (OSError, ValueError):
            pass

        log.info("Supervisor running. Dashboard: http://127.0.0.1:%d", self.port)

        # Main loop
        self._run_loop()

    def _run_loop(self) -> None:
        while not self._stop_evt.is_set():
            try:
                self._settings.invalidate()
                s = self._settings.get()

                # Reconcile worker pool
                self._reconcile(s)

                # Periodic recovery
                if time.time() >= self._next_recovery:
                    n = recover_leases(self._con)
                    if n:
                        log.info("Recovered %d stale tasks", n)
                    self._next_recovery = time.time() + self.RECOVERY_INTERVAL

                # Check if all work is done
                pending = self._con.execute(
                    "SELECT COUNT(*) FROM tasks WHERE state IN ('pending','running')"
                ).fetchone()[0]
                if pending == 0 and not s.stopping:
                    active = self._con.execute(
                        "SELECT COUNT(*) FROM books WHERE state='active'"
                    ).fetchone()[0]
                    if active == 0:
                        log.info("All work done. Supervisor idle.")

                time.sleep(self.RECONCILE_INTERVAL)

            except KeyboardInterrupt:
                break
            except Exception as exc:
                log.exception("Supervisor loop error: %s", exc)
                time.sleep(2)

        self._shutdown()

    def _reconcile(self, s: Any) -> None:
        """Spawn / drain workers to match the desired counts."""
        if s.stopping or self._stop_evt.is_set():
            # If stopping, do not spawn ANY new workers. Drain all existing workers.
            for lane, workers in self._pool.items():
                dead = [w for w in workers if not w.process.is_alive()]
                for w in dead:
                    self._handle_worker_death(w)
                self._pool[lane] = [w for w in workers if w.process.is_alive()]
                for w in self._pool[lane]:
                    if not w.draining:
                        self._mark_draining(w)
            return

        want = {
            "cpu": s.workers_cpu,
            "gpu": s.workers_gpu,
            "io": s.workers_io,
            "llm": s.workers_llm,
        }

        for lane, desired in want.items():
            workers = self._pool[lane]

            # Reap dead processes
            dead = [w for w in workers if not w.process.is_alive()]
            for w in dead:
                self._handle_worker_death(w)
            self._pool[lane] = [w for w in workers if w.process.is_alive()]
            workers = self._pool[lane]

            alive = [w for w in workers if not w.draining]

            if len(alive) < desired:
                # Check crash loop
                for _ in range(desired - len(alive)):
                    if self._crash_loop_breaker(lane):
                        break
                    self._spawn(lane)
                    time.sleep(0.1)  # Stagger process spawns to avoid storm

            elif len(alive) > desired:
                # Mark excess workers as draining (idlest first)
                excess = len(alive) - desired
                for w in sorted(alive, key=lambda w: w.last_crash or 0, reverse=True)[:excess]:
                    self._mark_draining(w)

    def _spawn(self, lane: str, gpu_index: int | None = None) -> None:
        seq = self._worker_seq.get(lane, 0) + 1
        self._worker_seq[lane] = seq
        worker_id = f"worker-{lane}-{seq}"

        from dabook.core.runtime.worker import worker_main

        p = multiprocessing.Process(
            target=worker_main,
            args=(self.db_path, self.workspace, worker_id, lane),
            kwargs={"gpu_index": gpu_index, "log_dir": self.workspace / "logs" / "workers"},
            daemon=True,
            name=worker_id,
        )
        p.start()
        mw = ManagedWorker(
            worker_id=worker_id,
            resource_class=lane,
            gpu_index=gpu_index,
            process=p,
        )
        self._pool[lane].append(mw)
        log.info("Spawned %s (pid=%d)", worker_id, p.pid)

    def _mark_draining(self, w: ManagedWorker) -> None:
        w.draining = True
        with write_txn(self._con):
            self._con.execute("UPDATE workers SET state='draining' WHERE id=?", (w.worker_id,))

    def _handle_worker_death(self, w: ManagedWorker) -> None:
        code = w.process.exitcode
        now = time.time()
        log.warning("Worker %s died (exitcode=%s)", w.worker_id, code)

        self._death_history[w.resource_class].append(now)

        # Try joining and closing dead process to free OS resources
        try:
            w.process.join(timeout=0.1)
            w.process.close()
        except Exception:
            pass

        # Mark worker dead and immediately release any uncompleted task it held
        try:
            with write_txn(self._con):
                self._con.execute("UPDATE workers SET state='dead' WHERE id=?", (w.worker_id,))
                row = self._con.execute(
                    "SELECT id FROM tasks WHERE worker_id=? AND state='running'", (w.worker_id,)
                ).fetchone()
                if row:
                    release_task_unspent(self._con, row["id"])
                    log.info(
                        "Released task %d back to pending from dead worker %s",
                        row["id"],
                        w.worker_id,
                    )
        except Exception as exc:
            log.warning("Error releasing task for dead worker %s: %s", w.worker_id, exc)

        if code == -9:
            log_event(
                self._con,
                "WARNING",
                "worker_oom",
                f"Worker {w.worker_id} killed (exitcode={code})",
                worker_id=w.worker_id,
            )

    def _crash_loop_breaker(self, lane: str) -> bool:
        """Return True if spawning this lane should be suppressed."""
        now = time.time()
        if now < self._spawn_cooldown.get(lane, 0.0):
            return True
        self._death_history[lane] = [
            t for t in self._death_history[lane] if (now - t) < self.CRASH_LOOP_WINDOW
        ]
        if len(self._death_history[lane]) >= self.CRASH_LOOP_LIMIT:
            log.error(
                "Crash loop detected in %s lane (%d exits in %ds) — backing off for 30s",
                lane,
                len(self._death_history[lane]),
                int(self.CRASH_LOOP_WINDOW),
            )
            self._spawn_cooldown[lane] = now + 30.0
            # Clear history so the 30s cooldown is a genuine reset, not a
            # permanent block that re-triggers on every subsequent spawn attempt.
            self._death_history[lane].clear()
            return True
        return False

    def _startup_recovery(self) -> None:
        log.info("Running startup recovery…")
        swept = sweep_partials(self.workspace)
        if swept:
            log.info("Swept %d partial dirs", len(swept))
        n = recover_leases(self._con)
        if n:
            log.info("Recovered %d stale tasks at startup", n)

    def _start_dashboard(self) -> None:
        def _run() -> None:
            import uvicorn

            from dabook.server.app import create_app

            app = create_app(self.db_path, self.workspace, self._telemetry, self._eta)
            config = uvicorn.Config(
                app,
                host="127.0.0.1",
                port=self.port,
                log_level="warning",
                access_log=False,
            )
            server = uvicorn.Server(config)
            # Open browser once server starts
            if self.open_browser:
                threading.Timer(1.5, _open_browser, args=(self.port,)).start()
            server.run()

        t = threading.Thread(target=_run, daemon=True, name="dashboard")
        t.start()

    def _shutdown(self) -> None:
        log.info("Supervisor shutting down…")

        # Signal all workers to stop
        set_stopping(self._con, True)

        # Wait for workers to finish (up to 30 s)
        deadline = time.time() + 30
        while time.time() < deadline:
            alive = [w for lane in self._pool.values() for w in lane if w.process.is_alive()]
            if not alive:
                break
            time.sleep(0.5)

        # Force-kill remaining
        for lane_workers in self._pool.values():
            for w in lane_workers:
                if w.process.is_alive():
                    w.process.terminate()
                    w.process.join(timeout=3)

        if self._telemetry:
            self._telemetry.stop()
            self._telemetry.join(timeout=3)

        self._con.close()
        log.info("Supervisor exited cleanly")

    def _handle_sigterm(self, sig: int, frame: object) -> None:
        log.info("SIGTERM received: draining…")
        set_stopping(self._con, True)
        self._stop_evt.set()

    def _handle_sigint(self, sig: int, frame: object) -> None:
        s = self._settings.get()
        if s.stopping:
            # Second Ctrl+C → hard stop
            log.warning("Hard stop: terminating workers immediately")
            self._stop_evt.set()
        else:
            log.info("Ctrl+C: finishing current tasks then stopping…")
            set_stopping(self._con, True)


def _open_browser(port: int) -> None:
    import webbrowser

    webbrowser.open(f"http://127.0.0.1:{port}")
