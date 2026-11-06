"""`write` append mode: non-destructive growth, bounds, and checkpoint rollback."""

from __future__ import annotations

import importlib
import os
import stat
from pathlib import Path

import pytest

from executor.errors import ExecutorToolError
from executor.tools import write

CHECKPOINT_BYTES = 100_000_000


def _write(root: Path, arguments: dict, *, max_bytes: int = 1_000_000):
    return write(
        root,
        arguments,
        max_bytes=max_bytes,
        max_checkpoint_bytes=CHECKPOINT_BYTES,
    )


def test_append_creates_a_missing_file_and_parents(tmp_path: Path) -> None:
    output, data = _write(
        tmp_path,
        {
            "path": "new/deep/a.txt",
            "content": "first\n",
            "mode": "append",
            "create_parents": True,
        },
    )
    assert (tmp_path / "new/deep/a.txt").read_text(encoding="utf-8") == "first\n"
    assert data["mode"] == "append"
    assert data["old_sha256"] is None
    assert data["appended_bytes"] == 6
    assert data["size_bytes"] == 6
    assert output.startswith("Created new/deep/a.txt.")


def test_append_preserves_content_and_reports_sizes(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    # Bytes, not write_text: text mode writes "\r\n" on Windows — five bytes, not four.
    path.write_bytes(b"old\n")
    _, data = _write(
        tmp_path,
        {"path": "a.txt", "content": "new\n", "mode": "append"},
    )
    assert path.read_bytes() == b"old\nnew\n"
    assert data["old_size_bytes"] == 4
    assert data["appended_bytes"] == 4
    assert data["size_bytes"] == 8
    assert data["old_sha256"] != data["new_sha256"]


@pytest.mark.posix
def test_append_preserves_the_file_mode(tmp_path: Path) -> None:
    # Windows has no POSIX permission bits: os.chmod there only toggles the read-only flag.
    path = tmp_path / "a.txt"
    path.write_text("old\n", encoding="utf-8")
    os.chmod(path, 0o640)
    _write(tmp_path, {"path": "a.txt", "content": "new\n", "mode": "append"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_append_without_the_shrink_guard(tmp_path: Path) -> None:
    path = tmp_path / "large.txt"
    path.write_text("line\n" * 2_000, encoding="utf-8")
    _, data = _write(
        tmp_path,
        {"path": "large.txt", "content": "tail\n", "mode": "append"},
    )
    assert path.read_text(encoding="utf-8").endswith("tail\n")
    assert "shrink_warning" not in data


def test_append_is_bounded_by_the_resulting_file_size(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_text("x" * 90, encoding="utf-8")
    with pytest.raises(ExecutorToolError) as raised:
        _write(
            tmp_path,
            {"path": "a.txt", "content": "y" * 20, "mode": "append"},
            max_bytes=100,
        )
    error = raised.value
    assert error.code == "limit.exceeded"
    assert error.details["failure"] == "append_too_large"
    assert error.details["size_bytes"] == 110
    assert path.read_text(encoding="utf-8") == "x" * 90


def test_append_rejects_an_unknown_mode(tmp_path: Path) -> None:
    for mode in ("insert", ["append"], True):
        with pytest.raises(ExecutorToolError) as raised:
            _write(tmp_path, {"path": "a.txt", "content": "x", "mode": mode})
        assert raised.value.code == "request.invalid_arguments"
    assert not (tmp_path / "a.txt").exists()


def test_a_failed_append_restores_the_checkpoint(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "a.txt"
    path.write_text("original\n", encoding="utf-8")
    module = importlib.import_module("executor.tools.write")

    def interrupted(target, content, *, encoding="utf-8"):
        raise OSError("disk full")

    monkeypatch.setattr(module, "atomic_append_text", interrupted)
    with pytest.raises(OSError):
        _write(tmp_path, {"path": "a.txt", "content": "appended\n", "mode": "append"})
    assert path.read_text(encoding="utf-8") == "original\n"
    assert not list(tmp_path.glob(".a.txt.atomic-*"))
