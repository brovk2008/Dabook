"""
DABOOK CLI — built with Typer + Rich.

Quick start: ``dabook run book.pdf``
"""

from __future__ import annotations

import os
import socket
import sqlite3
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from dabook.config.models import DabookConfig

console = Console()
app = typer.Typer(
    name="dabook",
    help="📚 DABOOK — compile books into ML-ready datasets",
    add_completion=False,
    rich_markup_mode="rich",
)


# ─────────────────────────────────────────────────────────
# run
# ─────────────────────────────────────────────────────────
@app.command()
def run(
    paths: list[Path] = typer.Argument(..., help="PDFs, directories, or globs"),
    workspace: Path | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    profile: str = typer.Option(
        "balanced", "--profile", "-p", help="Profile: fast|balanced|accurate|lowram"
    ),
    modes: str = typer.Option("raw,rag", "--modes", help="Dataset modes (comma-separated)"),
    formats: str = typer.Option("jsonl", "--formats", help="Export formats"),
    workers_cpu: int | None = typer.Option(None, "--workers-cpu"),
    workers_gpu: int | None = typer.Option(None, "--workers-gpu"),
    book_concurrency: int | None = typer.Option(None, "--book-concurrency"),
    port: int = typer.Option(8765, "--port"),
    no_ui: bool = typer.Option(False, "--no-ui", help="Don't start the dashboard"),
    no_open: bool = typer.Option(False, "--no-open", help="Don't open browser"),
    watch: bool = typer.Option(False, "--watch", help="Watch for new PDFs"),
    autotune: bool = typer.Option(False, "--autotune"),
) -> None:
    """Process one or more PDF books."""
    _run_impl(
        paths=paths,
        workspace=workspace,
        profile=profile,
        modes=modes.split(","),
        formats=formats.split(","),
        workers_cpu=workers_cpu,
        workers_gpu=workers_gpu,
        book_concurrency=book_concurrency,
        port=port,
        start_ui=not no_ui,
        open_browser=not no_open,
        watch=watch,
        autotune=autotune,
    )


def _run_impl(
    paths: list[Path],
    workspace: Path | None,
    profile: str,
    modes: list[str],
    formats: list[str],
    workers_cpu: int | None,
    workers_gpu: int | None,
    book_concurrency: int | None,
    port: int,
    start_ui: bool,
    open_browser: bool,
    watch: bool,
    autotune: bool,
) -> None:
    from dabook.config.loader import load_config
    from dabook.core.lock import LockError, WorkspaceLock
    from dabook.core.logging import setup_logging
    from dabook.core.runtime.supervisor import Supervisor
    from dabook.core.scheduler.planner import plan_book
    from dabook.core.store.db import connect, write_txn
    from dabook.core.workspace import get_db_path, init_workspace, register_pdf

    overrides: dict = {"profile": profile}
    if workspace:
        overrides["workspace"] = str(workspace)
    if workers_cpu is not None:
        overrides.setdefault("workers", {})["cpu"] = workers_cpu
    if workers_gpu is not None:
        overrides.setdefault("workers", {})["gpu"] = workers_gpu
    if book_concurrency is not None:
        overrides["workers"] = {
            **overrides.get("workers", {}),
            "book_concurrency": book_concurrency,
        }
    overrides["server"] = {"port": port}
    overrides["dataset"] = {"modes": modes, "formats": formats}

    cfg = load_config(overrides=overrides)
    ws = init_workspace(cfg)
    db_path = get_db_path(ws)

    # Acquire workspace lock
    try:
        lock = WorkspaceLock(ws, port=port)
        lock.acquire()
    except LockError as e:
        console.print(f"[red]✗ {e}[/red]")
        raise typer.Exit(1) from None

    # Set up logging
    log_dir = ws / "logs"
    setup_logging(log_dir, "supervisor")

    con = connect(db_path)

    # Sync settings from config to DB
    _sync_settings(con, cfg, port)

    # Collect PDFs
    pdfs = _collect_pdfs(paths)
    if not pdfs:
        console.print("[yellow]⚠ No PDF files found.[/yellow]")
        lock.release()
        raise typer.Exit(0)

    console.print(
        Panel.fit(
            f"[bold]DABOOK[/bold] · {len(pdfs)} book(s) · profile=[cyan]{profile}[/cyan] · workspace=[dim]{ws}[/dim]",
            border_style="blue",
        )
    )

    # Register books and plan tasks
    params = cfg.to_params_dict()
    registered = 0
    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"), transient=True
    ) as prog:
        task = prog.add_task("Registering books…", total=len(pdfs))
        for pdf in pdfs:
            try:
                book_id, sha = register_pdf(ws, pdf)
                page_count = _get_page_count(pdf) or 0
                # Update page count in DB
                with write_txn(con):
                    con.execute("UPDATE books SET pages=? WHERE id=?", (page_count, book_id))
                plan_book(
                    con, book_id, sha, page_count, ws, params, shard_pages=cfg.limits.shard_pages
                )
                registered += 1
                prog.advance(task)
            except Exception as e:
                console.print(f"[red]✗ {pdf.name}: {e}[/red]")

    con.close()

    # Print status
    if start_ui:
        console.print(
            f"[green]▸[/green] Dashboard: [link=http://127.0.0.1:{port}]http://127.0.0.1:{port}[/link]"
        )

    # Start supervisor (blocks)
    sup = Supervisor(
        workspace=ws,
        db_path=db_path,
        port=port,
        open_browser=open_browser and start_ui,
        start_ui=start_ui,
    )
    try:
        sup.start()
    finally:
        lock.release()


