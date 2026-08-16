from __future__ import annotations

import asyncio
from pathlib import Path

from executor.app import ExecutorService
from executor.config import ExecutorConfig
from server.openrouter.agent import LOCAL_TOOL_SCHEMAS
from shared.tools import AGENT_TOOLS, PLAN_TOOLS, TOOL_NAMES, ToolRequest

CANONICAL = ("read", "write", "edit", "bash", "grep", "find", "ls")
READ_ONLY = frozenset({"read", "grep", "find", "ls"})


def _config(tmp_path: Path) -> ExecutorConfig:
    return ExecutorConfig(
        source_root=tmp_path / "source",
        work_root=tmp_path / "work",
        socket_path=tmp_path / "executor.sock",
        token="x" * 32,
        workspace_id="phase2-test",
        max_read_bytes=100,
        max_write_bytes=110,
        max_edit_target_bytes=120,
        max_edit_result_bytes=130,
        max_bash_output_bytes=140,
        max_bash_stdin_bytes=150,
        max_command_bytes=160,
        max_checkpoint_bytes=1_000,
        max_staging_bytes=10_000,
        max_staged_files=170,
        max_command_seconds=180,
        max_search_results=3,
        max_find_results=4,
        max_ls_results=5,
        max_grep_scan_bytes=190,
    )


def test_agent_and_plan_contracts_are_exactly_canonical() -> None:
    schema_names = tuple(item["function"]["name"] for item in LOCAL_TOOL_SCHEMAS)
    assert schema_names == CANONICAL
    assert TOOL_NAMES == frozenset(CANONICAL)
    assert AGENT_TOOLS == TOOL_NAMES
    assert PLAN_TOOLS == READ_ONLY
    assert {name for name in CANONICAL if name not in READ_ONLY} == {"write", "edit", "bash"}
    assert all(item["function"]["parameters"]["additionalProperties"] is False for item in LOCAL_TOOL_SCHEMAS)


def test_model_schema_hard_maxima_match_configuration_ceilings() -> None:
    schemas = {item["function"]["name"]: item["function"]["parameters"]["properties"] for item in LOCAL_TOOL_SCHEMAS}
    assert schemas["read"]["max_bytes"]["maximum"] == ExecutorConfig.HARD_CEILINGS["max_read_bytes"]
    assert schemas["bash"]["timeout_seconds"]["maximum"] == ExecutorConfig.HARD_CEILINGS["max_command_seconds"]
    assert schemas["bash"]["stdin"]["maxLength"] == ExecutorConfig.HARD_CEILINGS["max_bash_stdin_bytes"]
    assert schemas["grep"]["max_results"]["maximum"] == ExecutorConfig.HARD_CEILINGS["max_search_results"]
    assert schemas["find"]["max_results"]["maximum"] == ExecutorConfig.HARD_CEILINGS["max_find_results"]
    assert schemas["ls"]["max_results"]["maximum"] == ExecutorConfig.HARD_CEILINGS["max_ls_results"]


def test_dispatch_passes_only_operation_specific_effective_limits(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    source.mkdir()
    config = _config(tmp_path)
    service = ExecutorService(config)
    captured: dict[str, object] = {}

    def fake_read(root, arguments, *, max_bytes):
        captured["read"] = max_bytes
        return "ok", {}

    def fake_write(root, arguments, *, max_bytes, max_checkpoint_files, max_checkpoint_bytes):
        captured["write"] = (max_bytes, max_checkpoint_files, max_checkpoint_bytes)
        return "ok", {}

    def fake_edit(root, arguments, *, max_target_bytes, max_result_bytes, max_checkpoint_files, max_checkpoint_bytes):
        captured["edit"] = (max_target_bytes, max_result_bytes, max_checkpoint_files, max_checkpoint_bytes)
        return "ok", {}

    async def fake_bash(request_id, root, arguments, *, max_seconds, max_output, max_command_bytes, max_stdin_bytes, max_checkpoint_files, max_checkpoint_bytes):
        captured["bash"] = (max_seconds, max_output, max_command_bytes, max_stdin_bytes, max_checkpoint_files, max_checkpoint_bytes)
        return "ok", {}

    def fake_grep(root, arguments, *, max_bytes, max_results):
        captured["grep"] = (max_bytes, max_results)
        return "ok", {}

    def fake_find(root, arguments, *, max_results):
        captured["find"] = max_results
        return "ok", {}

    def fake_ls(root, arguments, *, max_results):
        captured["ls"] = max_results
        return "ok", {}

    monkeypatch.setattr("executor.app.read", fake_read)
    monkeypatch.setattr("executor.app.write", fake_write)
    monkeypatch.setattr("executor.app.edit", fake_edit)
    monkeypatch.setattr("executor.app.bash", fake_bash)
    monkeypatch.setattr("executor.app.grep", fake_grep)
    monkeypatch.setattr("executor.app.find", fake_find)
    monkeypatch.setattr("executor.app.ls", fake_ls)

    requests = (
        ToolRequest("read", "run", "read", "agent", {"path": "x", "max_bytes": 999}),
        ToolRequest("write", "run", "write", "agent", {"path": "x", "content": "x"}),
        ToolRequest("edit", "run", "edit", "agent", {"path": "x", "old_str": "x", "new_str": "y"}),
        ToolRequest("bash", "run", "bash", "agent", {"command": "true"}),
        ToolRequest("grep", "run", "grep", "agent", {"query": "x", "max_results": 999}),
        ToolRequest("find", "run", "find", "agent", {"max_results": 999}),
        ToolRequest("ls", "run", "ls", "agent", {"max_results": 999}),
    )
    for request in requests:
        result = asyncio.run(service.execute(request))
        assert result.ok, result.to_dict()

    assert captured["read"] == 100
    assert captured["write"] == (110, 170, 1_000)
    assert captured["edit"] == (120, 130, 170, 1_000)
    assert captured["bash"] == (180, 140, 160, 150, 170, 1_000)
    assert captured["grep"] == (190, 3)
    assert captured["find"] == 4
    assert captured["ls"] == 5
