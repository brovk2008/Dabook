# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

---

## [0.2.26] - 2026-10-02

### Fixed

- **Worker death loop** — the crash-loop breaker now clears `_death_history` after entering the
  30 s cooldown, so a single bad-spawn window no longer permanently blocks subsequent workers.
- **Startup blocking on previous Ctrl+C** — `_sync_settings` now always resets both the
  `stopping` and `queue.paused` flags to `0` on every `dabook run` / `resume`, preventing a
  prior graceful-shutdown from silently holding the queue paused on restart.
- **Worker claim-loop resilience** — transient SQLite errors (busy, locked) in the main
  `claim_task` loop no longer kill the worker process; they now trigger exponential backoff
  (0.5 s → 30 s) with a consecutive-error counter and a warning log.
- **`set_worker_idle` swallowed on queue-paused / no-task paths** — wrapped both idle-marking
  calls in a bare `except` so a flaky DB write never crashes a healthy worker.
- **Stage runner `FileExistsError` treated as error** — when two workers race to commit the
  same stage, the second now catches `FileExistsError` from `StageCommit` and returns a
  cache-hit result instead of propagating the exception.
- **Missing predecessor guard in stages s05–s13** — every stage function that reads a
  previous stage's output now raises a clear `RuntimeError` with a descriptive message
  (`"s05_reading_order: missing s04_furniture output"` etc.) instead of an opaque
  `AttributeError` / `NoneType` crash when a stage directory is absent.
- **Planner SQL query missing `stage_key` column** — the `SELECT` used by `plan_book` now
  includes `stage_key` so downstream row unpacking always has the expected number of fields.

### Changed

- **README** — reorganised and expanded documentation: added architecture overview, stage
  pipeline reference table, multi-pool worker model explanation, dashboard guide, dataset
  export formats, and contribution / license sections.
- **CLI `run` / `resume`** — added `"queue.paused": "0"` to the settings sync so a paused
  queue is always un-paused on a fresh invocation.



### Added
- Multi-pool worker execution model (CPU, GPU, IO, LLM) with dynamic concurrency controls.
- Interactive localhost control room dashboard (`FastAPI` + SSE real-time streaming).
- S0–S13 pipeline stage DAG planner and task generator.
- Cross-platform single-supervisor workspace file locking.
- 1 Hz telemetry background sampler monitoring CPU, memory, GPU VRAM, and disk storage.
- Comprehensive GitHub Community Health standards, CI matrix testing, and issue/PR templates.

---

## [0.1.0] - 2026-10-01

### Added
- Initial pre-alpha release of DABOOK.
- Atomic filesystem commits with two-phase staging and `_SUCCESS` manifest validation.
- SQLite WAL-mode state database with forward-only versioned migrations.
- Dynamic task scheduler with lease acquisition, heartbeats, and exponential backoff retry logic.
- Stale worker lease recovery and dead process detection.
- Core CLI commands: `run`, `resume`, `status`, `doctor`, `inspect`, and `cache`.
- Comprehensive unit and chaos kill-recovery test suite verifying crash-safety and zero data corruption.
