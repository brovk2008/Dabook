"""
Unit tests for DABOOK training dataset engine and workspace consolidation.
"""

from __future__ import annotations

import json
from pathlib import Path

from dabook.core.datasets import (
    consolidate_workspace_datasets,
    detect_code_language,
    estimate_tokens,
    is_boilerplate_node,
    is_valid_code_block,
    list_workspace_datasets,
    read_dataset_preview,
)


def test_token_estimation() -> None:
    text = "Reverse engineering is the act of dissecting a binary program."
    tokens = estimate_tokens(text)
    assert tokens > 0
    assert tokens >= len(text.split())


def test_boilerplate_detection() -> None:
    # Front-matter copyright block
    node_copyright = {
        "title": "Copyright Notice",
        "text": "Published by John Wiley & Sons, Inc.\nAll rights reserved. ISBN 978-1-118-78731-1",
        "breadcrumbs": [],
    }
    assert is_boilerplate_node(node_copyright, page_idx=4, total_pages=380) is True

    # Real technical section on page 40
    node_tech = {
        "title": "x86 Architecture Basics",
        "text": "The x86 architecture is little-endian and supports multiple addressing modes.",
        "breadcrumbs": ["Chapter 1"],
    }
    assert is_boilerplate_node(node_tech, page_idx=40, total_pages=380) is False

    # Back-of-book alphabetical index
    node_index = {
        "title": "Index",
        "text": "AAA instruction, 12, 14\nAAD instruction, 15\nAAM instruction, 16\n",
        "breadcrumbs": ["Index"],
    }
    assert is_boilerplate_node(node_index, page_idx=370, total_pages=380) is True


def test_code_validation_and_language_detection() -> None:
    # Valid assembly routine
    valid_asm = "push ebp\nmov ebp, esp\nsub esp, 0x10\nmov eax, [ebp+8]\nret"
    assert is_valid_code_block(valid_asm) is True
    assert detect_code_language(valid_asm) == "x86_asm"

    # Valid x64 assembly
    valid_x64 = "mov rax, rcx\nadd rax, rdx\nret"
    assert is_valid_code_block(valid_x64) is True
    assert detect_code_language(valid_x64) == "x64_asm"

    # Valid C struct
    valid_c = "typedef struct _IMAGE_DOS_HEADER {\n    WORD e_magic;\n    WORD e_cblp;\n} IMAGE_DOS_HEADER;"
    assert is_valid_code_block(valid_c) is True
    assert detect_code_language(valid_c) == "c"

    # Invalid: 1-line URL
    invalid_url = "https://www.wiley.com/go/practicalreverseengineering"
    assert is_valid_code_block(invalid_url) is False

    # Invalid: 1-line email
    invalid_email = "author@wiley.com"
    assert is_valid_code_block(invalid_email) is False

    # Invalid: 1-line filename
    invalid_filename = "main.c"
    assert is_valid_code_block(invalid_filename) is False


def test_workspace_consolidation_and_preview(tmp_path: Path) -> None:
    workspace = tmp_path / "test_workspace"
    workspace.mkdir()

    # Create dummy books
    book1 = workspace / "books" / "book_01" / "datasets"
    book1.mkdir(parents=True)
    book2 = workspace / "books" / "book_02" / "datasets"
    book2.mkdir(parents=True)

    # Populate dummy datasets
    pretrain_data_1 = [{"doc_id": "doc_01", "chapter_title": "Chap 1", "text": "# Chap 1\nHello"}]
    pretrain_data_2 = [{"doc_id": "doc_02", "chapter_title": "Chap 2", "text": "# Chap 2\nWorld"}]

    (book1 / "pretrain.jsonl").write_text(
        json.dumps(pretrain_data_1[0]) + "\n", encoding="utf-8"
    )
    (book2 / "pretrain.jsonl").write_text(
        json.dumps(pretrain_data_2[0]) + "\n", encoding="utf-8"
    )

    sft_data = [{"id": "sft_01", "instruction": "Explain X", "output": "X is..."}]
    (book1 / "sft.jsonl").write_text(json.dumps(sft_data[0]) + "\n", encoding="utf-8")

    # Consolidate
    manifest = consolidate_workspace_datasets(workspace)
    assert manifest["total_books"] == 2
    assert (workspace / "compiled" / "all_pretrain.jsonl").is_file()
    assert (workspace / "compiled" / "manifest.json").is_file()

    # Check consolidated contents
    compiled_pretrain = (workspace / "compiled" / "all_pretrain.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(compiled_pretrain) == 2

    # Check list datasets
    datasets_list = list_workspace_datasets(workspace)
    assert any(d["name"] == "all_pretrain.jsonl" for d in datasets_list)

    # Check preview pagination
    preview = read_dataset_preview(workspace, "compiled/all_pretrain.jsonl", offset=0, limit=1)
    assert preview["total_records"] == 2
    assert len(preview["records"]) == 1
    assert preview["records"][0]["doc_id"] == "doc_01"

    # Second page
    preview_p2 = read_dataset_preview(workspace, "compiled/all_pretrain.jsonl", offset=1, limit=1)
    assert len(preview_p2["records"]) == 1
    assert preview_p2["records"][0]["doc_id"] == "doc_02"
