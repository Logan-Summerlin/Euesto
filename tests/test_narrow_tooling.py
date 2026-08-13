from pathlib import Path

import pytest

from executor.tools.list_files import list_files
from executor.tools.read_file import read_file
from executor.tools.search_text import search_text
from shared.tools import AGENT_TOOLS, PLAN_TOOLS, TOOL_NAMES, ToolRequest


def test_read_file_rejects_mixed_line_and_byte_ranges(tmp_path: Path) -> None:
    path = tmp_path / "sample.txt"
    path.write_text("one\ntwo\nthree\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mutually exclusive"):
        read_file(tmp_path, {"path": "sample.txt", "start_line": 1, "start_byte": 2}, max_bytes=64_000)


def test_read_file_rejects_out_of_range_lines_instead_of_empty_result(tmp_path: Path) -> None:
    (tmp_path / "sample.txt").write_text("one\ntwo\n", encoding="utf-8")
    with pytest.raises(ValueError, match="line range is outside file"):
        read_file(tmp_path, {"path": "sample.txt", "start_line": 4, "end_line": 4}, max_bytes=64_000)


def test_read_file_reports_exact_missing_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"file not found: nested/missing.txt"):
        read_file(tmp_path, {"path": "nested/missing.txt"}, max_bytes=64_000)


def test_list_files_reports_truncation_and_keeps_default_output_minimal(tmp_path: Path) -> None:
    for name in ("a.txt", "b.txt", "c.txt"):
        (tmp_path / name).write_text(name, encoding="utf-8")
    output, data = list_files(tmp_path, {"max_results": 2})
    assert output.splitlines() == ["a.txt", "b.txt"]
    assert data["truncated"] is True
    assert data["has_more"] is True
    assert data["next_cursor"]
    assert "size_bytes" not in data


def test_list_files_max_depth_prunes_recursive_traversal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shallow = tmp_path / "shallow"
    deep = shallow / "nested"
    deep.mkdir(parents=True)
    (deep / "file.txt").write_text("nested", encoding="utf-8")
    (tmp_path / "sibling.txt").write_text("sibling", encoding="utf-8")
    original_iterdir = Path.iterdir
    traversed: list[Path] = []
    def tracking_iterdir(path: Path):
        traversed.append(path)
        return original_iterdir(path)
    monkeypatch.setattr(Path, "iterdir", tracking_iterdir)
    output, data = list_files(tmp_path, {"max_depth": 1})
    assert output.splitlines() == ["shallow/", "sibling.txt"]
    assert data["total_known"] == 2
    assert traversed == [tmp_path]


def test_list_files_max_results_stops_before_descending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir()
    second.mkdir()
    for index in range(100):
        (second / f"{index:03}.txt").write_text("x", encoding="utf-8")
    original_iterdir = Path.iterdir
    traversed: list[Path] = []
    def tracking_iterdir(path: Path):
        traversed.append(path)
        return original_iterdir(path)
    monkeypatch.setattr(Path, "iterdir", tracking_iterdir)
    output, data = list_files(tmp_path, {"max_results": 1})
    assert output == "a/"
    assert data["truncated"] is True
    assert second not in traversed


def test_list_files_preserves_deterministic_order_and_cursor(tmp_path: Path) -> None:
    for name in ("b.txt", "a.txt", "c.txt"):
        (tmp_path / name).write_text(name, encoding="utf-8")
    first, first_data = list_files(tmp_path, {"max_results": 2})
    second, second_data = list_files(tmp_path, {"max_results": 2, "cursor": first_data["next_cursor"]})
    assert first.splitlines() == ["a.txt", "b.txt"]
    assert second.splitlines() == ["c.txt"]
    assert second_data["has_more"] is False


def test_search_text_glob_limits_files_considered_and_searched(tmp_path: Path) -> None:
    (tmp_path / "match.txt").write_text("needle\n", encoding="utf-8")
    (tmp_path / "other.py").write_text("needle\n", encoding="utf-8")
    output, data = search_text(tmp_path, {"query": "needle", "include_glob": "*.txt"}, max_bytes=64_000)
    assert "match.txt:1:needle" in output
    assert "other.py" not in output
    assert data["files_considered"] == 1
    assert data["files_searched"] == 1


def test_phase_one_tool_contract_is_exact() -> None:
    assert TOOL_NAMES == {"read", "write", "edit", "bash", "grep", "find", "ls"}
    assert PLAN_TOOLS == {"read", "grep", "find", "ls"}
    assert AGENT_TOOLS == TOOL_NAMES
    for name in TOOL_NAMES:
        ToolRequest("request", "run", name, "agent")
    for name in PLAN_TOOLS:
        ToolRequest("request", "run", name, "plan")
    for name in TOOL_NAMES - PLAN_TOOLS:
        with pytest.raises(ValueError, match="Plan mode only permits"):
            ToolRequest("request", "run", name, "plan")
    for name in ("list_files", "read_file", "search_text", "inspect_workspace", "inspect_checkpoint", "apply_patch", "run_command", "move_file", "copy_file", "restore_checkpoint"):
        with pytest.raises(ValueError, match=f"Unknown tool: {name}"):
            ToolRequest("request", "run", name, "agent")
