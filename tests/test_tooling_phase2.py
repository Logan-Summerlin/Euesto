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
from executor.errors import ExecutorToolError


def test_read_preserves_hash_and_line_ranges(tmp_path: Path) -> None:
    path = tmp_path / "src.py"; path.write_text("one\ntwo\nthree\n", encoding="utf-8")
    output, data = read(tmp_path, {"path": "src.py", "start_line": 2, "end_line": 3}, max_bytes=64_000)
    assert output == "two\nthree"; assert data["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest(); assert data["encoding"] == "utf-8"


def test_write_can_create_parents_without_hash(tmp_path: Path) -> None:
    output, data = write(tmp_path, {"path": "src/new.py", "content": "print('ok')\n", "create_parents": True}, max_bytes=64_000)
    assert output == "Created src/new.py. Changed 1 line."; assert (tmp_path / "src/new.py").read_text(encoding="utf-8") == "print('ok')\n"; assert data["old_sha256"] is None; assert data["new_sha256"] == hashlib.sha256(b"print('ok')\n").hexdigest()


def test_write_rejects_wrong_hash(tmp_path: Path) -> None:
    (tmp_path / "x.py").write_text("old", encoding="utf-8")
    with pytest.raises(ValueError, match="Staging hash conflict"): write(tmp_path, {"path": "x.py", "content": "new", "expected_sha256": "wrong"}, max_bytes=64_000)
    assert (tmp_path / "x.py").read_text(encoding="utf-8") == "old"


def test_edit_defaults_to_one_occurrence_and_reports_actual_count(tmp_path: Path) -> None:
    path = tmp_path / "x.py"; path.write_text("value\nvalue\n", encoding="utf-8")
    with pytest.raises(ValueError, match="expected 1, found 2"): edit(tmp_path, {"path": "x.py", "old_str": "value", "new_str": "changed"}, max_target_bytes=64_000, max_result_bytes=64_000)
    assert path.read_text(encoding="utf-8") == "value\nvalue\n"


def test_edit_succeeds_without_hash(tmp_path: Path) -> None:
    path = tmp_path / "x.py"; path.write_text("before", encoding="utf-8")
    _, data = edit(tmp_path, {"path": "x.py", "old_str": "before", "new_str": "after"}, max_target_bytes=64_000, max_result_bytes=64_000)
    assert path.read_text(encoding="utf-8") == "after"; assert data["actual_occurrences"] == 1; assert data["old_sha256"] != data["new_sha256"]


def test_edit_has_independent_target_and_result_limits(tmp_path: Path) -> None:
    path = tmp_path / "x.py"; path.write_text("small", encoding="utf-8")
    with pytest.raises(ValueError, match="Edited content exceeds"): edit(tmp_path, {"path": "x.py", "old_str": "small", "new_str": "this result is too large"}, max_target_bytes=100, max_result_bytes=10)
    assert path.read_text(encoding="utf-8") == "small"


def test_ls_is_not_recursive_and_find_is_recursive(tmp_path: Path) -> None:
    (tmp_path / "top.py").write_text("top", encoding="utf-8"); (tmp_path / "src").mkdir(); (tmp_path / "src" / "nested.py").write_text("nested", encoding="utf-8")
    ls_output, ls_data = ls(tmp_path, {"path": ".", "details": False}); find_output, find_data = find(tmp_path, {"path": ".", "glob": "*.py", "max_depth": 10, "max_results": 500, "details": False})
    assert "top.py" in ls_output and "nested.py" not in ls_output; assert "top.py" in find_output and "src/nested.py" in find_output; assert ls_data["recursive"] is False and find_data["recursive"] is True


def test_result_limits_are_authoritative(tmp_path: Path) -> None:
    for index in range(4): (tmp_path / f"{index}.py").write_text("needle\n", encoding="utf-8")
    ls_output, ls_data = ls(tmp_path, {"path": ".", "max_results": 4}, max_results=2); find_output, find_data = find(tmp_path, {"path": ".", "glob": "*.py", "max_results": 4}, max_results=2); grep_output, grep_data = grep(tmp_path, {"path": ".", "query": "needle", "max_results": 4}, max_bytes=64_000, max_results=2)
    assert ls_data["limit"] == 2 and ls_data["returned"] == 2 and ls_data["truncated"]; assert find_data["limit"] == 2 and find_data["returned"] == 2 and find_data["truncated"]; assert grep_data["matches_returned"] == 2 and grep_data["truncated"]; assert len(ls_output.splitlines()) == 2 and len(find_output.splitlines()) == 2 and len(grep_output.splitlines()) == 2


def test_grep_preserves_literal_matching_and_case_sensitivity(tmp_path: Path) -> None:
    (tmp_path / "x.py").write_text("ExecutorService\nexecutorservice\n", encoding="utf-8"); output, data = grep(tmp_path, {"path": ".", "query": "ExecutorService", "case_sensitive": True, "max_results": 10}, max_bytes=64_000)
    assert output.startswith("x.py:1:ExecutorService"); assert data["matches_returned"] == 1


def test_read_line_range_from_oversized_file(tmp_path: Path) -> None:
    path = tmp_path / "large.txt"; path.write_text("".join(f"line-{i}\n" for i in range(80_000)), encoding="utf-8")
    output, data = read(tmp_path, {"path": "large.txt", "start_line": 70_000, "end_line": 70_002, "max_bytes": 128}, max_bytes=128)
    assert output == "line-69999\nline-70000\nline-70001"; assert data["size_bytes"] > 256_000; assert data["truncated"] is False; assert data["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_read_line_range_is_bounded_with_continuation(tmp_path: Path) -> None:
    path = tmp_path / "large.txt"; path.write_text("".join(f"line-{i}\n" for i in range(20_000)), encoding="utf-8")
    output, data = read(tmp_path, {"path": "large.txt", "start_line": 1, "end_line": 20_000, "max_bytes": 64}, max_bytes=64)
    assert len(output.encode("utf-8")) <= 64; assert data["truncated"] is True; assert isinstance(data["next_offset"], int); assert isinstance(data["next_start_line"], int)


def test_read_byte_offsets_and_utf8_boundaries(tmp_path: Path) -> None:
    path = tmp_path / "utf8.txt"; path.write_text("alpha café omega", encoding="utf-8"); offset = len("alpha ".encode("utf-8")); output, data = read(tmp_path, {"path": "utf8.txt", "offset": offset, "max_bytes": 32}, max_bytes=32)
    assert output == "café omega"; assert data["byte_offset"] == offset
    with pytest.raises(ValueError, match="UTF-8 character boundary"): read(tmp_path, {"path": "utf8.txt", "offset": offset + 4}, max_bytes=32)


def test_read_rejects_binary_and_invalid_utf8(tmp_path: Path) -> None:
    binary = tmp_path / "binary.bin"; binary.write_bytes(b"hello\x00world")
    with pytest.raises(ValueError, match="Binary files"): read(tmp_path, {"path": "binary.bin"}, max_bytes=64)
    invalid = tmp_path / "invalid.txt"; invalid.write_bytes(b"valid\xfftext")
    with pytest.raises(ValueError, match="valid UTF-8"): read(tmp_path, {"path": "invalid.txt"}, max_bytes=64)


def test_read_limits_are_strict_and_additive_metadata_is_preserved(tmp_path: Path) -> None:
    path = tmp_path / "x.txt"; path.write_text("abcdefghij", encoding="utf-8"); output, data = read(tmp_path, {"path": "x.txt", "max_bytes": 4}, max_bytes=4)
    assert len(output.encode("utf-8")) <= 4; assert data["path"] == "x.txt"; assert "sha256" in data; assert data["truncated"] is True; assert data["next_offset"] == 4


def test_edit_can_replace_in_large_target_with_bounded_memory(tmp_path: Path) -> None:
    path = tmp_path / "large.txt"; path.write_text(("x" * 100 + "\n") * 20_000 + "TARGET\n", encoding="utf-8"); old_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    _, data = edit(tmp_path, {"path": "large.txt", "old_str": "TARGET", "new_str": "done", "expected_sha256": old_hash}, max_target_bytes=3_000_000, max_result_bytes=3_000_000)
    assert path.read_text(encoding="utf-8").endswith("done\n"); assert data["actual_occurrences"] == 1; assert data["old_sha256"] == old_hash; assert data["new_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest(); assert data["diff"]["truncated"] is True


def test_edit_result_limit_is_enforced_before_replacement(tmp_path: Path) -> None:
    path = tmp_path / "large.txt"; path.write_text("a" * 1_100_000, encoding="utf-8")
    with pytest.raises(ValueError, match="Edited content exceeds"): edit(tmp_path, {"path": "large.txt", "old_str": "a", "new_str": "aa", "expected_occurrences": 1}, max_target_bytes=2_000_000, max_result_bytes=1_100_000)
    assert path.stat().st_size == 1_100_000


def test_edit_occurrence_and_hash_conflicts_remain_safe_for_large_files(tmp_path: Path) -> None:
    path = tmp_path / "large.txt"; path.write_text(("needle\n" * 2) + ("x" * 100 + "\n") * 20_000, encoding="utf-8")
    with pytest.raises(ValueError, match="expected 1, found 2"): edit(tmp_path, {"path": "large.txt", "old_str": "needle", "new_str": "changed"}, max_target_bytes=3_000_000, max_result_bytes=3_000_000)
    with pytest.raises(ValueError, match="Staging hash conflict"): edit(tmp_path, {"path": "large.txt", "old_str": "needle", "new_str": "changed", "expected_sha256": "wrong"}, max_target_bytes=3_000_000, max_result_bytes=3_000_000)
    assert path.read_text(encoding="utf-8").startswith("needle\nneedle\n")


def test_edit_preserves_shrink_detection_for_large_targets(tmp_path: Path) -> None:
    path = tmp_path / "large.txt"; old = "line\n" * 50_000; path.write_text(old + ("x" * 100 + "\n") * 500, encoding="utf-8")
    with pytest.raises(ExecutorToolError, match="shrink"): edit(tmp_path, {"path": "large.txt", "old_str": old, "new_str": "small\n"}, max_target_bytes=3_000_000, max_result_bytes=3_000_000)
    assert path.read_text(encoding="utf-8").startswith("line\nline\n")


def test_search_reports_oversized_files_without_claiming_complete_scan(tmp_path: Path) -> None:
    (tmp_path / "large.txt").write_text("needle\n" * 100_000, encoding="utf-8"); (tmp_path / "small.txt").write_text("needle\n", encoding="utf-8")
    output, data = grep(tmp_path, {"path": ".", "query": "needle", "max_results": 10}, max_bytes=100_000, max_results=10)
    assert "small.txt:1:needle" in output; assert data["files_skipped_too_large"] == 1; assert data["scan_scope_complete"] is False


def test_find_paginates_results(tmp_path: Path) -> None:
    for index in range(5): (tmp_path / f"{index}.py").write_text("x", encoding="utf-8")
    first, first_data = find(tmp_path, {"path": ".", "glob": "*.py", "max_results": 2}); second, second_data = find(tmp_path, {"path": ".", "glob": "*.py", "max_results": 2, "cursor": first_data["next_cursor"]})
    assert first.splitlines() == ["0.py", "1.py"]; assert second.splitlines() == ["2.py", "3.py"]; assert first_data["truncated"] is True and second_data["truncated"] is True; assert second_data["next_cursor"]


def test_ls_paginates_results(tmp_path: Path) -> None:
    for index in range(5): (tmp_path / f"{index}.txt").write_text("x", encoding="utf-8")
    first, first_data = ls(tmp_path, {"path": ".", "max_results": 2, "details": False}); second, second_data = ls(tmp_path, {"path": ".", "max_results": 2, "details": False, "cursor": first_data["next_cursor"]})
    assert first.splitlines() == ["0.txt", "1.txt"]; assert second.splitlines() == ["2.txt", "3.txt"]; assert first_data["truncated"] is True and second_data["truncated"] is True


def test_write_reports_requested_limit_and_staging_capacity_separately(tmp_path: Path) -> None:
    _, data = write(tmp_path, {"path": "x.txt", "content": "hello"}, max_bytes=10, max_staging_bytes=20)
    assert data["requested_write_bytes"] == 5; assert data["max_write_bytes"] == 10; assert data["staging_capacity_bytes"] == 20
    with pytest.raises(ValueError, match="staging capacity"): write(tmp_path, {"path": "y.txt", "content": "hello"}, max_bytes=10, max_staging_bytes=4)
