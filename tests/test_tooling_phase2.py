from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from executor.tools.edit import edit
from executor.tools.find import find
from executor.tools.grep import grep
from executor.tools.ls import ls
from executor.tools.read import read
from executor.tools.write import write


def test_read_preserves_hash_and_line_ranges(tmp_path: Path) -> None:
    path = tmp_path / "src.py"
    path.write_text("one\ntwo\nthree\n", encoding="utf-8")
    output, data = read(tmp_path, {"path": "src.py", "start_line": 2, "end_line": 3}, max_bytes=64_000)
    assert output == "two\nthree"
    assert data["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert data["encoding"] == "utf-8"


def test_write_can_create_parents_without_hash(tmp_path: Path) -> None:
    output, data = write(tmp_path, {"path": "src/new.py", "content": "print('ok')\n", "create_parents": True}, max_bytes=64_000)
    assert output == "Wrote src/new.py."
    assert (tmp_path / "src/new.py").read_text(encoding="utf-8") == "print('ok')\n"
    assert data["old_sha256"] is None
    assert data["new_sha256"] == hashlib.sha256(b"print('ok')\n").hexdigest()


def test_write_rejects_wrong_hash(tmp_path: Path) -> None:
    (tmp_path / "x.py").write_text("old", encoding="utf-8")
    with pytest.raises(ValueError, match="Staging hash conflict"):
        write(tmp_path, {"path": "x.py", "content": "new", "expected_sha256": "wrong"}, max_bytes=64_000)
    assert (tmp_path / "x.py").read_text(encoding="utf-8") == "old"


def test_edit_defaults_to_one_occurrence_and_reports_actual_count(tmp_path: Path) -> None:
    path = tmp_path / "x.py"
    path.write_text("value\nvalue\n", encoding="utf-8")
    with pytest.raises(ValueError, match="expected 1, found 2"):
        edit(tmp_path, {"path": "x.py", "old_str": "value", "new_str": "changed"}, max_bytes=64_000)
    assert path.read_text(encoding="utf-8") == "value\nvalue\n"


def test_edit_succeeds_without_hash(tmp_path: Path) -> None:
    path = tmp_path / "x.py"
    path.write_text("before", encoding="utf-8")
    _, data = edit(tmp_path, {"path": "x.py", "old_str": "before", "new_str": "after"}, max_bytes=64_000)
    assert path.read_text(encoding="utf-8") == "after"
    assert data["actual_occurrences"] == 1
    assert data["old_sha256"] != data["new_sha256"]


def test_ls_is_not_recursive_and_find_is_recursive(tmp_path: Path) -> None:
    (tmp_path / "top.py").write_text("top", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "nested.py").write_text("nested", encoding="utf-8")
    ls_output, ls_data = ls(tmp_path, {"path": ".", "details": False},)
    find_output, find_data = find(tmp_path, {"path": ".", "glob": "*.py", "max_depth": 10, "max_results": 500, "details": False})
    assert "top.py" in ls_output
    assert "nested.py" not in ls_output
    assert "top.py" in find_output and "src/nested.py" in find_output
    assert ls_data["recursive"] is False
    assert find_data["recursive"] is True


def test_grep_preserves_literal_matching_and_case_sensitivity(tmp_path: Path) -> None:
    (tmp_path / "x.py").write_text("ExecutorService\nexecutorservice\n", encoding="utf-8")
    output, data = grep(tmp_path, {"path": ".", "query": "ExecutorService", "case_sensitive": True, "max_results": 10}, max_bytes=64_000)
    assert output.startswith("x.py:1:ExecutorService")
    assert data["matches_returned"] == 1
