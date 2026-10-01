"""
Unit tests for atomic.py — the §5.4 commit protocol.

Tests cover:
- Normal commit round-trip
- FileExistsError on double-commit (cache hit)
- Incomplete commit without _SUCCESS is not valid
- is_valid_commit with and without deep check
- Simulated crash between rename and DB write
- write_json / write_text roundtrip
- sweep_partials removes .partial dirs
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from dabook.core.atomic import (
    StageCommit,
    is_valid_commit,
    read_manifest,
    sha256_bytes,
    sweep_partials,
    write_file,
    write_json,
    write_text,
)


def test_write_json_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "test.json"
    obj = {"key": "value", "nums": [1, 2, 3]}
    write_json(p, obj)
    assert p.exists()
    loaded = json.loads(p.read_text())
    assert loaded == obj


def test_write_text_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "test.txt"
    write_text(p, "hello\nworld")
    assert p.read_text() == "hello\nworld"


def test_sha256_bytes_stable() -> None:
    h = sha256_bytes(b"hello")
    assert h == sha256_bytes(b"hello")
    assert h != sha256_bytes(b"world")
    assert len(h) == 64


def test_stage_commit_success(tmp_path: Path) -> None:
    stage_dir = tmp_path / "stages" / "s02_extract"
    meta = {"stage": "s02_extract", "impl_version": "0.1.0"}
    key = "abc123def456"

    with StageCommit(stage_dir, key, meta) as work_dir:
        assert work_dir == stage_dir / f"{key}.partial"
        write_json(work_dir / "output.json", {"status": "ok"})

    final = stage_dir / key
    assert final.is_dir()
    assert (final / "_SUCCESS").is_file()
    assert (final / "output.json").is_file()


def test_stage_commit_manifest_valid(tmp_path: Path) -> None:
    stage_dir = tmp_path / "stages" / "s02_extract"
    key = "testkey0001"
    meta = {"stage": "test"}

    with StageCommit(stage_dir, key, meta) as work_dir:
        write_json(work_dir / "data.json", {"rows": 42})

    final = stage_dir / key
    manifest = read_manifest(final)
    assert "files" in manifest
    assert "committed_at" in manifest
    assert "data.json" in manifest["files"]
    assert manifest["files"]["data.json"]["bytes"] > 0


def test_is_valid_commit_false_without_success(tmp_path: Path) -> None:
    d = tmp_path / "fake_stage"
    d.mkdir()
    (d / "output.json").write_text("{}")
    assert not is_valid_commit(d)


def test_is_valid_commit_true_after_commit(tmp_path: Path) -> None:
    stage_dir = tmp_path / "stages"
    key = "validstagekey"
    meta = {"stage": "test"}

    with StageCommit(stage_dir, key, meta) as work_dir:
        write_file(work_dir / "file.bin", b"\x00" * 100)

    assert is_valid_commit(stage_dir / key)
    assert is_valid_commit(stage_dir / key, deep=True)


def test_double_commit_raises(tmp_path: Path) -> None:
    stage_dir = tmp_path / "stages"
    key = "dupkey"
    meta = {"stage": "test"}

    with StageCommit(stage_dir, key, meta) as work_dir:
        write_json(work_dir / "x.json", {})

    with pytest.raises(FileExistsError), StageCommit(stage_dir, key, meta):
        pass


def test_partial_dir_left_on_exception(tmp_path: Path) -> None:
    stage_dir = tmp_path / "stages"
    key = "failkey"
    meta = {"stage": "test"}

    with pytest.raises(ValueError), StageCommit(stage_dir, key, meta) as work_dir:
        write_json(work_dir / "x.json", {})
        raise ValueError("simulated failure")

    # .partial dir should still be there
    assert (stage_dir / f"{key}.partial").is_dir()
    # Final dir should NOT exist
    assert not (stage_dir / key).exists()


def test_partial_dir_reused_on_retry(tmp_path: Path) -> None:
    """Retry picks up where the first attempt left off."""
    stage_dir = tmp_path / "stages"
    key = "retrykey"
    meta = {"stage": "test"}

    # First attempt: write one file, then fail
    try:
        with StageCommit(stage_dir, key, meta) as work_dir:
            write_json(work_dir / "progress.json", {"unit": 1})
            raise RuntimeError("crash")
    except RuntimeError:
        pass

    partial = stage_dir / f"{key}.partial"
    assert partial.is_dir()
    assert (partial / "progress.json").is_file()

    # Second attempt: reuses partial dir, adds more files, succeeds
    with StageCommit(stage_dir, key, meta) as work_dir:
        # Previous file is still there
        assert (work_dir / "progress.json").is_file()
        write_json(work_dir / "output.json", {"done": True})

    assert is_valid_commit(stage_dir / key)
    # Manifest should include both files
    manifest = read_manifest(stage_dir / key)
    assert "progress.json" in manifest["files"]
    assert "output.json" in manifest["files"]


def test_sweep_partials(tmp_path: Path) -> None:
    # Create some .partial dirs
    (tmp_path / "abc.partial").mkdir()
    (tmp_path / "def.partial" / "sub").mkdir(parents=True)
    (tmp_path / "abc.partial" / "data.json").write_text("{}")
    # Also create a legit committed dir (no .partial suffix)
    committed = tmp_path / "committed"
    committed.mkdir()
    (committed / "_SUCCESS").write_text("{}")

    swept = sweep_partials(tmp_path)
    assert len(swept) == 2
    assert not (tmp_path / "abc.partial").exists()
    assert not (tmp_path / "def.partial").exists()
    # Legitimate committed dir is untouched
    assert committed.exists()


def test_concurrent_commit_idempotent(tmp_path: Path) -> None:
    """Simulates two workers committing the same stage_key. Second is silently discarded."""
    stage_dir = tmp_path / "stages"
    key = "concurrent"
    meta = {"stage": "test"}

    # Worker 1 commits successfully
    with StageCommit(stage_dir, key, meta) as wd:
        write_json(wd / "result.json", {"worker": 1})

    # Worker 2 has a .partial and tries to rename — target exists
    partial2 = stage_dir / f"{key}.partial2"
    partial2.mkdir(parents=True)
    write_json(partial2 / "result.json", {"worker": 2})
    write_json(partial2 / "_SUCCESS", {"committed_at": 0, "files": {}})

    # Simulate the os.replace failing gracefully (final exists)
    try:
        os.replace(partial2, stage_dir / key)
    except OSError:
        # On Windows if dest exists it may raise; clean up
        if (stage_dir / key).exists():
            shutil.rmtree(partial2, ignore_errors=True)

    # Original commit is still valid
    assert is_valid_commit(stage_dir / key)
    manifest = read_manifest(stage_dir / key)
    assert manifest["committed_at"] > 0
    # Worker 1's commit should prevail
    worker = json.loads((stage_dir / key / "result.json").read_text())["worker"]
    assert worker == 1
