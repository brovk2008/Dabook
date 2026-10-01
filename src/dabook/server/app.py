"""
FastAPI dashboard application (§7).

Binds to 127.0.0.1 only. Per-run auth token embedded in opened URL.
SSE stream for real-time updates; REST endpoints for control.
"""

from __future__ import annotations

import json
import secrets
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from dabook.core.store.db import connect

# Module-level token generated once per supervisor run
_API_TOKEN: str = secrets.token_urlsafe(32)


def get_token() -> str:
    return _API_TOKEN


def create_app(
    db_path: Path,
    workspace: Path,
    telemetry: Any | None = None,
    eta_engine: Any | None = None,
) -> FastAPI:
    app = FastAPI(title="DABOOK Control Room", version="0.1.0", docs_url=None, redoc_url=None)

    # Read-only DB connection for dashboard (never blocks workers)
    _ro_con = connect(
        db_path, readonly=False
    )  # SSE needs some writes for events; use rw but read carefully

    # CORS: localhost only
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:*", "http://localhost:*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ------------------------------------------------------------------ #
    # Static files
    # ------------------------------------------------------------------ #
    static_dir = Path(__file__).parent / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # ------------------------------------------------------------------ #
    # Root → serve the SPA
    # ------------------------------------------------------------------ #
    @app.get("/", response_class=HTMLResponse)
    async def root() -> HTMLResponse:
        index = static_dir / "index.html"
        if index.is_file():
            return HTMLResponse(index.read_text(encoding="utf-8"))
        return HTMLResponse("<h1>DABOOK</h1><p>Dashboard loading…</p>")

    # ------------------------------------------------------------------ #
    # SSE stream (§7.2)
    # ------------------------------------------------------------------ #
    @app.get("/api/stream")
    async def stream(request: Request) -> Any:
        from fastapi.responses import StreamingResponse

        async def gen() -> Any:
            # Initial snapshot
            snap = _build_snapshot(_ro_con, telemetry, eta_engine)
            yield _sse("snapshot", snap)

            last_event_id = 0
            while not await request.is_disconnected():
                tick = _build_tick(_ro_con, telemetry, eta_engine)
                yield _sse("tick", tick)

                # New log events
                rows = _ro_con.execute(
                    "SELECT id, ts, level, kind, msg, book_id, task_id, worker_id, data "
                    "FROM events WHERE id > ? ORDER BY id LIMIT 50",
                    (last_event_id,),
                ).fetchall()
                for r in rows:
                    last_event_id = r["id"]
                    yield _sse("log", dict(r))

                import asyncio

                await asyncio.sleep(1.0)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ------------------------------------------------------------------ #
    # Overview snapshot
    # ------------------------------------------------------------------ #
    @app.get("/api/overview")
    async def overview() -> JSONResponse:
        return JSONResponse(_build_snapshot(_ro_con, telemetry, eta_engine))

    # ------------------------------------------------------------------ #
    # Books
    # ------------------------------------------------------------------ #
    @app.get("/api/books")
    async def list_books() -> JSONResponse:
        rows = _ro_con.execute(
            "SELECT id, sha256, path, title, pages, state, priority, "
            "quality_score, added_at, started_at, finished_at, doc_type FROM books "
            "WHERE state != 'removed' ORDER BY priority DESC, added_at"
        ).fetchall()
        books = []
        for r in rows:
            b = dict(r)
            # Attach stage grid
            stages = _ro_con.execute(
                "SELECT stage, state, COUNT(*) as cnt FROM tasks "
                "WHERE book_id=? GROUP BY stage, state",
                (r["id"],),
            ).fetchall()
            b["stages"] = [dict(s) for s in stages]
            books.append(b)
        return JSONResponse(books)

    @app.get("/api/books/{book_id}")
    async def get_book(book_id: int) -> JSONResponse:
        row = _ro_con.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Book not found")
        b = dict(row)
        tasks = _ro_con.execute(
            "SELECT * FROM tasks WHERE book_id=? ORDER BY stage_order, shard_idx", (book_id,)
        ).fetchall()
        b["tasks"] = [dict(t) for t in tasks]
        return JSONResponse(b)

    @app.post("/api/books/{book_id}/{action}")
    async def book_action(book_id: int, action: str, request: Request) -> JSONResponse:
        _require_token(request)
        con = connect(db_path)
        try:
            from dabook.core.store.db import write_txn

            with write_txn(con):
                if action == "pause":
                    con.execute("UPDATE books SET paused=1 WHERE id=?", (book_id,))
                elif action == "resume":
                    con.execute("UPDATE books SET paused=0 WHERE id=?", (book_id,))
                elif action == "prioritize":
                    body = await request.json()
                    con.execute(
                        "UPDATE books SET priority=? WHERE id=?",
                        (body.get("priority", 1), book_id),
                    )
                elif action == "retry":
                    con.execute(
                        "UPDATE tasks SET state='pending', attempts=0, not_before=0 "
                        "WHERE book_id=? AND state IN ('failed','dead')",
                        (book_id,),
                    )
                    con.execute(
                        "UPDATE books SET state='queued', error=NULL WHERE id=? AND state='failed'",
                        (book_id,),
                    )
                elif action == "cancel":
                    con.execute(
                        "UPDATE tasks SET state='cancelled' WHERE book_id=? AND state='pending'",
                        (book_id,),
                    )
                    con.execute("UPDATE books SET state='removed' WHERE id=?", (book_id,))
                else:
                    raise HTTPException(400, f"Unknown action: {action}")
        finally:
            con.close()
        return JSONResponse({"ok": True})

    # ------------------------------------------------------------------ #
    # Settings
    # ------------------------------------------------------------------ #
    @app.get("/api/settings")
    async def get_settings() -> JSONResponse:
        rows = _ro_con.execute("SELECT k, v, live FROM settings ORDER BY k").fetchall()
        return JSONResponse([dict(r) for r in rows])

    @app.put("/api/settings")
    async def update_settings(request: Request) -> JSONResponse:
        _require_token(request)
        body = await request.json()
        con = connect(db_path)
        try:
            from dabook.core.store.settings import update_setting

            updated = []
            for key, value in body.items():
                update_setting(con, key, value)
                updated.append(key)
        finally:
            con.close()
        return JSONResponse({"updated": updated})

    # ------------------------------------------------------------------ #
    # Queue control
    # ------------------------------------------------------------------ #
    @app.post("/api/queue/{action}")
    async def queue_action(action: str, request: Request) -> JSONResponse:
        _require_token(request)
        con = connect(db_path)
        try:
            from dabook.core.store.settings import update_setting

            if action == "pause":
                update_setting(con, "queue.paused", "1")
            elif action == "resume":
                update_setting(con, "queue.paused", "0")
            elif action == "drain":
                update_setting(con, "stopping", "1")
            else:
                raise HTTPException(400, f"Unknown action: {action}")
        finally:
            con.close()
        return JSONResponse({"ok": True})

    # ------------------------------------------------------------------ #
    # History (for charts)
    # ------------------------------------------------------------------ #
    @app.get("/api/history")
    async def history(window: str = "1h") -> JSONResponse:
        cutoff = time.time() - {"1h": 3600, "24h": 86400, "run": 1e9}.get(window, 3600)
        rows = _ro_con.execute(
            "SELECT * FROM samples WHERE ts >= ? ORDER BY ts", (cutoff,)
        ).fetchall()
        return JSONResponse([dict(r) for r in rows])

    # ------------------------------------------------------------------ #
    # Doctor
    # ------------------------------------------------------------------ #
    @app.get("/api/doctor")
    async def doctor() -> JSONResponse:
        from dabook.cli import _run_doctor_checks

        return JSONResponse(_run_doctor_checks(workspace, db_path))

    # ------------------------------------------------------------------ #
    # Datasets (§8.4)
    # ------------------------------------------------------------------ #
    @app.get("/api/datasets")
    async def get_datasets() -> JSONResponse:
        from dabook.core.datasets import list_workspace_datasets

        return JSONResponse(list_workspace_datasets(workspace))

    @app.get("/api/datasets/preview")
    async def get_dataset_preview(file: str, offset: int = 0, limit: int = 10) -> JSONResponse:
        from dabook.core.datasets import read_dataset_preview

        # Security check: avoid directory traversal
        rel = Path(file)
        if ".." in rel.parts or rel.is_absolute():
            raise HTTPException(400, "Invalid file path")
        return JSONResponse(
            read_dataset_preview(workspace, file, offset=offset, limit=min(limit, 50))
        )

    @app.post("/api/datasets/consolidate")
    async def consolidate_datasets_endpoint(request: Request) -> JSONResponse:
        _require_token(request)
        from dabook.core.datasets import consolidate_workspace_datasets

        manifest = consolidate_workspace_datasets(workspace)
        return JSONResponse({"ok": True, "manifest": manifest})

    # ------------------------------------------------------------------ #
    # Workers (for swimlane)
    # ------------------------------------------------------------------ #
    @app.get("/api/workers")
    async def list_workers() -> JSONResponse:
        rows = _ro_con.execute("SELECT * FROM workers ORDER BY started_at").fetchall()
        return JSONResponse([dict(r) for r in rows])

    return app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def _require_token(request: Request) -> None:
    """Verify Bearer token on mutating endpoints."""
    auth = request.headers.get("Authorization", "")
    token = request.query_params.get("t", "")
    if auth == f"Bearer {_API_TOKEN}" or token == _API_TOKEN:
        return
    raise HTTPException(403, "Forbidden")


