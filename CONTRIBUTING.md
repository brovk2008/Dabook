# Contributing to DABOOK

Welcome to DABOOK! We are delighted that you are interested in contributing. Whether you are fixing a typo, improving documentation, adding an extraction backend, optimizing the Book Graph IR, or writing chaos tests, your contributions are warmly appreciated.

This guide will help you get set up quickly and walk you through our development workflow.

---

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [How Can You Contribute?](#how-can-you-contribute)
- [Development Setup](#development-setup)
- [Project Architecture & Structure](#project-architecture--structure)
- [Coding Guidelines & Standards](#coding-guidelines--standards)
- [Running Tests & Linting](#running-tests--linting)
- [Git Workflow & Commit Guidelines](#git-workflow--commit-guidelines)
- [Submitting a Pull Request](#submitting-a-pull-request)
- [Questions & Getting Help](#questions--getting-help)

---

## Code of Conduct

All contributors and participants are expected to adhere to our [Code of Conduct](CODE_OF_CONDUCT.md). Please read it before participating in our issues, discussions, or pull requests.

---

## How Can You Contribute?

Here are some great ways to get involved:

- **Bug Reports:** Found an edge case with a malformed PDF or unexpected scheduler behavior? [Open an issue](https://github.com/brovk2008/Dabook/issues/new?template=bug_report.yml).
- **Documentation:** Improve docstrings, add examples to the README, or clarify architecture decisions.
- **Parsing Backends:** Help implement or benchmark alternative PDF engines (e.g., Docling, Marker, pdfminer).
- **Core Pipeline Stages:** Contribute to the S0–S13 pipeline stages (heuristic furniture removal, table extraction, formula preservation, Book Graph serialization).
- **Dashboard / Frontend:** Enhance the live localhost dashboard (`fastapi` + SSE + Vanilla JS) with clearer telemetry visualizations or accessibility improvements.
- **Chaos & Resilience Testing:** Write kill-recovery tests, concurrent access benchmarks, or stress tests.

---

## Development Setup

DABOOK requires **Python 3.11+**. We recommend using [`uv`](https://github.com/astral-sh/uv) for fast, deterministic dependency management, but standard `pip` and `venv` work as well.

### 1. Fork and Clone the Repository

```bash
git clone https://github.com/brovk2008/Dabook.git
cd Dabook
```

### 2. Create and Activate a Virtual Environment

Using `uv` (recommended):
```bash
uv venv --python 3.11
# Windows PowerShell:
.\.venv\Scripts\Activate.ps1
# Linux / macOS:
source .venv/bin/activate
```

Or using standard Python:
```bash
python -m venv .venv
# Windows PowerShell:
.\.venv\Scripts\Activate.ps1
# Linux / macOS:
source .venv/bin/activate
```

### 3. Install in Editable Mode with Dev Dependencies

```bash
uv pip install -e ".[dev]"
# Or with standard pip:
pip install -e ".[dev]"
```

### 4. Install Pre-Commit Hooks (Optional but Recommended)

```bash
pre-commit install
```

### 5. Verify Your Environment

Run the DABOOK doctor command and the test suite:

```bash
dabook --help
pytest
```

If all tests pass, your local environment is ready!

---

## Project Architecture & Structure

```
DaBook/
├── src/dabook/
│   ├── cli.py                  # Typer CLI (dabook run, resume, status, doctor, inspect, cache)
│   ├── config/                 # Pydantic settings & profile configs (fast, balanced, accurate)
│   ├── core/
│   │   ├── atomic.py           # Two-phase atomic filesystem commits with _SUCCESS manifests
│   │   ├── hashing.py          # Content SHA256 & provenance hashing
│   │   ├── ids.py              # Canonical identifier generation
│   │   ├── lock.py             # Single-supervisor workspace locking
│   │   ├── logging.py          # Dual file + console structured logging
│   │   ├── workspace.py        # Workspace layout & directory structure
│   │   ├── runtime/
│   │   │   ├── supervisor.py   # Supervisor process, health loop, signal handling
│   │   │   ├── telemetry.py    # Background 1 Hz system telemetry sampler (CPU, RAM, GPU)
│   │   │   └── worker.py       # Multi-pool worker lifecycle (CPU, GPU, IO, LLM)
│   │   ├── scheduler/
│   │   │   ├── eta.py          # Online ETA calculation with uncertainty bounds
│   │   │   ├── planner.py      # Stage DAG planner & task graph generation
│   │   │   └── recovery.py     # Stale lease & dead process recovery
│   │   └── store/
│   │       ├── db.py           # SQLite WAL connection & write transaction managers
│   │       ├── migrations.py   # Forward-only versioned schema migrations
│   │       ├── schema.sql      # Baseline relational schema
│   │       ├── settings.py     # Runtime dynamic settings store
│   │       └── tasks.py        # Atomic task claiming, heartbeats, and status transitions
│   ├── backends/               # Extraction backend adapters (pypdfium2, pdfminer, etc.)
│   ├── ir/                     # Book Graph Intermediate Representation schema
│   ├── server/                 # FastAPI localhost dashboard + SSE real-time streaming
│   └── stages/                 # S0–S13 pipeline stages
├── tests/
│   ├── unit/                   # Fast, isolated unit tests (atomic commits, DB store, lock)
│   ├── chaos/                  # Crash-safety and kill-recovery simulations
│   └── integration/            # Multi-stage and end-to-end integration tests
├── .github/                    # CI workflows, issue templates, PR templates
├── pyproject.toml              # Build config, dependencies, Ruff, Mypy, and Pytest configuration
├── LICENSE                     # Apache 2.0 license
├── README.md                   # Project overview & quick start
└── CONTRIBUTING.md             # This guide
```

---

## Coding Guidelines & Standards

1. **Modern Python:** We target Python 3.11+. Use modern syntax such as `str | None` instead of `Optional[str]`, `list[int]` instead of `typing.List[int]`, and native pattern matching when appropriate.
2. **Type Safety:** All functions, methods, and module interfaces must have complete type hints. Our code is checked with `mypy --strict`.
3. **Immutability & Provenance:** Every stage output in DABOOK must preserve provenance: `PDF -> page -> bbox -> block -> node -> record`.
4. **Crash Safety First:** Any new disk writes must use the two-phase atomic commit pattern (`StageCommit` in `dabook.core.atomic`). Never write directly to a final stage directory without an atomic rename and `_SUCCESS` manifest.
5. **No Blind Retries:** Distinguish transient recoverable errors from deterministic bugs or corrupted inputs. Permanent errors should dead-letter the task cleanly rather than looping indefinitely.

---

## Running Tests & Linting

### Linting & Formatting

We use **[Ruff](https://astral.sh/ruff)** for blazing-fast linting and code formatting:

```bash
# Check code style and common bugs
ruff check .

# Automatically apply safe fixes
ruff check . --fix

# Check code formatting
ruff format --check .

# Auto-format all files
ruff format .
```

### Type Checking

```bash
mypy src
```

### Running the Test Suite

```bash
# Run all fast unit and chaos tests
pytest

# Run tests with verbose output
pytest -v

# Run only unit tests
pytest tests/unit/

# Run crash-safety chaos tests
pytest tests/chaos/

# Run with test coverage
pytest --cov=dabook --cov-report=term-missing
```

---

## Git Workflow & Commit Guidelines

### 1. Branch Naming

Create a feature branch from `main`:

- `feat/feature-name` (e.g. `feat/pdfminer-backend`)
- `fix/bug-description` (e.g. `fix/stale-lease-race`)
- `docs/doc-update` (e.g. `docs/improve-architecture-diagram`)
- `test/test-description` (e.g. `test/add-worker-heartbeat-tests`)
- `perf/optimization` (e.g. `perf/sqlite-batch-claim`)

```bash
git checkout -b feat/my-enhancement
```

### 2. Commit Message Conventions

We follow the [Conventional Commits](https://www.conventionalcommits.org/) specification:

- `feat:` A new feature or capability
- `fix:` A bug fix
- `docs:` Documentation changes only
- `test:` Adding or updating tests
- `refactor:` Code change that neither fixes a bug nor adds a feature
- `perf:` Performance improvements
- `chore:` Maintenance, dependency bumps, or tool configuration

**Example:**
```
feat(scheduler): add dynamic backoff for rate-limited OCR workers

Implements exponential backoff with jitter when OCR worker slots
are temporarily starved. Adds unit test in tests/unit/test_scheduler.py.
```

---

## Submitting a Pull Request

1. **Keep it focused:** Each PR should address a single concern or feature.
2. **Add tests:** Include unit or chaos tests demonstrating that your change works and prevents regressions.
3. **Verify locally:** Ensure `ruff check .`, `ruff format --check .`, and `pytest` all pass before pushing.
4. **Create the PR:** Open a Pull Request against the `main` branch on GitHub.
5. **Fill in the PR template:** Provide context, explain the motivation, describe testing performed, and link any related issues.
6. **Code Review:** Maintainers will review your PR. Address feedback constructively, and push updates to your branch.

---

## Questions & Getting Help

- **Found a bug?** Open an [Issue](https://github.com/brovk2008/Dabook/issues).
- **Have an idea or architectural question?** Start a discussion on GitHub or reach out to the project maintainers.
- **Security concerns?** Please refer to our [Security Policy](SECURITY.md).

Thank you for helping make DABOOK the best open-source book-to-dataset compiler!