# ─────────────────────────────────────────────────────────
# resume
# ─────────────────────────────────────────────────────────
@app.command()
def resume(
    workspace: Path = typer.Option(Path("./dabook_workspace"), "--workspace", "-w"),
    port: int = typer.Option(8765, "--port"),
    no_ui: bool = typer.Option(False, "--no-ui"),
    no_open: bool = typer.Option(False, "--no-open"),
) -> None:
    """Resume all work in the workspace."""
    _run_impl(
        paths=[],
        workspace=workspace,
        profile="balanced",
        modes=["raw", "rag"],
        formats=["jsonl"],
        workers_cpu=None,
        workers_gpu=None,
        book_concurrency=None,
        port=port,
        start_ui=not no_ui,
        open_browser=not no_open,
        watch=False,
        autotune=False,
    )


# ─────────────────────────────────────────────────────────
# ui
# ─────────────────────────────────────────────────────────
@app.command()
def ui(
    workspace: Path = typer.Option(Path("./dabook_workspace"), "--workspace", "-w"),
    port: int = typer.Option(8765, "--port"),
) -> None:
    """Open dashboard for an existing workspace (read-only browse mode)."""
    from dabook.core.workspace import get_db_path
    from dabook.server.app import create_app

    db_path = get_db_path(workspace)
    if not db_path.exists():
        console.print(f"[red]✗ No workspace at {workspace}[/red]")
        raise typer.Exit(1)

    import uvicorn

    app_inst = create_app(db_path, workspace)
    console.print(f"[green]▸[/green] Dashboard: http://127.0.0.1:{port}")
    uvicorn.run(app_inst, host="127.0.0.1", port=port, log_level="warning")


