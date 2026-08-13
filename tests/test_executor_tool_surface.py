from __future__ import annotations

import asyncio
import importlib
from pathlib import Path

import pytest

from executor.app import ExecutorService
from executor.config import ExecutorConfig
from executor.tools import bash, edit, find, grep, ls, read, write
from server.openrouter.agent import LOCAL_TOOL_SCHEMAS
from shared.tools import AGENT_TOOLS, PLAN_TOOLS, TOOL_NAMES, ToolRequest

CANONICAL_TOOLS = ("read", "write", "edit", "bash", "grep", "find", "ls")
READ_ONLY_TOOLS = frozenset({"read", "grep", "find", "ls"})
LEGACY_TOOL_NAMES = {"read_file", "write_file", "edit_file", "apply_patch", "run_command", "list_files", "search_files", "move", "copy", "checkpoint", "restore"}


def _schema_map() -> dict[str, dict]:
    return {item["function"]["name"]: item["function"] for item in LOCAL_TOOL_SCHEMAS}


def test_executor_import_surface_is_loadable() -> None:
    modules = ("executor.app", "executor.config", "executor.errors", "executor.mutations", "executor.paths", "executor.permissions", "executor.staging", "executor.tools", "executor.tools.bash", "executor.tools.edit", "executor.tools.find", "executor.tools.grep", "executor.tools.ls", "executor.tools.read", "executor.tools.write", "server.openrouter.agent")
    for module in modules:
        importlib.import_module(module)


def test_executor_exports_exactly_the_canonical_tools() -> None:
    from executor import tools
    assert set(tools.__all__) == set(CANONICAL_TOOLS) | {"MAX_READ_BYTES"}
    assert all(hasattr(tools, name) for name in CANONICAL_TOOLS)
    assert not LEGACY_TOOL_NAMES.intersection(vars(tools))


def test_public_schema_and_shared_contract_have_one_vocabulary() -> None:
    schema_names = tuple(item["function"]["name"] for item in LOCAL_TOOL_SCHEMAS)
    assert schema_names == CANONICAL_TOOLS
    assert TOOL_NAMES == frozenset(CANONICAL_TOOLS)
    assert PLAN_TOOLS == READ_ONLY_TOOLS
    assert AGENT_TOOLS == TOOL_NAMES
    assert not LEGACY_TOOL_NAMES.intersection(TOOL_NAMES)


def test_each_schema_has_a_matching_executor_callable() -> None:
    implementations = {name: globals()[name] for name in CANONICAL_TOOLS}
    for name, schema in _schema_map().items():
        assert callable(implementations[name])
        assert schema["parameters"]["additionalProperties"] is False


def test_tool_schemas_match_executor_argument_names() -> None:
    expected = {
        "read": {"path", "start_line", "end_line", "max_bytes"},
        "write": {"path", "content", "expected_sha256", "create_parents"},
        "edit": {"path", "old_str", "new_str", "expected_occurrences", "expected_sha256"},
        "bash": {"command", "working_directory", "timeout_seconds", "env", "stdin"},
        "grep": {"query", "path", "regex", "case_sensitive", "include_glob", "exclude_glob", "max_results", "context_lines", "include_metadata", "cursor"},
        "find": {"path", "glob", "max_depth", "max_results", "details"},
        "ls": {"path", "max_results", "details"},
    }
    schemas = _schema_map()
    for name, properties in expected.items():
        assert set(schemas[name]["parameters"]["properties"]) == properties


def test_mode_boundaries_are_enforced() -> None:
    for name in READ_ONLY_TOOLS:
        ToolRequest("request", "run", name, "plan", {})
    for name in CANONICAL_TOOLS:
        if name not in READ_ONLY_TOOLS:
            with pytest.raises(ValueError, match="Plan mode"):
                ToolRequest("request", "run", name, "plan", {})
    for name in CANONICAL_TOOLS:
        assert ToolRequest("request", "run", name, "agent", {}).tool == name


def test_executor_dispatches_all_read_only_tools(tmp_path: Path) -> None:
    source = tmp_path / "source"
    work = tmp_path / "work"
    source.mkdir()
    (source / "one.txt").write_text("needle\n", encoding="utf-8")
    (source / "two.txt").write_text("other\n", encoding="utf-8")
    config = ExecutorConfig(source_root=source, work_root=work, socket_path=tmp_path / "executor.sock", token="x" * 32, workspace_id="test")
    service = ExecutorService(config)
    requests = (
        ToolRequest("read-1", "run", "read", "agent", {"path": "one.txt"}),
        ToolRequest("grep-1", "run", "grep", "agent", {"path": ".", "query": "needle", "max_results": 10}),
        ToolRequest("find-1", "run", "find", "agent", {"path": ".", "glob": "*.txt", "max_results": 10}),
        ToolRequest("ls-1", "run", "ls", "agent", {"path": ".", "details": False}),
    )
    for request in requests:
        result = asyncio.run(service.execute(request))
        assert result.ok, (request.tool, result.to_dict())


def test_executor_dispatches_mutations_only_into_staging(tmp_path: Path) -> None:
    source = tmp_path / "source"
    work = tmp_path / "work"
    source.mkdir()
    (source / "existing.txt").write_text("before\n", encoding="utf-8")
    config = ExecutorConfig(source_root=source, work_root=work, socket_path=tmp_path / "executor.sock", token="x" * 32, workspace_id="test")
    service = ExecutorService(config)
    for request in (
        ToolRequest("write-1", "run", "write", "agent", {"path": "new.txt", "content": "created\n"}),
        ToolRequest("edit-1", "run", "edit", "agent", {"path": "existing.txt", "old_str": "before", "new_str": "after"}),
        ToolRequest("bash-1", "run", "bash", "agent", {"command": "printf '%s\\n' command > command.txt"}),
    ):
        result = asyncio.run(service.execute(request))
        assert result.ok, result.to_dict()
    assert (work / "new.txt").read_text(encoding="utf-8") == "created\n"
    assert (work / "existing.txt").read_text(encoding="utf-8") == "after\n"
    assert (work / "command.txt").read_text(encoding="utf-8") == "command\n"
    assert not (source / "new.txt").exists()
    assert (source / "existing.txt").read_text(encoding="utf-8") == "before\n"


def test_legacy_tool_request_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown tool"):
        ToolRequest.from_dict({"request_id": "request", "run_id": "run", "tool": "run_command", "mode": "agent", "arguments": {"command": "echo nope"}})
