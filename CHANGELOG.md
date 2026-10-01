# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

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
