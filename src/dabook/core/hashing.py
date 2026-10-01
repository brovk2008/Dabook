"""
Content-addressed hashing utilities.

Stage keys are derived by hashing the combination of:
  - stage name + impl version
  - output-affecting parameters (canonical JSON, sorted keys)
  - sorted input artifact sha256s

Changing worker counts, shard size, or UI settings does NOT affect stage_key.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def sha256_stream(path: Path, chunk: int = 1 << 20) -> str:
    """Stream-hash a file without loading it entirely into memory."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(chunk):
            h.update(b)
    return h.hexdigest()


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(obj: Any) -> str:
    """Stable, sorted JSON serialisation for use in cache keys."""
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def stage_key(
    stage_name: str,
    impl_version: str,
    output_affecting_params: dict[str, Any],
    input_sha256s: list[str],
) -> str:
    """
    Compute a 16-char hex stage cache key (§4.4).

    Only output-affecting params (declared in ``Stage.affects_output``) are included.
    Runtime params (worker counts, shard size, port, …) must NOT appear here.
    """
    parts = [
        stage_name,
        impl_version,
        canonical_json(output_affecting_params),
        *sorted(input_sha256s),
    ]
    digest = hashlib.sha256("|".join(parts).encode()).hexdigest()
    return digest[:16]


def book_dir_name(sha256: str) -> str:
    """First 12 chars of a book's sha256 — used as its workspace directory name."""
    return sha256[:12]