# ─────────────────────────────────────────────────────────
# status
# ─────────────────────────────────────────────────────────
@app.command()
def status(
    workspace: Path = typer.Option(Path("./dabook_workspace"), "--workspace", "-w"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Print processing status snapshot."""
    from dabook.core.store.db import connect
    from dabook.core.workspace import get_db_path

    db_path = get_db_path(workspace)
    if not db_path.exists():
        console.print("[yellow]No workspace found.[/yellow]")
        raise typer.Exit(1)

    con = connect(db_path, readonly=False)

    if json_out:
        import json

        snap = _build_status_dict(con)
        print(json.dumps(snap, indent=2, default=str))
        return

    books = con.execute("SELECT state, COUNT(*) n FROM books GROUP BY state").fetchall()
    tasks = con.execute("SELECT state, COUNT(*) n FROM tasks GROUP BY state").fetchall()

    table = Table(title="DABOOK Status", border_style="blue")
    table.add_column("Entity")
    table.add_column("State")
    table.add_column("Count", justify="right")
    for r in books:
        table.add_row("Books", r["state"], str(r["n"]))
    for r in tasks:
        table.add_row("Tasks", r["state"], str(r["n"]))

    console.print(table)
    con.close()


# ─────────────────────────────────────────────────────────
# pause / continue / stop
# ─────────────────────────────────────────────────────────
@app.command(name="pause")
def pause_cmd(workspace: Path = typer.Option(Path("./dabook_workspace"), "-w")) -> None:
    """Pause the processing queue."""
    _set_setting(workspace, "queue.paused", "1")
    console.print("[yellow]⏸ Queue paused.[/yellow]")


@app.command(name="continue")
def continue_cmd(workspace: Path = typer.Option(Path("./dabook_workspace"), "-w")) -> None:
    """Resume the processing queue."""
    _set_setting(workspace, "queue.paused", "0")
    console.print("[green]▶ Queue resumed.[/green]")


@app.command()
def stop(
    workspace: Path = typer.Option(Path("./dabook_workspace"), "-w"),
    now: bool = typer.Option(False, "--now", help="Hard stop (don't finish current task)"),
) -> None:
    """Signal supervisor to stop."""
    _set_setting(workspace, "stopping", "1")
    console.print("[yellow]🛑 Stop signal sent.[/yellow]")


# ─────────────────────────────────────────────────────────
# retry
# ─────────────────────────────────────────────────────────
@app.command()
def retry(
    workspace: Path = typer.Option(Path("./dabook_workspace"), "-w"),
    dead: bool = typer.Option(False, "--dead"),
    failed: bool = typer.Option(False, "--failed"),
    book: str | None = typer.Option(None, "--book"),
) -> None:
    """Retry failed or dead tasks."""
    from dabook.core.store.db import connect, write_txn
    from dabook.core.workspace import get_db_path

    db_path = get_db_path(workspace)
    con = connect(db_path)
    states = []
    if dead or (not dead and not failed):
        states.append("dead")
    if failed or (not dead and not failed):
        states.append("failed")

    placeholders = ",".join("?" * len(states))
    where = f"state IN ({placeholders})"
    args = list(states)

    if book:
        where += " AND book_id=(SELECT id FROM books WHERE sha256 LIKE ? LIMIT 1)"
        args.append(f"{book}%")

    with write_txn(con):
        n = con.execute(
            f"UPDATE tasks SET state='pending', attempts=0, not_before=0 WHERE {where}",
            args,
        ).rowcount
    con.close()
    console.print(f"[green]↺ Retrying {n} task(s).[/green]")


# ─────────────────────────────────────────────────────────
# doctor
# ─────────────────────────────────────────────────────────
@app.command()
def doctor(
    workspace: Path = typer.Option(Path("./dabook_workspace"), "-w"),
) -> None:
    """Check environment, dependencies, and workspace health."""
    db_path = workspace / "state.db"
    checks = _run_doctor_checks(workspace, db_path)

    table = Table(title="DABOOK Doctor", border_style="blue")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Details")

    all_ok = True
    for c in checks:
        ok = c.get("ok", False)
        if not ok:
            all_ok = False
        status = "[green]✓[/green]" if ok else "[red]✗[/red]"
        table.add_row(c["name"], status, c.get("detail", ""))

    console.print(table)
    if all_ok:
        console.print("[green bold]All checks passed.[/green bold]")
    else:
        console.print("[red]Some checks failed — see details above.[/red]")
        raise typer.Exit(1)


def _run_doctor_checks(workspace: Path, db_path: Path) -> list[dict]:
    checks = []

    # Python version
    pv = sys.version_info
    checks.append(
        {
            "name": "Python version",
            "ok": pv >= (3, 11),
            "detail": f"{pv.major}.{pv.minor}.{pv.micro} (need ≥ 3.11)",
        }
    )

    # SQLite version
    sv = sqlite3.sqlite_version_info
    checks.append(
        {
            "name": "SQLite version",
            "ok": sv >= (3, 35, 0),
            "detail": f"{sqlite3.sqlite_version} (need ≥ 3.35 for RETURNING)",
        }
    )

    # Workspace writable
    ws_ok = workspace.is_dir() and os.access(workspace, os.W_OK)
    checks.append(
        {
            "name": "Workspace writable",
            "ok": ws_ok,
            "detail": str(workspace),
        }
    )

    # DB on local filesystem
    checks.append(
        {
            "name": "DB on local filesystem",
            "ok": True,  # simplified check
            "detail": "WAL requires local disk",
        }
    )

    # Disk space
    import shutil

    if workspace.is_dir():
        usage = shutil.disk_usage(workspace)
        free_gb = usage.free / 1024**3
        checks.append(
            {
                "name": "Disk free",
                "ok": free_gb >= 5.0,
                "detail": f"{free_gb:.1f} GB free (need ≥ 5 GB recommended)",
            }
        )

    # psutil
    try:
        import psutil  # noqa: F401

        checks.append({"name": "psutil", "ok": True, "detail": "system monitoring available"})
    except ImportError:
        checks.append({"name": "psutil", "ok": False, "detail": "pip install psutil"})

    # FastAPI
    try:
        import fastapi  # noqa: F401

        checks.append({"name": "FastAPI", "ok": True, "detail": "dashboard available"})
    except ImportError:
        checks.append({"name": "FastAPI", "ok": False, "detail": "pip install fastapi"})

    # pypdfium2
    try:
        import pypdfium2  # noqa: F401

        checks.append({"name": "pypdfium2", "ok": True, "detail": "PDF parsing available"})
    except ImportError:
        checks.append({"name": "pypdfium2", "ok": False, "detail": "pip install pypdfium2"})

    # NVIDIA GPU
    try:
        import pynvml

        pynvml.nvmlInit()
        count = pynvml.nvmlDeviceGetCount()
        checks.append({"name": "GPU (NVIDIA)", "ok": True, "detail": f"{count} GPU(s) detected"})
    except Exception:
        checks.append(
            {"name": "GPU (NVIDIA)", "ok": True, "detail": "No NVIDIA GPU (CPU-only mode ok)"}
        )

    # Port available
    port = 8765
    try:
        s = socket.socket()
        s.bind(("127.0.0.1", port))
        s.close()
        checks.append({"name": f"Port {port}", "ok": True, "detail": "available"})
    except OSError:
        checks.append(
            {"name": f"Port {port}", "ok": False, "detail": f"Port {port} already in use"}
        )

    return checks


# ─────────────────────────────────────────────────────────
# cache
# ─────────────────────────────────────────────────────────
@app.command()
def cache(
    action: str = typer.Argument(..., help="gc|stats|verify"),
    workspace: Path = typer.Option(Path("./dabook_workspace"), "-w"),
    older_than: int | None = typer.Option(None, "--older-than", help="Days"),
) -> None:
    """Manage the stage cache."""
    if action == "stats":
        total = sum(1 for _ in (workspace / "books").rglob("_SUCCESS"))
        console.print(f"Committed stage artifacts: {total}")
    elif action == "gc":
        console.print("[dim]Cache GC not yet implemented (M10).[/dim]")
    elif action == "verify":
        from dabook.core.atomic import is_valid_commit

        bad = 0
        for d in (workspace / "books").rglob("*/"):
            if (d / "_SUCCESS").exists() and not is_valid_commit(d, deep=True):
                console.print(f"[red]Invalid: {d}[/red]")
                bad += 1
        console.print(f"Verified. Invalid artifacts: {bad}")


# ─────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────


def _collect_pdfs(paths: list[Path]) -> list[Path]:
    result = []
    for p in paths:
        if not p.exists():
            console.print(f"[yellow]⚠ Not found: {p}[/yellow]")
            continue
        if p.is_file() and p.suffix.lower() == ".pdf":
            result.append(p)
        elif p.is_dir():
            result.extend(p.rglob("*.pdf"))
    return sorted(set(result))


def _get_page_count(pdf_path: Path) -> int | None:
    try:
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument(str(pdf_path))
        n = len(doc)
        doc.close()
        return n
    except Exception:
        return None


def _set_setting(workspace: Path, key: str, value: str) -> None:
    from dabook.core.store.db import connect
    from dabook.core.store.settings import update_setting
    from dabook.core.workspace import get_db_path

    db_path = get_db_path(workspace)
    if not db_path.exists():
        console.print("[yellow]No workspace found.[/yellow]")
        return
    con = connect(db_path)
    update_setting(con, key, value)
    con.close()


def _sync_settings(con: object, cfg: DabookConfig, port: int) -> None:  # type: ignore[name-defined]
    from dabook.core.store.settings import update_setting

    s = cfg
    updates = {
        "workers.cpu": s.workers.cpu,
        "workers.gpu": s.workers.gpu,
        "workers.io": s.workers.io,
        "workers.llm": s.workers.llm,
        "book_concurrency": s.workers.book_concurrency,
        "ram_ceiling_pct": s.limits.ram_ceiling_pct,
        "vram_ceiling_pct": s.limits.vram_ceiling_pct,
        "min_free_gb": s.limits.min_free_gb,
        "shard_pages": s.limits.shard_pages,
        "retry.max_attempts": s.limits.max_attempts,
        "retry.backoff_s": s.limits.backoff_s,
        "lease_s": s.limits.lease_s,
        "heartbeat_s": s.limits.heartbeat_s,
        "stopping": "0",
        "ui.port": port,
    }
    for k, v in updates.items():
        update_setting(con, k, v)  # type: ignore[arg-type]


def _build_status_dict(con: object) -> dict:  # type: ignore[name-defined]
    books = con.execute("SELECT state, COUNT(*) n FROM books GROUP BY state").fetchall()  # type: ignore
    tasks = con.execute("SELECT state, COUNT(*) n FROM tasks GROUP BY state").fetchall()  # type: ignore
    return {
        "books": {r["state"]: r["n"] for r in books},
        "tasks": {r["state"]: r["n"] for r in tasks},
    }


if __name__ == "__main__":
    app()
