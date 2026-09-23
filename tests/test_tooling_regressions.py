from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from server.openrouter.agent import LOCAL_TOOL_SCHEMAS
from shared.tools import AGENT_TOOLS, PLAN_TOOLS, TOOL_NAMES, ToolRequest

CANONICAL_TOOLS = ("read", "write", "edit", "apply_patch", "bash", "grep", "find", "ls", "status")
MODEL_TOOL_NAMES = CANONICAL_TOOLS + ("investigate_repository",)
READ_ONLY_TOOLS = frozenset({"read", "grep", "find", "ls"})
LEGACY_TOOL_NAMES = {
    "list_files",
    "read_file",
    "write_file",
    "edit_file",
    "search_files",
    "search_text",
    "inspect_workspace",
    "inspect_checkpoint",
    "patch",
    "run_command",
    "move_file",
    "copy_file",
    "restore_checkpoint",
    "move",
    "copy",
    "checkpoint",
    "restore",
}


def test_public_tool_contract_includes_scoped_investigation_tool() -> None:
    assert tuple(item["function"]["name"] for item in LOCAL_TOOL_SCHEMAS) == MODEL_TOOL_NAMES
    assert TOOL_NAMES == frozenset(MODEL_TOOL_NAMES)
    assert AGENT_TOOLS == TOOL_NAMES
    assert PLAN_TOOLS == READ_ONLY_TOOLS
    assert not LEGACY_TOOL_NAMES.intersection(TOOL_NAMES)
    assert not LEGACY_TOOL_NAMES.intersection(
        item["function"]["name"] for item in LOCAL_TOOL_SCHEMAS
    )


def test_plan_surface_is_read_only() -> None:
    for name in READ_ONLY_TOOLS:
        request = ToolRequest("request", "run", name, "plan", {})
        assert request.tool == name
    for name in CANONICAL_TOOLS:
        if name not in READ_ONLY_TOOLS:
            with pytest.raises(ValueError, match="Plan mode"):
                ToolRequest("request", "run", name, "plan", {})
    request = ToolRequest("request", "run", "investigate_repository", "agent", {})
    assert request.tool == "investigate_repository"


def test_removed_legacy_tools_are_rejected_by_the_request_contract() -> None:
    for name in LEGACY_TOOL_NAMES:
        with pytest.raises(ValueError, match="Unknown tool"):
            ToolRequest.from_dict({"request_id": "request", "run_id": "run", "tool": name, "mode": "agent", "arguments": {}})


class StatusOnlyExecutor:
    """Executor fake that answers status and fails if a tool is executed."""

    def __init__(self, environment: dict) -> None:
        self.environment = environment
        self.status_calls = 0

    async def status(self) -> dict:
        self.status_calls += 1
        return {"workspace_id": "workspace", "environment": self.environment}

    async def execute(self, _request):
        raise AssertionError("staging checks must use executor status, not a tool call")


def _executor_service(tmp_path: Path, environment: dict):
    from server.config import GatewayConfig
    from server.service import GatewayService

    socket = tmp_path / "executor.sock"
    socket.touch()
    service = GatewayService(GatewayConfig("t" * 43, tmp_path / "gateway.sqlite3", executor_socket=socket, executor_token="e" * 43, workspace_id="workspace"))
    service.executor = StatusOnlyExecutor(environment)
    return service


def test_gateway_status_advertises_canonical_local_tools(tmp_path: Path) -> None:
    service = _executor_service(tmp_path, {})
    try:
        local = [name for name in service.status().supported_tools if not name.startswith("openrouter:")]
    finally:
        asyncio.run(service.close())
    assert tuple(local) == CANONICAL_TOOLS
    assert not LEGACY_TOOL_NAMES.intersection(local)


def test_staging_inspection_uses_executor_status_not_a_removed_tool(tmp_path: Path) -> None:
    service = _executor_service(tmp_path, {"unpublished_changes": True, "agent_snapshot": {"file_count": 3}})

    async def scenario() -> dict:
        try:
            return await service.inspect_staging("workspace")
        finally:
            await service.close()

    result = asyncio.run(scenario())
    assert result["output"] == "Staging is dirty."
    assert result["data"]["file_count"] == 3
    assert service.executor.status_calls == 1


def test_auto_preflight_uses_executor_status_not_a_removed_tool(tmp_path: Path) -> None:
    from server.service import GatewayServiceError
    from shared.requests import AgentRunRequest

    service = _executor_service(tmp_path, {"unpublished_changes": True})
    service.configure_client_key("k" * 16)
    request = AgentRunRequest(model="m", messages=({"role": "user", "content": "go"},), mode="agent", workspace_id="workspace", approval_policy="auto", investigation_model_id=None)

    async def scenario() -> None:
        try:
            await service.start_agent(request)
        finally:
            await service.close()

    with pytest.raises(GatewayServiceError, match="clean staging"):
        asyncio.run(scenario())
    assert service.executor.status_calls == 1