def _build_snapshot(con: Any, telemetry: Any, eta_engine: Any) -> dict[str, Any]:
    now = time.time()

    # Counts
    counts = {}
    for state in ("pending", "running", "done", "skipped", "failed", "dead"):
        counts[state] = con.execute(
            "SELECT COUNT(*) FROM tasks WHERE state=?", (state,)
        ).fetchone()[0]

    book_counts = {}
    for state in ("queued", "active", "done", "failed", "paused"):
        book_counts[state] = con.execute(
            "SELECT COUNT(*) FROM books WHERE state=?", (state,)
        ).fetchone()[0]

    # Running tasks
    running = con.execute(
        """SELECT t.id, t.stage, t.shard_idx, t.units_done, t.units_total,
                  t.substep, t.started_at, t.worker_id, t.attempts,
                  b.title, b.sha256
             FROM tasks t JOIN books b ON b.id=t.book_id
            WHERE t.state='running'"""
    ).fetchall()

    # System
    sys_info: dict[str, Any] = {}
    if telemetry:
        snap = telemetry.latest()
        if snap:
            sys_info = {
                "cpu_pct": snap.cpu_pct,
                "cpu_per_core": snap.cpu_per_core,
                "ram_used_mb": snap.ram_used_mb,
                "ram_total_mb": snap.ram_total_mb,
                "disk_free_gb": snap.disk_free_gb,
                "gpus": snap.gpus,
                "workers_busy": snap.workers_busy,
                "pages_done": snap.pages_done,
                "tasks_pending": snap.tasks_pending,
            }

    # ETA
    eta_str = "calculating…"
    if eta_engine:
        try:
            live_workers = {
                "cpu": con.execute(
                    "SELECT COUNT(*) FROM workers WHERE resource_class='cpu' AND state='busy'"
                ).fetchone()[0],
                "gpu": con.execute(
                    "SELECT COUNT(*) FROM workers WHERE resource_class='gpu' AND state='busy'"
                ).fetchone()[0],
                "io": 2,
                "llm": 0,
            }
            eta = eta_engine.estimate(con, live_workers)
            eta_str = eta.format()
        except Exception:
            pass

    return {
        "ts": now,
        "tasks": counts,
        "books": book_counts,
        "running": [dict(r) for r in running],
        "system": sys_info,
        "eta": eta_str,
    }


def _build_tick(con: Any, telemetry: Any, eta_engine: Any) -> dict[str, Any]:
    """Lightweight delta for the 1 Hz tick."""
    return _build_snapshot(con, telemetry, eta_engine)
