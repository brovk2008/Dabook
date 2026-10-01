"""
Stable node and block ID generation.

IDs must be stable across re-runs with the same content so that the
edit log (edits.jsonl) can be re-matched after re-extraction.
"""

from __future__ import annotations

import hashlib


def block_id(book12: str, page: int, idx: int) -> str:
    """Extraction-time block ID: ``{book12}_p{page:04d}_b{idx:03d}``."""
    return f"{book12}_p{page:04d}_b{idx:03d}"


def node_id(
    book_sha: str,
    text_prefix: str,
    first_page: int,
    bbox: tuple[float, float, float, float],
    node_type: str,
    *,
    grid: int = 8,
) -> str:
    """
    Content-addressed node ID (§9.5).

    Stable across re-runs unless content truly changes.
    ``bbox`` is quantised to *grid* PDF points to absorb minor extraction jitter.
    """
    qbbox = tuple(round(v / grid) * grid for v in bbox)
    raw = "|".join(
        [
            book_sha[:12],
            text_prefix[:80].strip(),
            str(first_page),
            str(qbbox),
            node_type,
        ]
    )
    digest = hashlib.sha256(raw.encode()).hexdigest()[:8]
    return f"n_{digest}"


def chunk_id(book_slug: str, chapter: int, section: int, seq: int, text: str) -> str:
    """
    Stable RAG chunk ID (§11.2).

    ``{book_slug}_{chapter:02d}_{section:02d}_{seq:04d}`` + content-hash suffix.
    """
    content_hash = hashlib.sha256(text.encode()).hexdigest()[:6]
    return f"{book_slug}_{chapter:02d}_{section:02d}_{seq:04d}_{content_hash}"


def make_book_slug(title: str, sha256: str) -> str:
    """URL-safe slug derived from title + sha12."""
    import re

    safe = re.sub(r"[^a-z0-9]+", "_", title.lower())[:24].strip("_")
    return f"{safe}_{sha256[:6]}" if safe else sha256[:12]
