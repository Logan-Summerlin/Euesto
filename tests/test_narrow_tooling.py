from pathlib import Path

import pytest

from executor.tools.grep import grep
from executor.tools.ls import ls
from executor.tools.read import read
from shared.tools import AGENT_TOOLS, PLAN_TOOLS, TOOL_NAMES, TOOL_PROFILE, ToolRequest


def test_read_rejects_out_of_range_lines_instead_of_empty_result(tmp_path: Path) -> None:
    (tmp_path / "sample.txt").write_text("one\ntwo\n", encoding="utf-8")
    with pytest.raises(ValueError, match="line range is outside file"):
        read(tmp_path, {"path": "sample.txt", "start_line": 4, "end_line": 4}, max_bytes=64_000)


def test_read_reports_exact_missing_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"file not found: nested/missing.txt"):
        read(tmp_path, {"path": "nested/missing.txt"}, max_bytes=64_000)


def test_ls_lists_immediate_contents(tmp_path: Path) -> None:
    (tmp_path / "nested").mkdir()
    (tmp_path / "sample.txt").write_text("sample", encoding="utf-8")
    output, data = ls(tmp_path, {"path": ".", "details": False})
    assert output.splitlines() == ["nested/", "sample.txt"]
    assert data["recursive"] is False


def test_grep_limits_files_considered_and_searched(tmp_path: Path) -> None:
    (tmp_path / "match.txt").write_text("needle\n", encoding="utf-8")
    (tmp_path / "other.py").write_text("needle\n", encoding="utf-8")
    output, data = grep(tmp_path, {"query": "needle", "include_glob": "*.txt"}, max_bytes=64_000)
    assert "match.txt:1:needle" in output
    assert "other.py" not in output
    assert data["files_considered"] == 1
    assert data["files_searched"] == 1


def test_canonical_tool_profile_is_exact() -> None:
    assert TOOL_PROFILE == "pi-compatible"
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
