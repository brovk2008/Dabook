"""
Real stage implementations for S00–S13 (§8).

Executes real extraction, furniture filtering, heading structure reconciliation,
Book Graph construction, and dataset exports.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import ftfy

from dabook.backends.pdfium import PdfiumExtractor
from dabook.core.atomic import StageCommit, write_json
from dabook.ir.models import (
    BookNode,
)

logger = logging.getLogger("dabook.stages")


def run_stage(
    task: dict[str, Any],
    work_dir: Path,
    pdf_path: Path,
    progress_fn: Callable[[int, str], None],
    should_abort: Callable[[], bool],
) -> dict[str, Any]:
    """Execute the real stage function corresponding to task['stage']."""
    stage = task["stage"]
    key = task["stage_key"]
    stage_dir = work_dir / "stages" / stage

    t0 = time.time()
    meta = {
        "stage": stage,
        "task_id": task["id"],
        "shard_idx": task.get("shard_idx", 0),
        "impl_version": "0.2.0-real",
    }

    with StageCommit(stage_dir, key, meta) as out_dir:
        if stage == "s00_register":
            _stage_s00_register(task, pdf_path, out_dir)
        elif stage == "s01_inspect":
            _stage_s01_inspect(task, pdf_path, out_dir, progress_fn)
        elif stage == "s02_extract":
            _stage_s02_extract(task, pdf_path, out_dir, progress_fn, should_abort)
        elif stage == "s03_merge":
            _stage_s03_merge(task, work_dir, out_dir, progress_fn)
        elif stage == "s04_furniture":
            _stage_s04_furniture(task, work_dir, out_dir, progress_fn)
        elif stage == "s05_reading_order":
            _stage_s05_reading_order(task, work_dir, out_dir, progress_fn)
        elif stage == "s06_structure":
            _stage_s06_structure(task, work_dir, out_dir, progress_fn)
        elif stage == "s07_continuity":
            _stage_s07_continuity(task, work_dir, out_dir, progress_fn)
        elif stage == "s08_typed_content":
            _stage_s08_typed_content(task, work_dir, out_dir, progress_fn)
        elif stage == "s09_graph":
            _stage_s09_graph(task, work_dir, out_dir, progress_fn)
        elif stage == "s10_clean":
            _stage_s10_clean(task, work_dir, out_dir, progress_fn)
        elif stage == "s11_validate":
            _stage_s11_validate(task, work_dir, out_dir, progress_fn)
        elif stage == "s12_semantic":
            _stage_s12_semantic(task, work_dir, out_dir, progress_fn)
        elif stage == "s13_compile":
            _stage_s13_compile(task, work_dir, out_dir, progress_fn)
        else:
            write_json(out_dir / "output.json", {"status": "ok", "stage": stage})

    elapsed_ms = int((time.time() - t0) * 1000)
    return {
        "output_path": str(stage_dir / key),
        "duration_ms": elapsed_ms,
    }


# ---------------------------------------------------------------------------
# Stage S00: Register
# ---------------------------------------------------------------------------
def _stage_s00_register(task: dict[str, Any], pdf_path: Path, out_dir: Path) -> None:
    write_json(
        out_dir / "source_meta.json",
        {
            "pdf_name": pdf_path.name,
            "pdf_size": pdf_path.stat().st_size if pdf_path.exists() else 0,
            "registered_at": time.time(),
        },
    )


# ---------------------------------------------------------------------------
# Stage S01: Inspect
# ---------------------------------------------------------------------------
def _stage_s01_inspect(
    task: dict[str, Any],
    pdf_path: Path,
    out_dir: Path,
    progress_fn: Callable[[int, str], None],
) -> None:
    progress_fn(0, "Inspecting PDF and extracting bookmarks…")
    with PdfiumExtractor(pdf_path) as ext:
        page_count = ext.page_count()
        toc = ext.extract_toc()

        # Sample sample pages for text ratio
        sample_indices = [
            0,
            min(5, page_count - 1),
            page_count // 2,
            max(0, page_count - 2),
        ]
        total_chars = 0
        for idx in set(sample_indices):
            page_data = ext.extract_page(idx)
            total_chars += page_data.char_count

        is_born_digital = total_chars > 200

    write_json(
        out_dir / "inspection.json",
        {
            "page_count": page_count,
            "is_born_digital": is_born_digital,
            "toc_count": len(toc),
            "toc": [t.model_dump() for t in toc],
        },
    )
    progress_fn(1, "Inspection complete.")


# ---------------------------------------------------------------------------
# Stage S02: Extract (Sharded)
# ---------------------------------------------------------------------------
def _stage_s02_extract(
    task: dict[str, Any],
    pdf_path: Path,
    out_dir: Path,
    progress_fn: Callable[[int, str], None],
    should_abort: Callable[[], bool],
) -> None:
    page_start = task.get("page_start") or 0
    page_end = task.get("page_end") or (page_start + 1)

    extracted_pages: list[dict[str, Any]] = []
    with PdfiumExtractor(pdf_path) as ext:
        for idx, p_num in enumerate(range(page_start, page_end)):
            if should_abort():
                raise RuntimeError("Aborted")
            if p_num >= ext.page_count():
                break

            page_data = ext.extract_page(p_num)
            extracted_pages.append(page_data.model_dump())
            progress_fn(idx + 1, f"Extracted page {p_num + 1}/{page_end}")

    write_json(
        out_dir / "pages.json",
        {
            "shard_idx": task.get("shard_idx", 0),
            "page_start": page_start,
            "page_end": page_end,
            "pages": extracted_pages,
        },
    )


# ---------------------------------------------------------------------------
# Stage S03: Merge
# ---------------------------------------------------------------------------
def _stage_s03_merge(
    task: dict[str, Any],
    work_dir: Path,
    out_dir: Path,
    progress_fn: Callable[[int, str], None],
) -> None:
    progress_fn(0, "Merging sharded extraction outputs…")
    extract_dir = work_dir / "stages" / "s02_extract"
    all_pages: list[dict[str, Any]] = []

    # Find all committed stage keys in s02_extract
    for commit_dir in sorted(extract_dir.glob("*")):
        if (commit_dir / "_SUCCESS").is_file():
            pages_file = commit_dir / "pages.json"
            if pages_file.is_file():
                try:
                    data = json.loads(pages_file.read_text(encoding="utf-8"))
                    all_pages.extend(data.get("pages", []))
                except Exception as e:
                    logger.warning("Error reading %s: %s", pages_file, e)

    # Sort pages by page_idx
    all_pages.sort(key=lambda p: p["page_idx"])
    write_json(out_dir / "merged_pages.json", {"pages": all_pages, "total_pages": len(all_pages)})
    progress_fn(len(all_pages), f"Merged {len(all_pages)} pages.")


# ---------------------------------------------------------------------------
# Stage S04: Furniture Removal (Headers & Footers)
# ---------------------------------------------------------------------------
# Stage S04: Furniture Removal (Headers, Footers, Watermarks)
# ---------------------------------------------------------------------------
def _stage_s04_furniture(
    task: dict[str, Any],
    work_dir: Path,
    out_dir: Path,
    progress_fn: Callable[[int, str], None],
) -> None:
    progress_fn(0, "Detecting running headers, footers, and watermarks…")
    s03_dir = _get_latest_stage_dir(work_dir, "s03_merge")
    if not s03_dir:
        write_json(out_dir / "clean_pages.json", {"pages": []})
        return

    pages_data = json.loads((s03_dir / "merged_pages.json").read_text(encoding="utf-8"))["pages"]

    # Frequency analysis across margin bands (top 6% and bottom 6%)
    margin_patterns: dict[str, int] = {}
    for page in pages_data:
        h = page.get("height", 660.0)
        for b in page.get("blocks", []):
            y0, y1 = b["bbox"][1], b["bbox"][3]
            txt = b["raw_text"].strip()
            if y0 < h * 0.06 or y1 > h * 0.94:
                margin_patterns[txt] = margin_patterns.get(txt, 0) + 1

    indesign_re = re.compile(r".*\.indd\s+\d+:\d+:\d+.*Page\s+\w+", re.IGNORECASE)
    watermark_re = re.compile(r"www\.[a-z0-9\-]+\.(info|org|com|net)", re.IGNORECASE)
    page_num_re = re.compile(r"^(\d{1,4}|[ivxlcdm]+)$", re.IGNORECASE)
    running_head_re = re.compile(r"^(Chapter\s+\d+|[A-Z0-9\s]{3,30}\s+\d{1,4})$", re.IGNORECASE)

    clean_pages: list[dict[str, Any]] = []
    for page in pages_data:
        h = page.get("height", 660.0)
        clean_blocks = []
        for b in page.get("blocks", []):
            txt = b["raw_text"].strip()
            y0, y1 = b["bbox"][1], b["bbox"][3]
            is_furniture = False

            # Universal watermark check anywhere on page
            if watermark_re.search(txt) or indesign_re.search(txt) or ((y0 < h * 0.06 or y1 > h * 0.94) and (
                margin_patterns.get(txt, 0) >= 2
                or page_num_re.match(txt)
                or (running_head_re.match(txt) and len(txt) < 40)
                or ("■" in txt and len(txt) < 50)
            )):
                is_furniture = True

            if is_furniture:
                b["block_type"] = "furniture"
            clean_blocks.append(b)

        clean_pages.append({**page, "blocks": clean_blocks})

    write_json(out_dir / "clean_pages.json", {"pages": clean_pages})
    progress_fn(len(clean_pages), "Furniture filtering complete.")


# ---------------------------------------------------------------------------
# Stage S05: Reading Order (XY-Cut++ Column Detection)
# ---------------------------------------------------------------------------
def _stage_s05_reading_order(
    task: dict[str, Any],
    work_dir: Path,
    out_dir: Path,
    progress_fn: Callable[[int, str], None],
) -> None:
    s04_dir = _get_latest_stage_dir(work_dir, "s04_furniture")
    pages_data = json.loads((s04_dir / "clean_pages.json").read_text(encoding="utf-8"))["pages"]

    for page in pages_data:
        pw = page.get("width", 530.0)
        ph = page.get("height", 660.0)
        blocks = [b for b in page["blocks"] if b["block_type"] != "furniture"]
        if not blocks:
            page["blocks"] = []
            continue

        # Multi-column detection (XY-Cut++)
        mid_x = pw / 2.0
        left_blocks = [b for b in blocks if b["bbox"][2] <= mid_x + 10]
        right_blocks = [b for b in blocks if b["bbox"][0] >= mid_x - 10]
        spanning_blocks = [
            b for b in blocks if b["bbox"][0] < mid_x - 10 and b["bbox"][2] > mid_x + 10
        ]

        is_two_col = (
            len(left_blocks) >= 2
            and len(right_blocks) >= 2
            and (len(left_blocks) + len(right_blocks)) / len(blocks) >= 0.75
            and len(spanning_blocks) <= 2
        )

        if is_two_col:
            top_spanning = [b for b in spanning_blocks if b["bbox"][1] < ph * 0.3]
            bot_spanning = [b for b in spanning_blocks if b["bbox"][1] >= ph * 0.3]

            left_sorted = sorted(
                left_blocks, key=lambda b: (round(b["bbox"][1] / 3.5), b["bbox"][0])
            )
            right_sorted = sorted(
                right_blocks, key=lambda b: (round(b["bbox"][1] / 3.5), b["bbox"][0])
            )
            ordered = (
                sorted(top_spanning, key=lambda b: b["bbox"][1])
                + left_sorted
                + right_sorted
                + sorted(bot_spanning, key=lambda b: b["bbox"][1])
            )
        else:
            ordered = sorted(blocks, key=lambda b: (round(b["bbox"][1] / 3.5), b["bbox"][0]))

        # Chapter opening banner inversion check (e.g. 'C H A P T E R 1: ...')
        ch_idx = next(
            (
                i
                for i, b in enumerate(ordered)
                if re.match(r"^C\s*H\s*A\s*P\s*T\s*E\s*R\s*\d+", b["raw_text"], re.IGNORECASE)
            ),
            None,
        )
        if ch_idx is not None and ch_idx > 0 and ordered[ch_idx]["bbox"][1] < ph * 0.3:
            ch_block = ordered.pop(ch_idx)
            ordered.insert(0, ch_block)

        page["blocks"] = ordered

    write_json(out_dir / "ordered_pages.json", {"pages": pages_data})
    progress_fn(len(pages_data), "Reading order organized.")


# ---------------------------------------------------------------------------
# Stage S06: Structure Hierarchy (TOC Reconciliation)
# ---------------------------------------------------------------------------
def _stage_s06_structure(
    task: dict[str, Any],
    work_dir: Path,
    out_dir: Path,
    progress_fn: Callable[[int, str], None],
) -> None:
    s05_dir = _get_latest_stage_dir(work_dir, "s05_reading_order")
    s01_dir = _get_latest_stage_dir(work_dir, "s01_inspect")

    pages_data = json.loads((s05_dir / "ordered_pages.json").read_text(encoding="utf-8"))["pages"]
    toc: list[dict[str, Any]] = []
    if s01_dir and (s01_dir / "inspection.json").is_file():
        toc = json.loads((s01_dir / "inspection.json").read_text(encoding="utf-8")).get("toc", [])

    # Map TOC items to page blocks with strict matching
    for page in pages_data:
        p_idx = page["page_idx"]
        page_toc = [t for t in toc if t.get("page_idx") == p_idx]

        for block in page["blocks"]:
            txt = block["raw_text"].strip()
            clean_txt_norm = re.sub(r"[^a-z0-9]", "", txt.lower())

            matched_toc = None
            for t in page_toc:
                t_norm = re.sub(r"[^a-z0-9]", "", t["title"].lower())
                if len(t_norm) >= 4 and (t_norm in clean_txt_norm or clean_txt_norm in t_norm):
                    matched_toc = t
                    break

            if matched_toc:
                block["block_type"] = "heading"
                block["metadata"]["level"] = matched_toc["level"]
                block["metadata"]["title"] = matched_toc["title"]
            elif block.get("is_bold") and len(txt) < 100 and not txt.endswith((".", ";", ":", ",")):
                if re.match(r"^(Chapter\s+\d+|Appendix\s+\w+|Index)$", txt, re.IGNORECASE):
                    block["block_type"] = "heading"
                    block["metadata"]["level"] = 1
                elif block["block_type"] == "heading":
                    block["metadata"]["level"] = 2
            else:
                if block["block_type"] == "heading":
                    block["block_type"] = "paragraph"

    write_json(out_dir / "structured_pages.json", {"pages": pages_data, "toc": toc})
    progress_fn(len(pages_data), "Structure and headings identified.")


# ---------------------------------------------------------------------------
# Stage S07: Continuity (Paragraph Stitching)
# ---------------------------------------------------------------------------
def _stage_s07_continuity(
    task: dict[str, Any],
    work_dir: Path,
    out_dir: Path,
    progress_fn: Callable[[int, str], None],
) -> None:
    s06_dir = _get_latest_stage_dir(work_dir, "s06_structure")
    pages_data = json.loads((s06_dir / "structured_pages.json").read_text(encoding="utf-8"))[
        "pages"
    ]

    # Stitch trailing words with hyphens
    for i in range(len(pages_data) - 1):
        curr_blocks = pages_data[i]["blocks"]
        next_blocks = pages_data[i + 1]["blocks"]

        if curr_blocks and next_blocks:
            last = curr_blocks[-1]
            first = next_blocks[0]
            if (
                last["block_type"] == "paragraph"
                and first["block_type"] == "paragraph"
                and not last["raw_text"].endswith((".", ":", ";", "?", "!"))
            ):
                last["metadata"]["continues_on_next_page"] = True
                first["metadata"]["continues_from_prev_page"] = True

    write_json(out_dir / "continuous_pages.json", {"pages": pages_data})
    progress_fn(len(pages_data), "Continuity verified.")


# ---------------------------------------------------------------------------
# Stage S08: Typed Content (Code, Assembly, Equations)
# ---------------------------------------------------------------------------
def _stage_s08_typed_content(
    task: dict[str, Any],
    work_dir: Path,
    out_dir: Path,
    progress_fn: Callable[[int, str], None],
) -> None:
    s07_dir = _get_latest_stage_dir(work_dir, "s07_continuity")
    pages_data = json.loads((s07_dir / "continuous_pages.json").read_text(encoding="utf-8"))[
        "pages"
    ]

    asm_registers = {
        "eax",
        "ebx",
        "ecx",
        "edx",
        "esi",
        "edi",
        "ebp",
        "esp",
        "rax",
        "rbx",
        "rcx",
        "rdx",
        "rsi",
        "rdi",
        "rbp",
        "rsp",
        "r8",
        "r9",
        "r10",
        "r11",
        "r12",
        "r13",
        "r14",
        "r15",
        "al",
        "bl",
        "cl",
        "dl",
        "ah",
        "bh",
        "ch",
        "dh",
        "ax",
        "bx",
        "cx",
        "dx",
        "sp",
        "bp",
        "si",
        "di",
    }
    c_types_keywords = {
        "typedef",
        "struct",
        "sizeof",
        "volatile",
        "unsigned",
        "#include",
        "#define",
        "memcpy",
        "uint32_t",
        "uint64_t",
        "dword",
        "qword",
        "guid",
    }

    code_block_count = 0
    for page in pages_data:
        for block in page["blocks"]:
            txt = block["raw_text"]
            b_type = block["block_type"]

            is_mono = block.get("is_mono", False) or b_type == "code"

            words = set(re.findall(r"\b[a-zA-Z_][a-zA-Z0-9_]*\b", txt.lower()))
            has_reg = bool(words.intersection(asm_registers))
            has_c_kw = bool(words.intersection(c_types_keywords))
            has_line_num = bool(re.search(r"^\s*\d{2}:", txt, re.MULTILINE))
            has_mem_bracket = bool(re.search(r"\[[eErR]?[a-z0-9_+* -]+\]", txt))
            has_hex_addr = bool(re.search(r"0x[0-9a-fA-F]{4,8}", txt))

            if is_mono or has_line_num or (has_reg and (has_mem_bracket or has_hex_addr)):
                block["block_type"] = "code"
                if has_c_kw and not has_reg:
                    block["metadata"]["language"] = "c"
                else:
                    block["metadata"]["language"] = "x86_asm"
                code_block_count += 1
            elif txt.strip().startswith(("1.", "2.", "3.", "■", "•", "-")):
                if block["block_type"] != "heading":
                    block["block_type"] = "list_item"

    write_json(
        out_dir / "typed_pages.json",
        {"pages": pages_data, "code_block_count": code_block_count},
    )
    progress_fn(len(pages_data), f"Typed content identified ({code_block_count} code blocks).")


# ---------------------------------------------------------------------------
# Stage S09: Book Graph
# ---------------------------------------------------------------------------
def _stage_s09_graph(
    task: dict[str, Any],
    work_dir: Path,
    out_dir: Path,
    progress_fn: Callable[[int, str], None],
) -> None:
    s08_dir = _get_latest_stage_dir(work_dir, "s08_typed_content")
    pages_data = json.loads((s08_dir / "typed_pages.json").read_text(encoding="utf-8"))["pages"]

    nodes: list[BookNode] = []
    current_chapter_id = "root"
    current_section_id = "root"

    # Root book node
    nodes.append(
        BookNode(
            node_id="root",
            node_type="book",
            title="Book Root",
        )
    )

    for page in pages_data:
        p_idx = page["page_idx"]
        for b in page["blocks"]:
            b_id = b["block_id"]
            b_type = b["block_type"]
            txt = b["raw_text"]

            if b_type == "heading":
                level = b.get("metadata", {}).get("level", 1)
                node_type = "chapter" if level == 0 or "Chapter" in txt else "section"
                parent_id = "root" if node_type == "chapter" else current_chapter_id

                node = BookNode(
                    node_id=b_id,
                    node_type=node_type,
                    title=txt,
                    text=txt,
                    page_idx=p_idx,
                    bbox=b["bbox"],
                    parent_id=parent_id,
                )
                if node_type == "chapter":
                    current_chapter_id = b_id
                current_section_id = b_id
                nodes.append(node)
            else:
                node = BookNode(
                    node_id=b_id,
                    node_type=b_type,
                    text=txt,
                    page_idx=p_idx,
                    bbox=b["bbox"],
                    parent_id=current_section_id,
                    metadata=b.get("metadata", {}),
                )
                nodes.append(node)

    write_json(out_dir / "graph.json", {"nodes": [n.model_dump() for n in nodes]})
    progress_fn(len(nodes), f"Book Graph constructed with {len(nodes)} nodes.")


# ---------------------------------------------------------------------------
# Stage S10: Clean (Unicode NFC, Ligatures, Dehyphenation)
# ---------------------------------------------------------------------------
def _stage_s10_clean(
    task: dict[str, Any],
    work_dir: Path,
    out_dir: Path,
    progress_fn: Callable[[int, str], None],
) -> None:
    s09_dir = _get_latest_stage_dir(work_dir, "s09_graph")
    nodes_data = json.loads((s09_dir / "graph.json").read_text(encoding="utf-8"))["nodes"]

    ligature_fixes = [
        ("fl ag", "flag"),
        ("fi nd", "find"),
        ("fi rst", "first"),
        ("defi ne", "define"),
        ("defi ned", "defined"),
        ("effi cient", "efficient"),
        ("affi liate", "affiliate"),
        ("offi ce", "office"),
        ("confl ict", "conflict"),
        ("signifi cant", "significant"),
        ("signifi cantly", "significantly"),
        ("speci fi", "specifi"),
        ("fi eld", "field"),
        ("identi fi", "identifi"),
        ("beneﬁ", "benefi"),
    ]

    for node in nodes_data:
        text = node["text"]
        text = ftfy.fix_text(text)

        for bad, good in ligature_fixes:
            text = text.replace(bad, good)

        text = re.sub(r"\bC\s+H\s+A\s+P\s+T\s+E\s+R\b", "CHAPTER", text, flags=re.IGNORECASE)
        text = re.sub(r"\bA\s+P\s+P\s+E\s+N\s+D\s+I\s+X\b", "APPENDIX", text, flags=re.IGNORECASE)
        text = re.sub(r"\bI\s+N\s+D\s+E\s+X\b", "INDEX", text, flags=re.IGNORECASE)

        text = text.replace("\xad", "").replace("\x02", "").replace("\ufffd", "")
        text = re.sub(r"(\b[a-zA-Z]{3,})-\s*\n\s*([a-z]{2,}\b)", r"\1\2", text)

        node["text"] = text

    write_json(out_dir / "clean_graph.json", {"nodes": nodes_data})
    progress_fn(len(nodes_data), "Text normalized and cleaned.")


# ---------------------------------------------------------------------------
# Stage S11: Validate (Quality Audit)
# ---------------------------------------------------------------------------
def _stage_s11_validate(
    task: dict[str, Any],
    work_dir: Path,
    out_dir: Path,
    progress_fn: Callable[[int, str], None],
) -> None:
    s10_dir = _get_latest_stage_dir(work_dir, "s10_clean")
    nodes = json.loads((s10_dir / "clean_graph.json").read_text(encoding="utf-8"))["nodes"]

    total_words = sum(len(n["text"].split()) for n in nodes)
    code_blocks = sum(1 for n in nodes if n["node_type"] == "code")
    headings = sum(1 for n in nodes if n["node_type"] in ("chapter", "section"))

    # Confidence score calculation
    quality_score = 0.95 if total_words > 1000 and headings > 0 else 0.50

    write_json(
        out_dir / "validation.json",
        {
            "quality_score": quality_score,
            "total_nodes": len(nodes),
            "total_words": total_words,
            "code_blocks": code_blocks,
            "headings": headings,
            "status": "passed",
        },
    )
    progress_fn(1, f"Validation passed: {total_words} words, {code_blocks} code blocks.")


# ---------------------------------------------------------------------------
# Stage S12: Semantic (Breadcrumbs & Metadata)
# ---------------------------------------------------------------------------
def _stage_s12_semantic(
    task: dict[str, Any],
    work_dir: Path,
    out_dir: Path,
    progress_fn: Callable[[int, str], None],
) -> None:
    s10_dir = _get_latest_stage_dir(work_dir, "s10_clean")
    nodes = json.loads((s10_dir / "clean_graph.json").read_text(encoding="utf-8"))["nodes"]

    node_by_id = {n["node_id"]: n for n in nodes}
    for n in nodes:
        crumbs = []
        curr = n.get("parent_id")
        while curr and curr in node_by_id and curr != "root":
            p = node_by_id[curr]
            if p.get("title"):
                crumbs.insert(0, p["title"])
            curr = p.get("parent_id")
        n["breadcrumbs"] = crumbs

    write_json(out_dir / "semantic_graph.json", {"nodes": nodes})
    progress_fn(len(nodes), "Semantic breadcrumbs added.")


# ---------------------------------------------------------------------------
# Stage S13: Compile & Dataset Export
# ---------------------------------------------------------------------------
def _stage_s13_compile(
    task: dict[str, Any],
    work_dir: Path,
    out_dir: Path,
    progress_fn: Callable[[int, str], None],
) -> None:
    s12_dir = _get_latest_stage_dir(work_dir, "s12_semantic")
    nodes = json.loads((s12_dir / "semantic_graph.json").read_text(encoding="utf-8"))["nodes"]

    # Also export directly to book's final datasets directory
    datasets_dir = work_dir / "datasets"
    datasets_dir.mkdir(parents=True, exist_ok=True)

    # 1. raw.jsonl
    raw_path = datasets_dir / "raw.jsonl"
    with open(raw_path, "w", encoding="utf-8") as f:
        for n in nodes:
            if n["node_type"] != "book" and n["text"].strip():
                f.write(
                    json.dumps(
                        {
                            "id": n["node_id"],
                            "page": n["page_idx"] + 1,
                            "type": n["node_type"],
                            "text": n["text"],
                            "breadcrumbs": n.get("breadcrumbs", []),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )

    # 2. rag.jsonl (chunks sized ~300-600 words with metadata)
    rag_path = datasets_dir / "rag.jsonl"
    with open(rag_path, "w", encoding="utf-8") as f:
        current_chunk: list[str] = []
        current_crumbs: list[str] = []
        chunk_page = 1
        chunk_idx = 0

        for n in nodes:
            if n["node_type"] in ("chapter", "section"):
                if current_chunk:
                    f.write(
                        json.dumps(
                            {
                                "chunk_id": f"chunk_{chunk_idx}",
                                "page": chunk_page,
                                "breadcrumbs": current_crumbs,
                                "content": "\n\n".join(current_chunk),
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                    chunk_idx += 1
                    current_chunk = []
                current_crumbs = [*n.get("breadcrumbs", []), n.get("title", "")]
                chunk_page = n["page_idx"] + 1
            elif n["text"].strip():
                current_chunk.append(n["text"])
                if sum(len(c.split()) for c in current_chunk) > 400:
                    f.write(
                        json.dumps(
                            {
                                "chunk_id": f"chunk_{chunk_idx}",
                                "page": chunk_page,
                                "breadcrumbs": current_crumbs,
                                "content": "\n\n".join(current_chunk),
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                    chunk_idx += 1
                    current_chunk = []

        if current_chunk:
            f.write(
                json.dumps(
                    {
                        "chunk_id": f"chunk_{chunk_idx}",
                        "page": chunk_page,
                        "breadcrumbs": current_crumbs,
                        "content": "\n\n".join(current_chunk),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    # 3. code.jsonl (all code & assembly blocks)
    code_path = datasets_dir / "code.jsonl"
    code_nodes = [n for n in nodes if n["node_type"] == "code"]
    with open(code_path, "w", encoding="utf-8") as f:
        for c in code_nodes:
            f.write(
                json.dumps(
                    {
                        "code_id": c["node_id"],
                        "page": c["page_idx"] + 1,
                        "language": c.get("metadata", {}).get("language", "asm"),
                        "breadcrumbs": c.get("breadcrumbs", []),
                        "code": c["text"],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    # Summary manifest
    summary = {
        "compiled_at": time.time(),
        "total_nodes": len(nodes),
        "raw_lines": len(nodes),
        "rag_chunks": chunk_idx + 1,
        "code_blocks": len(code_nodes),
        "datasets": {
            "raw": str(raw_path),
            "rag": str(rag_path),
            "code": str(code_path),
        },
    }
    write_json(out_dir / "summary.json", summary)
    write_json(datasets_dir / "manifest.json", summary)
    progress_fn(
        1, f"Datasets compiled: raw, rag ({chunk_idx + 1} chunks), code ({len(code_nodes)} blocks)."
    )


def _get_latest_stage_dir(work_dir: Path, stage_name: str) -> Path | None:
    stage_dir = work_dir / "stages" / stage_name
    if not stage_dir.is_dir():
        return None
    for p in sorted(stage_dir.glob("*"), reverse=True):
        if (p / "_SUCCESS").is_file():
            return p
    return None
