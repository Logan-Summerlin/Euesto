from __future__ import annotations

from pathlib import Path

import pytest

from executor.tools.list_files import list_files
from executor.tools.read_file import read_file
from executor.tools.search_text import search_text


def test_read_file_rejects_mixed_line_and_byte_ranges(tmp_path: Path) -> None:
    path = tmp_path / "sample.txt"
    path.write_text("one\ntwo\nthree\n", encoding="utf-8")

    with pytest.raises(ValueError, match="mutually exclusive"):
        read_file(
            tmp_path,
            {"path": "sample.txt", "start_line": 1, "start_byte": 2},
            max_bytes=64_000,
        )


def test_read_file_rejects_out_of_range_lines_instead_of_empty_result(tmp_path: Path) -> None:
    (tmp_path / "sample.txt").write_text("one\ntwo\n", encoding="utf-8")

    with pytest.raises(ValueError, match="line range is outside file"):
        read_file(
            tmp_path,
            {"path": "sample.txt", "start_line": 4, "end_line": 4},
            max_bytes=64_000,
        )


def test_read_file_reports_exact_missing_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"file not found: nested/missing.txt"):
        read_file(
            tmp_path,
            {"path": "nested/missing.txt"},
            max_bytes=64_000,
        )


def test_list_files_reports_truncation_and_keeps_default_output_minimal(tmp_path: Path) -> None:
    for name in ("a.txt", "b.txt", "c.txt"):
        (tmp_path / name).write_text(name, encoding="utf-8")

    output, data = list_files(tmp_path, {"max_results": 2})

    assert output.splitlines() == ["a.txt", "b.txt"]
    assert data["truncated"] is True
    assert data["has_more"] is True
    assert data["next_cursor"]
    assert "size_bytes" not in data


def test_search_text_glob_limits_files_considered_and_searched(tmp_path: Path) -> None:
    (tmp_path / "match.txt").write_text("needle\n", encoding="utf-8")
    (tmp_path / "other.py").write_text("needle\n", encoding="utf-8")

    output, data = search_text(
        tmp_path,
        {"query": "needle", "include_glob": "*.txt"},
        max_bytes=64_000,
    )

    assert "match.txt:1:needle" in output
    assert "other.py" not in output
    assert data["files_considered"] == 1
    assert data["files_searched"] == 1
