from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from executor.app import _environment_context
from executor.checkpoints import create_checkpoint, inspect_checkpoint
from executor.config import ExecutorConfig
from executor.staging import Snapshot
from executor.tools.apply_patch import apply_patch
from executor.tools.list_files import list_files
from executor.tools.read_file import read_file
from executor.tools.search_text import search_text
from server.agent.runtime import _is_ephemeral_system_context, _render_executor_context
from server.openrouter.agent import LOCAL_TOOL_SCHEMAS
from shared.tools import AGENT_TOOLS, PLAN_TOOLS, TOOL_NAMES, ToolRequest


def _schema(name: str) -> dict:
    return next(item["function"] for item in LOCAL_TOOL_SCHEMAS if item["function"]["name"] == name)


def _config(tmp_path: Path) -> ExecutorConfig:
    source = tmp_path / "source"; work = tmp_path / "work"
    source.mkdir(); work.mkdir()
    return ExecutorConfig(source, work, tmp_path / "executor.sock", "x" * 43, "workspace")


def test_tool_contracts_expose_exact_seven_tools() -> None:
    assert [item["function"]["name"] for item in LOCAL_TOOL_SCHEMAS] == ["read", "write", "edit", "bash", "grep", "find", "ls"]
    assert TOOL_NAMES == {"read", "write", "edit", "bash", "grep", "find", "ls"}
    assert PLAN_TOOLS == {"read", "grep", "find", "ls"}
    assert AGENT_TOOLS == TOOL_NAMES
    assert all(_schema(name).get("description") for name in TOOL_NAMES)
    assert len(json.dumps(LOCAL_TOOL_SCHEMAS, separators=(",", ":"))) < 5_400


def test_legacy_tool_names_are_unknown() -> None:
    for name in ("list_files", "read_file", "search_text", "inspect_workspace", "inspect_checkpoint", "apply_patch", "run_command", "move_file", "copy_file", "restore_checkpoint"):
        with pytest.raises(ValueError, match=f"Unknown tool: {name}"):
            ToolRequest("request", "run", name, "agent")


def test_plan_mode_accepts_exactly_four_tools() -> None:
    for name in PLAN_TOOLS:
        ToolRequest("request", "run", name, "plan")
    for name in TOOL_NAMES - PLAN_TOOLS:
        with pytest.raises(ValueError, match="Plan mode only permits"):
            ToolRequest("request", "run", name, "plan")


def test_agent_mode_accepts_all_seven_tools() -> None:
    for name in TOOL_NAMES:
        ToolRequest("request", "run", name, "agent")


def test_list_files_details_are_bounded_without_duplicate_path_data(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("abc", encoding="utf-8")
    (tmp_path / "folder").mkdir()
    (tmp_path / ".local-chat-snapshot.json").write_text("internal", encoding="utf-8")
    output, data = list_files(tmp_path, {"details": True, "max_results": 10})
    assert "file\t3\ta.txt" in output and "directory\t-\tfolder/" in output
    assert ".local-chat-snapshot" not in output
    assert data["count"] == 2 and data["truncated"] is False and data["has_more"] is False


def test_list_files_can_return_bounded_hash_metadata(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"; target.write_text("abc", encoding="utf-8")
    output, data = list_files(tmp_path, {"include_sha256": True, "max_results": 500})
    assert hashlib.sha256(b"abc").hexdigest() in output
    assert data["include_sha256"] is True and data["limit"] == 100


def test_read_file_returns_edit_hash_and_bounds_batch_content(tmp_path: Path) -> None:
    first = tmp_path / "first.txt"; second = tmp_path / "second.txt"
    first.write_text("alpha\nbeta", encoding="utf-8"); second.write_text("gamma\ndelta", encoding="utf-8")
    output, data = read_file(tmp_path, {"path": "first.txt"}, max_bytes=1_000_000)
    assert output == "alpha\nbeta" and data["sha256"] == hashlib.sha256(b"alpha\nbeta").hexdigest()
    batch, batch_data = read_file(tmp_path, {"paths": ["first.txt", "second.txt"], "max_bytes": 8}, max_bytes=1_000_000)
    assert "--- first.txt ---" in batch and "--- second.txt ---" in batch and batch_data["content_bytes"] <= 8


def test_search_supports_case_and_globs_without_duplicating_matches(tmp_path: Path) -> None:
    (tmp_path / "one.py").write_text("Needle\nneedle", encoding="utf-8")
    (tmp_path / "two.txt").write_text("Needle", encoding="utf-8")
    output, data = search_text(tmp_path, {"query": "Needle", "case_sensitive": True, "include_glob": "*.py", "max_results": 10}, max_bytes=1_000_000)
    assert output == "one.py:1:Needle" and data["matches_returned"] == 1 and data["files_considered"] == 1


def test_executor_context_is_live_compact_and_mode_specific(tmp_path: Path) -> None:
    config = _config(tmp_path)
    environment = _environment_context(config, Snapshot("snapshot", {"a.txt": "hash"}, total_bytes=12))
    agent = _render_executor_context({"environment": environment}, "agent")
    plan = _render_executor_context({"environment": environment}, "plan")
    assert "1 files, 12 bytes" in agent and "no network or GPU" in agent
    assert "Plan mode reads the selected source workspace" in plan
    assert _is_ephemeral_system_context({"role": "system", "content": agent})


def test_checkpoint_inspection_can_diff_current_staging(tmp_path: Path) -> None:
    target = tmp_path / "value.txt"; target.write_text("before\n", encoding="utf-8")
    checkpoint_id = create_checkpoint(tmp_path); target.write_text("after\n", encoding="utf-8")
    result = inspect_checkpoint(tmp_path, checkpoint_id, diff_paths=["value.txt"], max_diff_bytes=10_000, max_diff_lines=100)
    diff = result["diffs"][0]["text"]
    assert "-before" in diff and "+after" in diff and result["diff_truncated"] is False


def test_apply_patch_exact_replacement_uses_read_hash(tmp_path: Path) -> None:
    target = tmp_path / "value.txt"; target.write_text("one two three", encoding="utf-8")
    _content, metadata = read_file(tmp_path, {"path": "value.txt"}, max_bytes=1_000)
    _output, result = apply_patch(tmp_path, {"edits": [{"path": "value.txt", "expected_sha256": metadata["sha256"], "mode": "replace_exact", "old_str": "two", "new_str": "2"}]}, max_bytes=1_000)
    assert target.read_text(encoding="utf-8") == "one 2 three"
    assert result["changed"][0]["staged_sha256"] == hashlib.sha256(b"one 2 three").hexdigest()
