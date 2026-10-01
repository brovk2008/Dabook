"""
Telemetry sampler — samples system resources at 1 Hz and stores
downsampled rows to the ``samples`` table (§7.3).

Tiers:
  - Tier 0 (raw, ~5 s): kept in memory for live dashboard
  - Tier 1 (60 s buckets): written to DB for the last hour
  - Tier 2 (300 s buckets): written to DB for longer history
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil

from dabook.core.store.db import write_txn

try:
    import pynvml  # type: ignore[import-untyped]

    pynvml.nvmlInit()
    _NVML_OK = True
except Exception:
    _NVML_OK = False


@dataclass
class SystemSnapshot:
    ts: float
    cpu_pct: float  # overall CPU %
    cpu_per_core: list[float]  # per-core %
    ram_used_mb: float
    ram_total_mb: float
    disk_free_gb: float
    gpus: list[dict[str, Any]] = field(default_factory=list)
    workers_busy: int = 0
    pages_done: int = 0
    tasks_pending: int = 0


class TelemetrySampler(threading.Thread):
    """Background thread that samples system telemetry and writes to DB."""

    def __init__(
        self,
        db_path: Path,  # type: ignore[name-defined]
        workspace: Path,  # type: ignore[name-defined]
        interval: float = 1.0,
    ) -> None:
        super().__init__(daemon=True, name="telemetry")
        self._db_path = db_path
        self._workspace = workspace
        self._interval = interval
        self._stop_evt = threading.Event()
        self._latest: SystemSnapshot | None = None
        self._lock = threading.Lock()
        # Tier bucket accumulators
        self._tier1_buf: list[SystemSnapshot] = []
        self._tier2_buf: list[SystemSnapshot] = []
        self._last_tier1 = time.time()
        self._last_tier2 = time.time()

    def stop(self) -> None:
        self._stop_evt.set()

    def latest(self) -> SystemSnapshot | None:
        with self._lock:
            return self._latest

    def run(self) -> None:
        from dabook.core.store.db import connect

        con = connect(self._db_path)
        try:
            while not self._stop_evt.wait(self._interval):
                snap = self._sample(con)
                with self._lock:
                    self._latest = snap
                self._maybe_flush(con, snap)
        finally:
            con.close()

    def _sample(self, con: Any) -> SystemSnapshot:
        now = time.time()
        cpu_pct = psutil.cpu_percent(interval=None)
        cpu_per_core = psutil.cpu_percent(interval=None, percpu=True)
        vm = psutil.virtual_memory()
        disk = psutil.disk_usage(str(self._workspace))

        gpus = _sample_gpus()

        # DB counts (fast reads)
        try:
            workers_busy = con.execute(
                "SELECT COUNT(*) FROM workers WHERE state='busy'"
            ).fetchone()[0]
            pages_done = con.execute(
                "SELECT COALESCE(SUM(units_done),0) FROM tasks WHERE state='done'"
            ).fetchone()[0]
            tasks_pending = con.execute(
                "SELECT COUNT(*) FROM tasks WHERE state='pending'"
            ).fetchone()[0]
        except Exception:
            workers_busy = pages_done = tasks_pending = 0

        return SystemSnapshot(
            ts=now,
            cpu_pct=cpu_pct,
            cpu_per_core=cpu_per_core if isinstance(cpu_per_core, list) else [cpu_pct],
            ram_used_mb=vm.used / 1024 / 1024,
            ram_total_mb=vm.total / 1024 / 1024,
            disk_free_gb=disk.free / 1024 / 1024 / 1024,
            gpus=gpus,
            workers_busy=workers_busy,
            pages_done=pages_done,
            tasks_pending=tasks_pending,
        )

    def _maybe_flush(self, con: Any, snap: SystemSnapshot) -> None:
        now = snap.ts
        self._tier1_buf.append(snap)
        self._tier2_buf.append(snap)

        # Tier 1: 60-second buckets
        if now - self._last_tier1 >= 60:
            self._flush_bucket(con, self._tier1_buf, tier=1)
            self._tier1_buf = []
            self._last_tier1 = now

        # Tier 2: 300-second buckets
        if now - self._last_tier2 >= 300:
            self._flush_bucket(con, self._tier2_buf, tier=2)
            self._tier2_buf = []
            self._last_tier2 = now

    def _flush_bucket(self, con: Any, buf: list[SystemSnapshot], tier: int) -> None:
        if not buf:
            return
        # Average the bucket
        avg_ts = sum(s.ts for s in buf) / len(buf)
        avg_cpu = sum(s.cpu_pct for s in buf) / len(buf)
        avg_ram = sum(s.ram_used_mb for s in buf) / len(buf)
        avg_disk = sum(s.disk_free_gb for s in buf) / len(buf)
        avg_busy = int(sum(s.workers_busy for s in buf) / len(buf))
        pages = buf[-1].pages_done
        pending = buf[-1].tasks_pending
        gpu_json = json.dumps(buf[-1].gpus)
        ram_total = buf[0].ram_total_mb

        try:
            with write_txn(con):
                con.execute(
                    """INSERT OR REPLACE INTO samples
                       (ts, tier, cpu, ram_used_mb, ram_total_mb, disk_free_gb,
                        gpu_json, workers_busy, pages_done, tasks_pending)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        avg_ts,
                        tier,
                        avg_cpu,
                        avg_ram,
                        ram_total,
                        avg_disk,
                        gpu_json,
                        avg_busy,
                        pages,
                        pending,
                    ),
                )
        except Exception:
            pass


def _sample_gpus() -> list[dict[str, Any]]:
    if not _NVML_OK:
        return []
    try:
        count = pynvml.nvmlDeviceGetCount()
        gpus = []
        for i in range(count):
            h = pynvml.nvmlDeviceGetHandleByIndex(i)
            util = pynvml.nvmlDeviceGetUtilizationRates(h)
            mem = pynvml.nvmlDeviceGetMemoryInfo(h)
            temp = pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU)
            try:
                power = pynvml.nvmlDeviceGetPowerUsage(h) / 1000  # mW → W
            except pynvml.NVMLError:
                power = None
            name = pynvml.nvmlDeviceGetName(h)
            if isinstance(name, bytes):
                name = name.decode()
            gpus.append(
                {
                    "index": i,
                    "name": name,
                    "util_pct": util.gpu,
                    "vram_used_mb": mem.used / 1024 / 1024,
                    "vram_total_mb": mem.total / 1024 / 1024,
                    "temp_c": temp,
                    "power_w": power,
                }
            )
        return gpus
    except Exception:
        return []
