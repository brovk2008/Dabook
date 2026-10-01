"""
Settings store — live key-value table polled by workers every 2 s.

Settings are typed; workers and supervisor read via ``read_settings()``
which caches for 2 s to avoid hammering the DB.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from dabook.core.store.db import write_txn


@dataclass
class LiveSettings:
    """Typed snapshot of the settings table."""

    # Worker counts
    workers_cpu: int = 4
    workers_gpu: int = 1
    workers_io: int = 2
    workers_llm: int = 0

    book_concurrency: int = 2
    queue_paused: bool = False
    stopping: bool = False  # soft-stop (Ctrl+C once)

    # Resource guards
    ram_ceiling_pct: float = 85.0
    vram_ceiling_pct: float = 90.0
    min_free_gb: float = 5.0

    # Task settings
    shard_pages: int = 20
    max_attempts: int = 3
    backoff_s: float = 5.0
    lease_s: float = 90.0
    heartbeat_s: float = 15.0

    # Quality thresholds
    quality_verified: float = 0.95
    quality_good: float = 0.85
    quality_review: float = 0.70

    # Dataset compilation settings
    dataset_merge_all: bool = True
    dataset_filter_boilerplate: bool = True
    dataset_sft_format: str = "both"

    def free_book_slots(self, active_books: int) -> int:
        return max(0, self.book_concurrency - active_books)


def load_settings(con: sqlite3.Connection) -> LiveSettings:
    """Read all settings from DB and return a typed ``LiveSettings``."""
    rows = con.execute("SELECT k, v FROM settings").fetchall()
    kv = {r["k"]: r["v"] for r in rows}

    def i(k: str, default: int) -> int:
        return int(kv.get(k, default))

    def f(k: str, default: float) -> float:
        return float(kv.get(k, default))

    def b(k: str, default: bool = False) -> bool:
        return kv.get(k, "1" if default else "0") not in ("0", "false", "False")

    return LiveSettings(
        workers_cpu=i("workers.cpu", 4),
        workers_gpu=i("workers.gpu", 1),
        workers_io=i("workers.io", 2),
        workers_llm=i("workers.llm", 0),
        book_concurrency=i("book_concurrency", 2),
        queue_paused=b("queue.paused"),
        stopping=b("stopping"),
        ram_ceiling_pct=f("ram_ceiling_pct", 85.0),
        vram_ceiling_pct=f("vram_ceiling_pct", 90.0),
        min_free_gb=f("min_free_gb", 5.0),
        shard_pages=i("shard_pages", 20),
        max_attempts=i("retry.max_attempts", 3),
        backoff_s=f("retry.backoff_s", 5.0),
        lease_s=f("lease_s", 90.0),
        heartbeat_s=f("heartbeat_s", 15.0),
        quality_verified=f("quality.verified", 0.95),
        quality_good=f("quality.good", 0.85),
        quality_review=f("quality.review", 0.70),
        dataset_merge_all=b("dataset.merge_all", True),
        dataset_filter_boilerplate=b("dataset.filter_boilerplate", True),
        dataset_sft_format=str(kv.get("dataset.sft_format", "both")),
    )


class SettingsCache:
    """Thread-safe settings cache with 2-second TTL."""

    def __init__(self, con: sqlite3.Connection, ttl: float = 2.0) -> None:
        self._con = con
        self._ttl = ttl
        self._cached: LiveSettings | None = None
        self._expires: float = 0.0

    def get(self) -> LiveSettings:
        now = time.time()
        if self._cached is None or now >= self._expires:
            self._cached = load_settings(self._con)
            self._expires = now + self._ttl
        return self._cached

    def invalidate(self) -> None:
        self._expires = 0.0


def update_setting(con: sqlite3.Connection, key: str, value: Any) -> None:
    """Update a single setting value."""
    with write_txn(con):
        con.execute(
            "UPDATE settings SET v=?, updated_at=? WHERE k=?",
            (str(value), time.time(), key),
        )


def set_stopping(con: sqlite3.Connection, value: bool) -> None:
    update_setting(con, "stopping", "1" if value else "0")
