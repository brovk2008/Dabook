"""
Workspace initialisation — creates the directory tree and populates the DB.
"""

from __future__ import annotations

import time
from pathlib import Path

from dabook.config.models import DabookConfig
from dabook.core.hashing import sha256_stream
from dabook.core.store.db import connect
from dabook.core.store.migrations import migrate
from dabook.core.store.tasks import register_book


def init_workspace(cfg: DabookConfig, cwd: Path | None = None) -> Path:
    """
    Ensure the workspace exists, apply schema migrations, and return its path.
    """
    workspace = cfg.resolved_workspace(cwd)
    workspace.mkdir(parents=True, exist_ok=True)

    # Create directory skeleton
    for d in [
        "books",
        "exports",
        "models",
        "logs/workers",
        "cache/llm",
        "inbox",
    ]:
        (workspace / d).mkdir(parents=True, exist_ok=True)

    # Migrations (idempotent)
    db_path = workspace / "state.db"
    migrate(db_path)

    return workspace


def get_db_path(workspace: Path) -> Path:
    return workspace / "state.db"


def register_pdf(
    workspace: Path,
    pdf_path: Path,
    priority: int = 0,
    profile: str | None = None,
) -> tuple[int, str]:
    """
    Hash the PDF and register it in the DB.

    Returns ``(book_id, sha256)``.
    """
    sha = sha256_stream(pdf_path)
    size = pdf_path.stat().st_size

    # Create book directory
    book_dir = workspace / "books" / sha[:12]
    book_dir.mkdir(parents=True, exist_ok=True)
    (book_dir / "stages").mkdir(exist_ok=True)
    (book_dir / "shards").mkdir(exist_ok=True)

    # Write source.json
    from dabook.core.atomic import write_json

    write_json(
        book_dir / "source.json",
        {
            "sha256": sha,
            "original_path": str(pdf_path),
            "size_bytes": size,
            "added_at": time.time(),
        },
    )

    con = connect(get_db_path(workspace))
    try:
        book_id = register_book(con, sha, str(pdf_path), size, priority=priority, profile=profile)
    finally:
        con.close()

    return book_id, sha
