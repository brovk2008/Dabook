# 📚 DABOOK

> Compile any book-like PDF into a **traceable, validated, editable Book Graph**, then export it as ML-ready datasets (`raw` / `pretrain` / `sft` / `rag` / `code`) — resumable, multi-book, parallel, with a live localhost control room.

[![CI](https://github.com/brovk2008/Dabook/actions/workflows/ci.yml/badge.svg)](https://github.com/brovk2008/Dabook/actions/workflows/ci.yml)
[![CodeQL](https://github.com/brovk2008/Dabook/actions/workflows/codeql.yml/badge.svg)](https://github.com/brovk2008/Dabook/actions/workflows/codeql.yml)
[![Version](https://img.shields.io/badge/version-0.1.0-orange.svg)](CHANGELOG.md)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://python.org)
[![SQLite](https://img.shields.io/badge/SQLite-3.35%2B-lightgrey.svg)](https://sqlite.org)
[![Status](https://img.shields.io/badge/status-pre--alpha-red.svg)](pyproject.toml)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)

---

## Version

**Current release: `v0.1.0` (Pre-Alpha)**

This is early-stage software. The core pipeline (S00–S13) and worker/supervisor runtime are functional. Some features listed in the CLI help (e.g. `cache gc`, `--watch`, `--autotune`) are planned but not yet implemented.

See [CHANGELOG.md](CHANGELOG.md) for release history.

---

## ⚡ Quick Start

### Prerequisites

- Python 3.11 or newer
- SQLite 3.35 or newer (ships with Python ≥ 3.12; check with `python -c "import sqlite3; print(sqlite3.sqlite_version)"`)
- ~5 GB free disk space per book

### Install

```bash
# Recommended: install with uv
uv tool install dabook

# Or with pip
pip install dabook
```

### Run

```bash
# Process a single book — starts workers, launches dashboard
dabook run my_book.pdf

# Process a whole directory
dabook run ./books/

# Resume an interrupted run with zero re-work
dabook resume

# Check that your environment is ready
dabook doctor
```

The live control room dashboard opens automatically at **`http://127.0.0.1:8765`**.

---

## 🌟 Key Features

- **Guaranteed Resumable** — `kill -9` or power loss at any instant; `dabook resume` picks up with zero re-work thanks to two-phase atomic filesystem commits and content-addressed caching.
- **Zero-Redo Caching** — Same PDF + same config = instant skip. The cache key is `sha256(pdf) + sha256(stage_params)`.
- **Multi-Book Parallelism** — Process batches of books concurrently. Worker counts, book concurrency, and memory ceilings are all live-adjustable from the dashboard without restart.
- **Live Localhost Control Room** — Real-time SSE dashboard at `http://127.0.0.1:8765` showing task throughput, worker pool status, per-book progress, error logs, and ETA.
- **Full Provenance Chain** — Every token traces back: PDF → Page → BBox → Block → Node → Export Record.
- **Multiple ML Export Formats** — Produces five JSONL files per book (see [Export Formats](#-export-formats) below).
- **Human-Editable** — The Book Graph is plain JSON; you can inspect and patch any node and re-compile.

---

## 🏗️ Pipeline Stages (S00–S13)

```
PDF → S00 → S01 → S02 (×shards) → S03 → S04 → S05 → S06
                                                        ↓
          datasets ← S13 ← S12 ← S11 ← S10 ← S09 ← S08 ← S07
```

| Stage | Name | Pool | What it does |
|-------|------|------|--------------|
| **S00** | `s00_register` | `io` | Hash the PDF (SHA-256), create workspace dirs, write source metadata |
| **S01** | `s01_inspect` | `cpu` | Extract page count, detect born-digital vs scanned, parse PDF bookmarks (TOC) |
| **S02** | `s02_extract` | `cpu` | Sharded parallel extraction — text blocks, bounding boxes, char counts per page |
| **S03** | `s03_merge` | `cpu` | Merge all extraction shards into a single sorted page stream |
| **S04** | `s04_furniture` | `cpu` | Detect and mark running headers, footers, page numbers, and watermarks |
| **S05** | `s05_reading_order` | `cpu` | XY-cut column detection; reconstruct true top-to-bottom, left-to-right reading order |
| **S06** | `s06_structure` | `cpu` | Reconcile PDF TOC bookmarks with font-weight heuristics to label headings and chapters |
| **S07** | `s07_continuity` | `cpu` | Stitch paragraphs broken across page boundaries; flag cross-page continuations |
| **S08** | `s08_typed_content` | `cpu` | Classify blocks as `code`, `list_item`, or `paragraph`; detect x86/x64 assembly and C |
| **S09** | `s09_graph` | `cpu` | Build the canonical **Book Graph** — a typed node tree (book → chapter → section → block) |
| **S10** | `s10_clean` | `cpu` | Unicode NFC normalization, ligature healing (`fi nd` → `find`), dehyphenation |
| **S11** | `s11_validate` | `cpu` | Compute quality score; check word count, heading count, and code block count |
| **S12** | `s12_semantic` | `cpu` | Add breadcrumb paths to every node (e.g. `["Chapter 3", "Memory Layout"]`) |
| **S13** | `s13_compile` | `io` | Export all five dataset formats (JSONL); consolidate workspace-level manifests |

All stages run through the same crash-safe commit protocol: write to `.partial/` → atomic rename → write `_SUCCESS`. A stage output is valid if and only if `_SUCCESS` exists.

---

## 📦 Export Formats

S13 produces five JSONL files inside `<workspace>/books/<sha12>/datasets/`:

| File | Description |
|------|-------------|
| `raw.jsonl` | Every non-root node with its text, type, page index, and breadcrumbs |
| `pretrain.jsonl` | Document-level Markdown chapters with fenced code blocks; suitable for continued pre-training |
| `sft.jsonl` | Supervised fine-tuning pairs — concept Q&A, code walkthroughs, and exercise answers; Alpaca and ChatML format |
| `code.jsonl` | Multi-line code and assembly snippets with language tag and surrounding context |
| `rag.jsonl` | ~400-word semantic chunks bounded by section boundaries, each with full breadcrumb path |

A `manifest.json` and `<workspace>/datasets/manifest.json` (consolidated across all books) are also written.

---

## 💻 CLI Reference

```bash
# ── Processing ──────────────────────────────────────────────────────
dabook run <path> [<path>...]          # Process PDFs or directories
  --workspace / -w <dir>              # Workspace dir (default: ./dabook_workspace)
  --profile fast|balanced|accurate|lowram
  --workers-cpu N                     # Override CPU worker count
  --workers-gpu N                     # Override GPU worker count
  --book-concurrency N                # Max books processed at once
  --port 8765                         # Dashboard port
  --no-ui                             # Skip dashboard
  --no-open                           # Don't auto-open browser

dabook resume [-w <dir>]              # Resume all work in a workspace

# ── Monitoring ──────────────────────────────────────────────────────
dabook status [-w <dir>] [--json]     # Print book/task counts by state
dabook ui [-w <dir>] [--port 8765]    # Start dashboard for an existing workspace
dabook doctor [-w <dir>]              # Check env, deps, SQLite version, disk space

# ── Queue Control ───────────────────────────────────────────────────
dabook pause [-w <dir>]               # Pause the processing queue
dabook continue [-w <dir>]            # Resume a paused queue
dabook stop [-w <dir>]                # Soft-stop (finish current task then exit)

# ── Recovery ────────────────────────────────────────────────────────
dabook retry [-w <dir>] [--dead] [--failed] [--book <sha_prefix>]

# ── Cache ────────────────────────────────────────────────────────────
dabook cache stats [-w <dir>]         # Count committed stage artifacts
dabook cache verify [-w <dir>]        # Deep-verify SHA-256 of all artifacts
dabook cache gc                       # (planned, not yet implemented)
```

---

## ⚙️ Worker Pools

Workers are spread across four resource classes. The supervisor spawns and drains them automatically; all counts are live-adjustable in the dashboard.

| Pool | Default | Used for |
|------|---------|----------|
| `cpu` | 4 | Most pipeline stages (S01–S12) |
| `io` | 2 | Register (S00), merge (S03), compile (S13) |
| `gpu` | 1 | Reserved for future GPU-accelerated backends |
| `llm` | 0 | Reserved for future LLM-assisted stages |

The supervisor uses a **crash loop breaker**: if a worker lane crashes 3+ times within 5 minutes, spawning is paused for 30 seconds before retrying.

---

## 🎯 Design Principles

1. **The block is the unit of meaning, never the page.** Page boundaries are printing artifacts; semantic blocks transcend them.
2. **Deterministic first, AI second.** Regex, font heuristics, and bbox geometry run before any neural model.
3. **Strict source-faithfulness.** Raw text is never discarded; all generated/inferred content is flagged separately.
4. **Three text representations.** `raw` (exact PDF glyphs) → `normalized` (ligatures, hyphens fixed) → `clean` (furniture stripped, Unicode NFC).
5. **Infrastructure before intelligence.** WAL-mode SQLite, process leases, atomic commits, and crash recovery come before ML pipelines.

---

## 🗄️ State Store

All state lives in `<workspace>/state.db` (SQLite, WAL mode). Key tables:

| Table | Purpose |
|-------|---------|
| `books` | One row per PDF; tracks state (`queued` → `active` → `done`/`failed`) |
| `tasks` | One row per stage+shard; owns the lease, heartbeat, and output path |
| `workers` | Live worker registry with PID, RSS, and current task |
| `settings` | Live-editable key-value config polled every 2 s by workers |
| `events` | Append-only event log (shown in dashboard log tail) |
| `samples` | Downsampled telemetry (CPU/RAM/disk/GPU) for history charts |

---

## 🤝 Contributing

```bash
git clone https://github.com/brovk2008/Dabook.git
cd Dabook
uv venv --python 3.11
source .venv/bin/activate        # Windows: .\.venv\Scripts\Activate.ps1
uv pip install -e ".[dev]"
pytest                           # run the test suite
pytest -m "not slow"             # skip crash-safety tests
```

See [CONTRIBUTING.md](CONTRIBUTING.md), [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md), [SECURITY.md](SECURITY.md), and [SUPPORT.md](SUPPORT.md).

---

## ⚖️ License

Distributed under the **Apache License 2.0**. See [`LICENSE`](LICENSE) for details.
Optional third-party plugins or proprietary model weights have separate licenses.
