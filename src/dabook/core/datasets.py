"""
DABOOK Training Dataset Engine.

Compiles model-ready datasets for:
1. Pre-training & Continued Pretraining (CLM): Document-level Markdown chapters.
2. Supervised Fine-Tuning (SFT): Instruction-response pairs in ChatML and Alpaca formats.
3. Code Models: Multi-line code and disassembly routines with context.
4. RAG Vectors: Semantic chunks with breadcrumb metadata.
5. Consolidated master datasets: Merging entire folders / multiple books into unified files.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

# Regex patterns for boilerplate front-matter and index detection
_BOILERPLATE_PATTERNS = [
    re.compile(
        r"\b(?:all\s+rights\s+reserved|isbn(?:-1[03])?:?|cataloging-in-publication|library\s+of\s+congress)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:published\s+by\s+john\s+wiley|wiley\s+publishing|o'reilly\s+media|packt\s+publishing)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:trademarks?:|no\s+part\s+of\s+this\s+publication\s+may\s+be\s+reproduced)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:printed\s+in\s+the\s+united\s+states\s+of\s+america)\b", re.IGNORECASE),
]

_URL_OR_REF_PATTERN = re.compile(
    r"^(?:https?://|www\.|[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+|[a-zA-Z0-9_-]+\.(?:c|h|cpp|hpp|py|rb|exe|dll|so|dylib|html?|pdf|zip))\s*$",
    re.IGNORECASE,
)

_ASM_OR_CODE_KEYWORDS = re.compile(
    r"\b(?:mov|push|pop|call|ret|retn|jmp|je|jne|jz|jnz|cmp|test|lea|xor|sub|add|int\s+3|"
    r"struct|typedef|void|int|char|unsigned|signed|float|double|return|#include|#define|"
    r"DWORD|QWORD|BYTE|WORD|PVOID|HANDLE|STATUS_|NTSTATUS|BOOL)\b"
)


def estimate_tokens(text: str) -> int:
    """Fast, accurate token count estimation (~1.3 tokens per word or len // 4)."""
    words = len(text.split())
    if words == 0:
        return 0
    return max(words, int(words * 1.33))


def is_boilerplate_node(node: dict[str, Any], page_idx: int, total_pages: int) -> bool:
    """
    Detect if a node contains copyright, publisher boilerplate, or alphabetical index noise.
    """
    text = node.get("text", "").strip()
    title = (node.get("title") or "").strip().lower()
    breadcrumbs = [b.lower() for b in node.get("breadcrumbs", [])]

    # 1. Front-matter pages (first 15 pages or first 5% of document)
    front_threshold = min(15, max(5, int(total_pages * 0.05)))
    if page_idx < front_threshold:
        if any(p.search(text) for p in _BOILERPLATE_PATTERNS):
            return True
        if any(
            term in title for term in ("copyright", "disclaimer", "trademarks", "publisher", "isbn")
        ):
            return True

    # 2. Back-of-book alphabetical index
    if ("index" in title or any("index" in b for b in breadcrumbs)) and (
        title in ("index", "subject index", "author index")
        or any(b in ("index", "subject index") for b in breadcrumbs)
    ):
        return True

    # Check for index phonebook pattern in text (many lines of 'Name, 12, 34')
    if page_idx > max(20, total_pages - 30):
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        if len(lines) >= 4:
            index_like_lines = sum(
                1 for line in lines if re.search(r",\s*\d+(?:-\d+)?(?:,\s*\d+)*$", line)
            )
            if index_like_lines / len(lines) > 0.6:
                return True

    return False


def is_valid_code_block(text: str) -> bool:
    """
    Validate that a code node contains actual multi-line routines or syntax,
    filtering out single-line monospace URLs, emails, filenames, and isolated words.
    """
    cleaned = text.strip()
    if not cleaned:
        return False

    # Check for URLs even across line breaks
    if re.search(
        r"(?:https?://|www\.|(?:\.com|\.org|\.edu|\.gov|\.io|\.html?|\.pdf)(?:/|\)|$))",
        cleaned,
        re.IGNORECASE,
    ) and not _ASM_OR_CODE_KEYWORDS.search(cleaned):
        return False

    # Filter out single-line URLs, filenames, or emails
    if "\n" not in cleaned:
        if _URL_OR_REF_PATTERN.match(cleaned):
            return False
        if len(cleaned.split()) <= 2 and not _ASM_OR_CODE_KEYWORDS.search(cleaned):
            return False

    lines = [line for line in cleaned.splitlines() if line.strip()]
    if len(lines) >= 2:
        # Check that it's not just lines of URLs or emails
        return not all(re.search(r"(@|\.com|\.org|https?:)", line) for line in lines)

    # Single line: must match known opcodes or C syntax
    return bool(_ASM_OR_CODE_KEYWORDS.search(cleaned) and len(cleaned) > 10)


def detect_code_language(code_text: str) -> str:
    """Identify programming or assembly language for markdown fencing."""
    lower = code_text.lower()
    if re.search(
        r"\b(mov|push|pop|call|retn?|jmp|lea|cmp|test|xor|eax|ebx|ecx|edx|esp|ebp|esi|edi)\b", lower
    ):
        if re.search(r"\b(rax|rbx|rcx|rdx|rsp|rbp|rsi|rdi|r8|r9|r10|r11|r12|r13|r14|r15)\b", lower):
            return "x64_asm"
        return "x86_asm"
    if re.search(
        r"\b(struct|typedef|void\*?|int|char\*?|unsigned|#include|#define|NTSTATUS)\b", code_text
    ):
        return "c"
    if re.search(r"\b(def\s+\w+|import\s+\w+|class\s+\w+:)\b", code_text):
        return "python"
    return "asm"


def consolidate_workspace_datasets(workspace: Path) -> dict[str, Any]:
    """
    Consolidate all book datasets in workspace into master files:
    workspace / compiled / all_pretrain.jsonl
    workspace / compiled / all_sft.jsonl
    workspace / compiled / all_code.jsonl
    workspace / compiled / all_rag.jsonl
    workspace / compiled / all_raw.jsonl
    workspace / compiled / manifest.json
    """
    from contextlib import ExitStack

    books_dir = workspace / "books"
    compiled_dir = workspace / "compiled"
    compiled_dir.mkdir(parents=True, exist_ok=True)

    targets = {
        "pretrain": (compiled_dir / "all_pretrain.jsonl", "pretrain.jsonl"),
        "sft": (compiled_dir / "all_sft.jsonl", "sft.jsonl"),
        "code": (compiled_dir / "all_code.jsonl", "code.jsonl"),
        "rag": (compiled_dir / "all_rag.jsonl", "rag.jsonl"),
        "raw": (compiled_dir / "all_raw.jsonl", "raw.jsonl"),
    }

    counts = {key: 0 for key in targets}
    book_sources: list[str] = []

    with ExitStack() as stack:
        handles = {
            key: stack.enter_context(path.open("w", encoding="utf-8"))
            for key, (path, _) in targets.items()
        }

        if books_dir.is_dir():
            for book_dir in sorted(books_dir.iterdir()):
                if not book_dir.is_dir():
                    continue
                datasets_path = book_dir / "datasets"
                if not datasets_path.is_dir():
                    continue

                book_sources.append(book_dir.name)
                for key, (_, filename) in targets.items():
                    src_file = datasets_path / filename
                    if src_file.is_file():
                        with src_file.open(encoding="utf-8") as in_f:
                            for line in in_f:
                                line = line.strip()
                                if line:
                                    handles[key].write(line + "\n")
                                    counts[key] += 1

    manifest = {
        "compiled_at": time.time(),
        "total_books": len(book_sources),
        "book_sources": book_sources,
        "datasets": {
            key: {
                "file": targets[key][0].name,
                "path": str(targets[key][0]),
                "records": counts[key],
                "size_bytes": targets[key][0].stat().st_size if targets[key][0].is_file() else 0,
            }
            for key in targets
        },
    }

    manifest_path = compiled_dir / "manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    return manifest


def list_workspace_datasets(workspace: Path) -> list[dict[str, Any]]:
    """
    List all datasets in workspace (both workspace-wide consolidated and per-book).
    """
    results: list[dict[str, Any]] = []

    # 1. Consolidated
    compiled_dir = workspace / "compiled"
    if compiled_dir.is_dir():
        for f in sorted(compiled_dir.glob("all_*.jsonl")):
            try:
                stat = f.stat()
                # Fast line count estimation
                line_count = 0
                with f.open(encoding="utf-8", errors="ignore") as fp:
                    for _ in fp:
                        line_count += 1
                dtype = f.stem.replace("all_", "")
                results.append(
                    {
                        "id": f"consolidated_{dtype}",
                        "name": f.name,
                        "scope": "workspace",
                        "scope_label": "All Books (Consolidated)",
                        "type": dtype,
                        "rel_path": f"compiled/{f.name}",
                        "size_bytes": stat.st_size,
                        "records": line_count,
                        "modified_at": stat.st_mtime,
                    }
                )
            except Exception:
                pass

    # 2. Per-book
    books_dir = workspace / "books"
    if books_dir.is_dir():
        for book_dir in sorted(books_dir.iterdir()):
            if not book_dir.is_dir():
                continue
            datasets_path = book_dir / "datasets"
            if not datasets_path.is_dir():
                continue

            book_title = book_dir.name
            source_json = book_dir / "source.json"
            if source_json.is_file():
                try:
                    src_data = json.loads(source_json.read_text(encoding="utf-8"))
                    orig_path = src_data.get("original_path", "")
                    if orig_path:
                        book_title = Path(orig_path).stem
                except Exception:
                    pass

            for f in sorted(datasets_path.glob("*.jsonl")):
                try:
                    stat = f.stat()
                    line_count = 0
                    with f.open(encoding="utf-8", errors="ignore") as fp:
                        for _ in fp:
                            line_count += 1
                    dtype = f.stem
                    results.append(
                        {
                            "id": f"book_{book_dir.name}_{dtype}",
                            "name": f.name,
                            "scope": book_dir.name,
                            "scope_label": book_title,
                            "type": dtype,
                            "rel_path": f"books/{book_dir.name}/datasets/{f.name}",
                            "size_bytes": stat.st_size,
                            "records": line_count,
                            "modified_at": stat.st_mtime,
                        }
                    )
                except Exception:
                    pass

    return results


def read_dataset_preview(
    workspace: Path,
    rel_path: str,
    offset: int = 0,
    limit: int = 10,
) -> dict[str, Any]:
    """
    Read paginated records from a dataset file for UI inspection.
    """
    target = workspace / rel_path
    if not target.is_file():
        return {
            "error": f"File not found: {rel_path}",
            "records": [],
            "total_records": 0,
            "offset": offset,
            "limit": limit,
        }

    records: list[dict[str, Any]] = []
    total = 0

    with target.open(encoding="utf-8", errors="ignore") as f:
        for idx, line in enumerate(f):
            total += 1
            if offset <= idx < offset + limit:
                line_str = line.strip()
                if line_str:
                    try:
                        records.append(json.loads(line_str))
                    except Exception:
                        records.append({"raw_line": line_str})

    return {
        "file": rel_path,
        "filename": target.name,
        "offset": offset,
        "limit": limit,
        "total_records": total,
        "records": records,
    }
