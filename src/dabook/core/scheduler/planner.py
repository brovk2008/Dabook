"""
Stage planner — computes the task DAG for a book and inserts/updates tasks in the DB.

For each book, the planner:
1. Walks the stage DAG definition
2. Computes the stage_key (content-addressed cache key)
3. Checks if a valid artifact already exists → marks as ``skipped``
4. Otherwise inserts as ``pending``
5. Records dependency edges in ``task_deps``
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dabook.core.atomic import is_valid_commit
from dabook.core.hashing import stage_key as compute_stage_key
from dabook.core.store.db import write_txn

# ---------------------------------------------------------------------------
# Stage definition
# ---------------------------------------------------------------------------


@dataclass
class StageSpec:
    """Describes one pipeline stage."""

    name: str
    order: int
    resource_class: str  # cpu | gpu | io | llm
    impl_version: str
    affects_output: list[str]  # param keys that affect the stage_key
    sharded: bool = False  # True → one task per page shard (S2)
    deps: list[str] = field(default_factory=list)  # stage names this depends on


# Stage DAG definition (S0–S13)
STAGE_DAG: list[StageSpec] = [
    StageSpec("s00_register", 0, "io", "0.1.0", []),
    StageSpec("s01_inspect", 1, "cpu", "0.1.0", ["backend", "ocr_dpi"], deps=["s00_register"]),
    StageSpec(
        "s02_extract",
        2,
        "cpu",
        "0.1.0",
        ["backend", "ocr_engine", "ocr_dpi", "layout_model", "render_dpi_assets"],
        sharded=True,
        deps=["s01_inspect"],
    ),
    StageSpec("s03_merge", 3, "cpu", "0.1.0", [], deps=["s02_extract"]),
    StageSpec("s04_furniture", 4, "cpu", "0.1.0", [], deps=["s03_merge"]),
    StageSpec("s05_reading_order", 5, "cpu", "0.1.0", [], deps=["s04_furniture"]),
    StageSpec(
        "s06_structure",
        6,
        "cpu",
        "0.1.0",
        ["toc_mode", "detect_regions"],
        deps=["s05_reading_order"],
    ),
    StageSpec(
        "s07_continuity",
        7,
        "cpu",
        "0.1.0",
        ["merge_threshold", "uncertain_threshold"],
        deps=["s06_structure"],
    ),
    StageSpec("s08_typed_content", 8, "cpu", "0.1.0", [], deps=["s07_continuity"]),
    StageSpec("s09_graph", 9, "cpu", "0.1.0", [], deps=["s08_typed_content"]),
    StageSpec(
        "s10_clean",
        10,
        "cpu",
        "0.1.0",
        ["dehyphenate", "fix_spaced_letters", "ocr_repair", "unicode_normal"],
        deps=["s09_graph"],
    ),
    StageSpec(
        "s11_validate",
        11,
        "cpu",
        "0.1.0",
        ["quality_verified", "quality_good", "quality_review"],
        deps=["s10_clean"],
    ),
    StageSpec("s12_semantic", 12, "cpu", "0.1.0", ["assist_provider"], deps=["s11_validate"]),
    StageSpec(
        "s13_compile",
        13,
        "io",
        "0.1.0",
        [
            "modes",
            "formats",
            "text_field",
            "chunk_target_tokens",
            "chunk_max_tokens",
            "split_by",
            "split_ratios",
            "dedup_exact",
            "dedup_near",
        ],
        deps=["s12_semantic"],
    ),
]

STAGE_BY_NAME = {s.name: s for s in STAGE_DAG}


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


def plan_book(
    con: sqlite3.Connection,
    book_id: int,
    book_sha: str,
    page_count: int,
    workspace: Path,
    params: dict[str, Any],
    shard_pages: int = 20,
) -> dict[str, int]:
    """
    Plan all tasks for *book_id*.

    Returns counts: ``{"pending": N, "skipped": N, "unchanged": N}``.
    """
    counts: dict[str, int] = {"pending": 0, "skipped": 0, "unchanged": 0}

    # Build mapping: stage_name → list[task_id]
    stage_tasks: dict[str, list[int]] = {}
    book12 = book_sha[:12]

    for spec in STAGE_DAG:
        shards = _build_shards(spec, page_count, shard_pages)

        for shard_idx, (page_start, page_end) in enumerate(shards):
            # Gather inputs: source file sha + outputs of dependency stages
            input_shas = [book_sha]

            # Compute stage_key
            affecting = {k: params.get(k) for k in spec.affects_output}
            if spec.sharded:
                affecting["shard_idx"] = shard_idx
                affecting["page_start"] = page_start
                affecting["page_end"] = page_end

            key = compute_stage_key(
                spec.name,
                spec.impl_version,
                affecting,
                input_shas,
            )

            # Check for cached output
            stage_dir = workspace / "books" / book12 / "stages" / spec.name
            output_path = stage_dir / key
            cached = is_valid_commit(output_path)
            initial_state = "skipped" if cached else "pending"

            with write_txn(con):
                # Upsert: if task with this stage_key exists, leave it; else insert
                existing = con.execute(
                    """SELECT id, state FROM tasks
                        WHERE book_id=? AND stage=? AND shard_idx=?""",
                    (book_id, spec.name, shard_idx),
                ).fetchone()

                if existing and existing["stage_key"] == key:
                    task_id = int(existing["id"])
                    counts["unchanged"] += 1
                elif existing:
                    # Stage key changed (params changed) — reset task
                    con.execute(
                        """UPDATE tasks
                              SET stage_key=?, state=?, output_path=NULL,
                                  output_sha256=NULL, attempts=0, not_before=0,
                                  error_class=NULL, error_msg=NULL
                            WHERE id=?""",
                        (key, initial_state, existing["id"]),
                    )
                    task_id = int(existing["id"])
                    counts[initial_state] += 1
                else:
                    cur = con.execute(
                        """INSERT INTO tasks
                           (book_id, stage, stage_order, shard_idx, page_start, page_end,
                            stage_key, resource_class, state, max_attempts,
                            units_total, output_path)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                           RETURNING id""",
                        (
                            book_id,
                            spec.name,
                            spec.order,
                            shard_idx,
                            page_start,
                            page_end,
                            key,
                            spec.resource_class,
                            initial_state,
                            params.get("max_attempts", 3),
                            (page_end - page_start) if page_start is not None else 1,
                            str(output_path) if cached else None,
                        ),
                    )
                    task_id = int(cur.fetchone()["id"])
                    counts[initial_state] += 1

            stage_tasks.setdefault(spec.name, []).append(task_id)

        # Insert dependency edges
        for dep_stage in spec.deps:
            dep_tasks = stage_tasks.get(dep_stage, [])
            my_tasks = stage_tasks.get(spec.name, [])
            if not dep_tasks or not my_tasks:
                continue

            if spec.sharded and dep_stage == "s01_inspect":
                # Each shard depends on the single inspect task
                for my_tid in my_tasks:
                    for dep_tid in dep_tasks:
                        _insert_dep(con, my_tid, dep_tid)
            elif spec.name == "s03_merge":
                # Merge depends on ALL shards
                for my_tid in my_tasks:
                    for dep_tid in dep_tasks:
                        _insert_dep(con, my_tid, dep_tid)
            else:
                # Sequential stages: each task depends on corresponding task
                for my_tid, dep_tid in zip(my_tasks, dep_tasks, strict=False):
                    _insert_dep(con, my_tid, dep_tid)

    return counts


def _build_shards(
    spec: StageSpec,
    page_count: int,
    shard_pages: int,
) -> list[tuple[int | None, int | None]]:
    """Return list of (page_start, page_end) tuples for the stage."""
    if not spec.sharded or page_count == 0:
        return [(None, None)]
    shards = []
    for start in range(0, page_count, shard_pages):
        end = min(start + shard_pages, page_count)
        shards.append((start, end))
    return shards


def _insert_dep(con: sqlite3.Connection, task_id: int, dep_id: int) -> None:
    with write_txn(con):
        con.execute(
            "INSERT OR IGNORE INTO task_deps (task_id, depends_on) VALUES (?, ?)",
            (task_id, dep_id),
        )
