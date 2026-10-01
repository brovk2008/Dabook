"""
ETA estimation engine (§7.4).

Uses a three-tier cost model:
1. EWMA of completed tasks in this run (alpha=0.3)
2. Historical median from stage_stats across previous runs
3. Static priors (conservative)

Reports a mid estimate and a confidence band.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

# Static priors: ms per page per stage (very conservative)
_PRIORS: dict[str, float] = {
    "s00_register": 50.0,
    "s01_inspect": 200.0,
    "s02_extract": 500.0,  # born-digital; 5–50× for scanned
    "s03_merge": 100.0,
    "s04_furniture": 200.0,
    "s05_reading_order": 300.0,
    "s06_structure": 400.0,
    "s07_continuity": 500.0,
    "s08_typed_content": 600.0,
    "s09_graph": 300.0,
    "s10_clean": 200.0,
    "s11_validate": 150.0,
    "s12_semantic": 1000.0,
    "s13_compile": 200.0,
}

_ALPHA = 0.3  # EWMA smoothing factor


@dataclass
class Eta:
    mid_s: float  # best estimate (seconds from now)
    low_s: float  # lower bound of band
    high_s: float  # upper bound of band
    confident: bool  # True when enough samples exist
    sample_count: int = 0

    def format(self) -> str:
        if not self.confident or self.mid_s <= 0:
            return "calculating…"
        mid_m = self.mid_s / 60
        low_m = self.low_s / 60
        high_m = self.high_s / 60
        if mid_m < 1:
            return f"~{self.mid_s:.0f}s"
        return f"~{mid_m:.0f} min ({low_m:.0f}–{high_m:.0f})"


@dataclass
class EtaEngine:
    """Stateful ETA engine; call ``record()`` as tasks complete."""

    _ewma: dict[str, float] = field(default_factory=dict)
    _sample_counts: dict[str, int] = field(default_factory=dict)

    def record(self, stage: str, units: int, ms: int) -> None:
        if units <= 0:
            return
        ms_per_unit = ms / units
        if stage in self._ewma:
            self._ewma[stage] = _ALPHA * ms_per_unit + (1 - _ALPHA) * self._ewma[stage]
        else:
            self._ewma[stage] = ms_per_unit
        self._sample_counts[stage] = self._sample_counts.get(stage, 0) + 1

    def ms_per_unit(self, stage: str, con: sqlite3.Connection | None = None) -> float:
        # Tier 1: EWMA from this run
        if stage in self._ewma and self._sample_counts.get(stage, 0) >= 3:
            return self._ewma[stage]
        # Tier 2: historical median
        if con is not None:
            hist = _historical_median(con, stage)
            if hist is not None:
                return hist
        # Tier 3: prior
        return _PRIORS.get(stage, 500.0)

    def estimate(
        self,
        con: sqlite3.Connection,
        live_workers: dict[str, int],
    ) -> Eta:
        """Compute overall ETA from pending/running tasks."""
        rows = con.execute(
            """SELECT t.stage, t.resource_class, t.units_total, t.units_done,
                      t.units_total - t.units_done AS remaining_units,
                      b.doc_type
                 FROM tasks t JOIN books b ON b.id=t.book_id
                WHERE t.state IN ('pending','running')""",
        ).fetchall()

        if not rows:
            return Eta(0, 0, 0, confident=True)

        lane_cost: dict[str, float] = {}
        for r in rows:
            stage = r["stage"]
            remaining = max(0, (r["remaining_units"] or 1))
            cost_ms = self.ms_per_unit(stage, con) * remaining
            lane = r["resource_class"]
            lane_cost[lane] = lane_cost.get(lane, 0.0) + cost_ms

        lane_eta_ms: dict[str, float] = {}
        for lane, cost in lane_cost.items():
            workers = max(1, live_workers.get(lane, 1))
            runnable = sum(1 for r in rows if r["resource_class"] == lane)
            effective = min(workers, runnable)
            lane_eta_ms[lane] = cost / effective

        mid_ms = max(lane_eta_ms.values(), default=0.0)
        mid_s = mid_ms / 1000.0

        # Spread from recent duration variance
        spread = _recent_spread(con)
        low_s = mid_s * (1 - spread)
        high_s = mid_s * (1 + spread)

        total_samples = sum(self._sample_counts.values())
        confident = total_samples >= 3

        return Eta(mid_s, low_s, high_s, confident=confident, sample_count=total_samples)


def _historical_median(con: sqlite3.Connection, stage: str) -> float | None:
    rows = con.execute(
        """SELECT CAST(ms AS REAL) / CAST(units AS REAL) AS mpu
             FROM stage_stats
            WHERE stage=? AND units > 0
            ORDER BY ts DESC
            LIMIT 20""",
        (stage,),
    ).fetchall()
    if len(rows) < 3:
        return None
    vals = sorted(r["mpu"] for r in rows)
    n = len(vals)
    if n % 2 == 0:
        return (vals[n // 2 - 1] + vals[n // 2]) / 2
    return vals[n // 2]


def _recent_spread(con: sqlite3.Connection) -> float:
    """Return clamp(p75/p50 - 1, 0.1, 0.6) from recent task durations."""
    rows = con.execute(
        """SELECT duration_ms FROM tasks
            WHERE state='done' AND duration_ms IS NOT NULL
            ORDER BY finished_at DESC LIMIT 100""",
    ).fetchall()
    if len(rows) < 6:
        return 0.4  # wide band while few samples
    vals = sorted(r["duration_ms"] for r in rows)
    n = len(vals)
    p50 = vals[n // 2]
    p75 = vals[int(n * 0.75)]
    if p50 <= 0:
        return 0.4
    spread = (p75 / p50) - 1.0
    return max(0.1, min(0.6, spread))
