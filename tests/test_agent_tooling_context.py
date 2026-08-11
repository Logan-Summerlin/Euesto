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


def _schema(name: str) -> dict:
    return next(item["function"] for item in LOCAL_TOOL_SCHEMAS if item["function"]["name"] == name)


def _config(tmp_path: Path) -> ExecutorConfig:
    source = tmp_path / "source"
    work = tmp_path / "work"
    source.mkdir()
    work.mkdir()
    return ExecutorConfig(source, work, tmp_path / "executor.sock", "x" * 43, "workspace")


def test_tool_contracts_expose_existing_controls_without_excess_schema_context() -> None:
    read_parameters = _schema("read_file")["parameters"]
    search_parameters = _schema("search_text")["parameters"]["properties"]
    patch_parameters = _schema("apply_patch")["parameters"]

    assert {"path", "paths", "max_bytes"} <= set(read_parameters["properties"])
    assert read_parameters["properties"]["max_bytes"]["maximum"] == 256_000
    assert "case_sensitive" in search_parameters
    assert {"include_glob", "exclude_glob"} <= set(search_parameters)
    assert "replacements" in patch_parameters["properties"]
    assert all(_schema(name).get("description") for name in {
        "list_files", "read_file", "search_text", "inspect_workspace",
        "inspect_checkpoint", "apply_patch", "run_command", "move_file",
        "copy_file", "restore_checkpoint",
    })
    assert len(json.dumps(LOCAL_TOOL_SCHEMAS, separators=(",", ":"))) < 5_400


def test_list_files_details_are_bounded_without_duplicate_path_data(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("abc", encoding="utf-8")
    (tmp_path / "folder").mkdir()
    (tmp_path / ".local-chat-snapshot.json").write_text("internal", encoding="utf-8")

    output, data = list_files(tmp_path, {"details": True, "max_results": 10})

    assert "file\t3\ta.txt" in output
    assert "directory\t-\tfolder/" in output
    assert ".local-chat-snapshot" not in output
    assert data == {"count": 2, "truncated": False, "details": True}


def test_list_files_can_return_bounded_hash_metadata(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("abc", encoding="utf-8")

    output, data = list_files(
        tmp_path, {"include_sha256": True, "max_results": 500}
    )

    assert hashlib.sha256(b"abc").hexdigest() in output
    assert data["include_sha256"] is True
    assert data["limit"] == 100


def test_read_file_returns_edit_hash_and_bounds_batch_content(tmp_path: Path) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("alpha\nbeta", encoding="utf-8")
    second.write_text("gamma\ndelta", encoding="utf-8")

    output, data = read_file(tmp_path, {"path": "first.txt"}, max_bytes=1_000_000)
    assert output == "alpha\nbeta"
    assert data["sha256"] == hashlib.sha256(b"alpha\nbeta").hexdigest()
    assert data["size_bytes"] == 10

    batch, batch_data = read_file(
        tmp_path,
        {"paths": ["first.txt", "second.txt"], "max_bytes": 8},
        max_bytes=1_000_000,
    )
    assert "--- first.txt ---" in batch and "--- second.txt ---" in batch
    assert batch_data["count"] == 2
    assert batch_data["content_bytes"] <= 8
    assert all("sha256" in item for item in batch_data["files"])


def test_search_supports_case_and_globs_without_duplicating_matches(tmp_path: Path) -> None:
    (tmp_path / "one.py").write_text("Needle\nneedle", encoding="utf-8")
    (tmp_path / "two.txt").write_text("Needle", encoding="utf-8")
    (tmp_path / ".local-chat-checkpoint.json").write_text("Needle", encoding="utf-8")

    output, data = search_text(
        tmp_path,
        {
            "query": "Needle",
            "case_sensitive": True,
            "include_glob": "*.py",
            "max_results": 10,
        },
        max_bytes=1_000_000,
    )
    assert output == "one.py:1:Needle"
    assert data == {"matches_returned": 1, "files_scanned": 1, "truncated": False}

    _output, limited = search_text(
        tmp_path,
        {"query": "needle", "max_results": 1},
        max_bytes=1_000_000,
    )
    assert limited["matches_returned"] == 1
    assert limited["truncated"] is True


def test_apply_patch_exact_replacement_uses_read_hash_and_fails_before_writing(
    tmp_path: Path,
) -> None:
    target = tmp_path / "value.txt"
    target.write_text("one two three", encoding="utf-8")
    _content, metadata = read_file(tmp_path, {"path": "value.txt"}, max_bytes=1_000)

    _output, result = apply_patch(
        tmp_path,
        {
            "replacements": [
                {
                    "path": "value.txt",
                    "expected_sha256": metadata["sha256"],
                    "old_text": "two",
                    "new_text": "2",
                }
            ]
        },
        max_bytes=1_000,
    )
    assert target.read_text(encoding="utf-8") == "one 2 three"
    assert result["changed"][0]["staged_sha256"] == hashlib.sha256(b"one 2 three").hexdigest()

    current = hashlib.sha256(b"one 2 three").hexdigest()
    with pytest.raises(ValueError, match="Replacement match conflict"):
        apply_patch(
            tmp_path,
            {
                "edits": [
                    {"path": "unwritten.txt", "expected_sha256": None, "content": "new"}
                ],
                "replacements": [
                    {
                        "path": "value.txt",
                        "expected_sha256": current,
                        "old_text": "e",
                        "new_text": "E",
                    }
                ],
            },
            max_bytes=1_000,
        )
    assert not (tmp_path / "unwritten.txt").exists()


def test_executor_context_is_live_compact_and_mode_specific(tmp_path: Path) -> None:
    config = _config(tmp_path)
    environment = _environment_context(
        config,
        Snapshot("snapshot", {"a.txt": "hash"}, total_bytes=12),
    )

    agent = _render_executor_context({"environment": environment}, "agent")
    plan = _render_executor_context({"environment": environment}, "plan")

    assert "1 files, 12 bytes" in agent
    assert "ephemeral staged copy" in agent
    assert "no network or GPU" in agent
    assert ".local-chat-* entries are executor metadata" in agent
    assert "Plan mode reads the selected source workspace" in plan
    assert "Initial visible staging snapshot" not in plan
    assert _is_ephemeral_system_context({"role": "system", "content": agent})
    assert len(agent) <= 1_600
    assert len(agent) // 4 < 400

    auto = _render_executor_context({"environment": environment}, "agent", "auto")
    assert "successful host publication are automatic" in auto
    assert len(auto) <= 1_600


def test_checkpoint_inspection_can_diff_current_staging(tmp_path: Path) -> None:
    target = tmp_path / "value.txt"
    target.write_text("before\n", encoding="utf-8")
    checkpoint_id = create_checkpoint(tmp_path)
    target.write_text("after\n", encoding="utf-8")

    result = inspect_checkpoint(
        tmp_path,
        checkpoint_id,
        diff_paths=["value.txt"],
        max_diff_bytes=10_000,
        max_diff_lines=100,
    )

    diff = result["diffs"][0]["text"]
    assert "-before" in diff and "+after" in diff
    assert result["diff_truncated"] is False
