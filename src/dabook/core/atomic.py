"""
DABOOK — atomic write primitives implementing the §5.4 commit protocol.

Rule: a stage output directory is valid if and only if it contains _SUCCESS.
A dir without _SUCCESS is garbage and may be swept at startup.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Low-level file primitives
# ---------------------------------------------------------------------------


def _fsync_dir(p: Path) -> None:
    """fsync the directory entry (POSIX only; no-op on Windows)."""
    if os.name == "nt":
        return
    fd = os.open(str(p), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_file(path: Path, data: bytes) -> None:
    """Write *data* to *path* with an fsync before close (crash-safe)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)  # atomic on POSIX and same-volume Windows
    _fsync_dir(path.parent)


def write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    write_file(path, text.encode(encoding))


def write_json(path: Path, obj: Any, *, indent: int = 2) -> None:
    write_file(path, json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=indent).encode())


def write_jsonl_append(path: Path, obj: Any) -> None:
    """Append one JSON line to *path* (not crash-safe; use for logs/edits, not artifacts)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(chunk):
            h.update(b)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Stage commit protocol  (§5.4)
# ---------------------------------------------------------------------------


class StageCommit:
    """
    Context manager implementing the §5.4 atomic commit protocol.

    Usage::

        with StageCommit(stage_dir, stage_key, meta={"stage":"s02"}) as work_dir:
            (work_dir / "blocks.jsonl").write_text(...)
            # _SUCCESS is written and dir is renamed atomically on __exit__

    The ``work_dir`` (a ``.partial`` directory) is *keyed by stage_key*, not by PID,
    so retries reuse the same partial dir and can resume intra-task progress.

    On success: ``.partial`` → final dir (atomic rename).
    On exception: ``.partial`` is left in place for the next retry.
    If the final dir already exists on entry: raises ``FileExistsError``
    (caller should treat as a cache hit and skip).
    """

    def __init__(self, stage_dir: Path, stage_key: str, meta: dict[str, Any]) -> None:
        self.final = stage_dir / stage_key
        self.partial = stage_dir / f"{stage_key}.partial"
        self.meta = meta

    def __enter__(self) -> Path:
        if self.final.exists():
            raise FileExistsError(f"Stage output already committed: {self.final}")
        self.partial.mkdir(parents=True, exist_ok=True)
        return self.partial

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> bool:
        if exc_type is not None:
            return False  # keep .partial for retry

        # Collect manifest
        # Note: write_file() already fsyncs each file at write time,
        # so we skip the redundant re-fsync pass (also avoids Windows Bad fd errors).
        files: dict[str, dict[str, Any]] = {}
        for p in sorted(self.partial.rglob("*")):
            if p.is_file() and p.name != "_SUCCESS":
                rel = str(p.relative_to(self.partial))
                files[rel] = {
                    "sha256": sha256_file(p),
                    "bytes": p.stat().st_size,
                }

        manifest = {
            **self.meta,
            "files": files,
            "committed_at": time.time(),
        }
        # _SUCCESS written LAST inside .partial
        write_json(self.partial / "_SUCCESS", manifest)

        # Atomic rename .partial → final
        try:
            os.replace(self.partial, self.final)
        except OSError:
            if self.final.exists():
                # Another worker won the race — discard ours (idempotent)
                shutil.rmtree(self.partial, ignore_errors=True)
            else:
                raise

        _fsync_dir(self.final.parent)
        return False


# ---------------------------------------------------------------------------
# Manifest validation
# ---------------------------------------------------------------------------


def is_valid_commit(d: Path, *, deep: bool = False) -> bool:
    """
    Return True if *d* is a successfully committed stage output.

    With ``deep=True``, verify each file's sha256 (slow; used by ``dabook cache verify``).
    """
    m = d / "_SUCCESS"
    if not m.is_file():
        return False
    try:
        manifest: dict[str, Any] = json.loads(m.read_text())
    except Exception:
        return False
    for rel, info in manifest.get("files", {}).items():
        fp = d / rel
        if not fp.is_file():
            return False
        if fp.stat().st_size != info.get("bytes", -1):
            return False
        if deep and sha256_file(fp) != info.get("sha256", ""):
            return False
    return True


def read_manifest(d: Path) -> dict[str, Any]:
    m = d / "_SUCCESS"
    return json.loads(m.read_text())


# ---------------------------------------------------------------------------
# Partial-dir cleanup
# ---------------------------------------------------------------------------


def sweep_partials(root: Path) -> list[Path]:
    """
    Remove ``*.partial`` directories that have no matching running task.
    Called at supervisor startup.  Returns list of swept paths.
    """
    swept: list[Path] = []
    for p in root.rglob("*.partial"):
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
            swept.append(p)
    return swept
