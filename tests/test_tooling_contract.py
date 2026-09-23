"""The public tool vocabulary, schemas, mode rules, and executor dispatch agree everywhere."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from executor.app import ExecutorService
from executor.config import ExecutorConfig
from server.openrouter.agent import LOCAL_TOOL_SCHEMAS
from shared.tools import (
    AGENT_TOOLS,
    INVESTIGATION_TOOLS,
    MUTATION_TOOLS,
    PLAN_TOOLS,
    READ_TOOLS,
    TOOL_NAMES,
    ToolRequest,
)

CANONICAL_TOOLS = ("read", "write", "edit", "apply_patch", "bash", "grep", "find", "ls", "status")
MODEL_TOOL_NAMES = CANONICAL_TOOLS + ("investigate_repository",)
READ_ONLY_TOOLS = frozenset({"read", "grep", "find", "ls"})
LEGACY_TOOL_NAMES = frozenset({
    "list_files", "read_file", "write_file", "edit_file", "search_files", "search_text",
    "inspect_workspace", "inspect_checkpoint", "patch", "run_command", "move_file", "copy_file",
    "restore_checkpoint", "move", "copy", "checkpoint", "restore",
})
SCHEMA_PROPERTIES = {
    "read": {"path", "start_line", "end_line", "max_bytes"},
    "write": {"path", "content", "expected_sha256", "create_parents"},
    "edit": {"path", "old_str", "new_str", "expected_occurrences", "expected_sha256"},
    "apply_patch": {"operations"},
    "bash": {"command", "working_directory", "timeout_seconds", "env", "stdin", "rollback_on_failure"},
    "grep": {"query", "path", "regex", "case_sensitive", "include_glob", "exclude_glob", "max_results", "context_lines", "include_metadata", "cursor"},
    "find": {"path", "glob", "max_depth", "max_results", "details"},
    "ls": {"path", "max_results", "details"},
    "status": {"paths", "include_diffs", "max_results", "cursor"},
    "investigate_repository": {"query", "inspected_paths"},
}


def _schemas() -> dict[str, dict]:
    return {item["function"]["name"]: item["function"] for item in LOCAL_TOOL_SCHEMAS}


def _config(tmp_path: Path, **limits: int) -> ExecutorConfig:
    return ExecutorConfig(source_root=tmp_path / "source", work_root=tmp_path / "work", socket_path=tmp_path / "executor.sock", token="x" * 32, workspace_id="contract-test", **limits)


# Vocabulary


def test_public_vocabulary_is_exactly_the_ten_tools() -> None:
    assert tuple(item["function"]["name"] for item in LOCAL_TOOL_SCHEMAS) == MODEL_TOOL_NAMES
    assert TOOL_NAMES == frozenset(MODEL_TOOL_NAMES)
    assert AGENT_TOOLS == TOOL_NAMES
    assert PLAN_TOOLS == READ_ONLY_TOOLS
    assert INVESTIGATION_TOOLS == {"investigate_repository"}
    assert READ_TOOLS == READ_ONLY_TOOLS | INVESTIGATION_TOOLS | {"status"}
    assert MUTATION_TOOLS == {"write", "edit", "apply_patch", "bash"}
    assert not LEGACY_TOOL_NAMES & TOOL_NAMES


def test_executor_exports_exactly_the_canonical_tools() -> None:
    from executor import tools

    assert set(tools.__all__) == set(CANONICAL_TOOLS) | {"MAX_READ_BYTES"}
    assert all(callable(getattr(tools, name)) for name in CANONICAL_TOOLS)
    assert not LEGACY_TOOL_NAMES & set(vars(tools))


def test_gateway_status_advertises_the_canonical_local_tools(tmp_path: Path) -> None:
    from server.config import GatewayConfig
    from server.service import GatewayService

    socket = tmp_path / "executor.sock"
    socket.touch()
    service = GatewayService(GatewayConfig("t" * 43, tmp_path / "gateway.sqlite3", executor_socket=socket, executor_token="e" * 43, workspace_id="workspace"))
    try:
        local = [name for name in service.status().supported_tools if not name.startswith("openrouter:")]
    finally:
        asyncio.run(service.close())
    assert tuple(local) == CANONICAL_TOOLS


# Schemas


def test_schemas_declare_exactly_the_executor_arguments() -> None:
    schemas = _schemas()
    assert set(schemas) == set(SCHEMA_PROPERTIES)
    for name, properties in SCHEMA_PROPERTIES.items():
        assert schemas[name]["parameters"]["additionalProperties"] is False, name
        assert set(schemas[name]["parameters"]["properties"]) == properties, name
    for name in ("write", "edit"):
        assert "expected_sha256" not in schemas[name]["parameters"].get("required", [])


def test_model_schema_hard_maxima_match_configuration_ceilings() -> None:
    schemas = {name: schema["parameters"]["properties"] for name, schema in _schemas().items()}
    ceilings = ExecutorConfig.HARD_CEILINGS
    assert schemas["read"]["max_bytes"]["maximum"] == ceilings["max_read_bytes"]
    assert schemas["bash"]["timeout_seconds"]["maximum"] == ceilings["max_command_seconds"]
    assert schemas["bash"]["stdin"]["maxLength"] == ceilings["max_bash_stdin_bytes"]
    assert schemas["grep"]["max_results"]["maximum"] == ceilings["max_search_results"]
    assert schemas["find"]["max_results"]["maximum"] == ceilings["max_find_results"]
    assert schemas["ls"]["max_results"]["maximum"] == ceilings["max_ls_results"]


# Mode rules


@pytest.mark.parametrize("name", MODEL_TOOL_NAMES)
def test_mode_boundaries(name: str) -> None:
    assert ToolRequest("request", "run", name, "agent", {}).tool == name
    if name in READ_ONLY_TOOLS:
        assert ToolRequest("request", "run", name, "plan", {}).tool == name
    else:
        with pytest.raises(ValueError, match="Plan mode only permits"):
            ToolRequest("request", "run", name, "plan", {})


@pytest.mark.parametrize("name", sorted(LEGACY_TOOL_NAMES))
def test_removed_legacy_tools_are_rejected_by_the_request_contract(name: str) -> None:
    with pytest.raises(ValueError, match="Unknown tool"):
        ToolRequest.from_dict({"request_id": "request", "run_id": "run", "tool": name, "mode": "agent", "arguments": {}})


# Executor dispatch


def test_executor_dispatches_every_read_only_tool(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "one.txt").write_text("needle\n", encoding="utf-8")
    (source / "two.txt").write_text("other\n", encoding="utf-8")
    service = ExecutorService(_config(tmp_path))
    for mode in ("plan", "agent"):
        for request in (
            ToolRequest("read-1", "run", "read", mode, {"path": "one.txt"}),
            ToolRequest("grep-1", "run", "grep", mode, {"path": ".", "query": "needle", "max_results": 10}),
            ToolRequest("find-1", "run", "find", mode, {"path": ".", "glob": "*.txt", "max_results": 10}),
            ToolRequest("ls-1", "run", "ls", mode, {"path": ".", "details": False}),
        ):
            result = asyncio.run(service.execute(request))
            assert result.ok, (mode, request.tool, result.to_dict())


def test_executor_dispatches_mutations_only_into_staging(tmp_path: Path) -> None:
    source = tmp_path / "source"
    work = tmp_path / "work"
    source.mkdir()
    (source / "existing.txt").write_text("before\n", encoding="utf-8")
    service = ExecutorService(_config(tmp_path))
    for request in (
        ToolRequest("write-1", "run", "write", "agent", {"path": "new.txt", "content": "created\n"}),
        ToolRequest("edit-1", "run", "edit", "agent", {"path": "existing.txt", "old_str": "before", "new_str": "after"}),
        ToolRequest("patch-1", "run", "apply_patch", "agent", {"operations": [{"operation": "write", "path": "patched.txt", "content": "patched\n"}]}),
        ToolRequest("bash-1", "run", "bash", "agent", {"command": "printf '%s\\n' command > command.txt"}),
    ):
        result = asyncio.run(service.execute(request))
        assert result.ok, result.to_dict()
        assert result.data["workspace_status"]["staged"] is True
    assert (work / "new.txt").read_text(encoding="utf-8") == "created\n"
    assert (work / "existing.txt").read_text(encoding="utf-8") == "after\n"
    assert (work / "patched.txt").read_text(encoding="utf-8") == "patched\n"
    assert (work / "command.txt").read_text(encoding="utf-8") == "command\n"
    assert not (source / "new.txt").exists() and not (source / "patched.txt").exists()
    assert (source / "existing.txt").read_text(encoding="utf-8") == "before\n"
    status = asyncio.run(service.execute(ToolRequest("status-1", "run", "status", "agent", {})))
    assert status.ok
    assert {name: status.data["counts"][name] for name in ("created", "modified", "deleted")} == {"created": 3, "modified": 1, "deleted": 0}


def test_dispatch_passes_only_operation_specific_effective_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "source").mkdir()
    config = _config(tmp_path, max_read_bytes=100, max_write_bytes=110, max_edit_target_bytes=120, max_edit_result_bytes=130, max_patch_operations=9, max_patch_bytes=135, max_bash_output_bytes=140, max_bash_stdin_bytes=150, max_command_bytes=160, max_checkpoint_bytes=1_000, max_staging_bytes=10_000, max_staged_files=170, max_command_seconds=180, max_search_results=3, max_find_results=4, max_ls_results=5, max_grep_scan_bytes=190, max_grep_output_bytes=195, max_search_seconds=7)
    service = ExecutorService(config)
    captured: dict[str, object] = {}

    def fake(name):
        def capture(*_args, **limits):
            captured[name] = limits
            return "ok", {}
        return capture

    async def fake_bash(_request_id, _root, _arguments, **limits):
        captured["bash"] = limits
        return "ok", {}

    for name in ("read", "write", "edit", "apply_patch", "grep", "find", "ls"):
        monkeypatch.setattr(f"executor.app.{name}", fake(name))
    monkeypatch.setattr("executor.app.bash", fake_bash)
    checkpoint = {"max_checkpoint_files": 170, "max_checkpoint_bytes": 1_000}
    requests = {
        "read": {"path": "x", "max_bytes": 999},
        "write": {"path": "x", "content": "x"},
        "edit": {"path": "x", "old_str": "x", "new_str": "y"},
        "apply_patch": {"operations": []},
        "bash": {"command": "true"},
        "grep": {"query": "x", "max_results": 999},
        "find": {"max_results": 999},
        "ls": {"max_results": 999},
    }
    for name, arguments in requests.items():
        result = asyncio.run(service.execute(ToolRequest(name, "run", name, "agent", arguments)))
        assert result.ok, result.to_dict()
    assert captured == {
        "read": {"max_bytes": 100},
        "write": {"max_bytes": 110, "max_staging_bytes": 10_000, **checkpoint},
        "edit": {"max_target_bytes": 120, "max_result_bytes": 130, **checkpoint},
        "apply_patch": {"max_operations": 9, "max_patch_bytes": 135, "max_write_bytes": 110, "max_edit_target_bytes": 120, "max_edit_result_bytes": 130, "max_staging_bytes": 10_000, **checkpoint},
        "bash": {"max_seconds": 180, "max_output": 140, "max_command_bytes": 160, "max_stdin_bytes": 150, **checkpoint},
        "grep": {"max_scan_bytes": 190, "max_output_bytes": 195, "max_results": 3, "max_seconds": 7},
        "find": {"max_results": 4, "max_seconds": 7},
        "ls": {"max_results": 5, "max_seconds": 7},
    }
