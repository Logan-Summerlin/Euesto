from __future__ import annotations

from pathlib import Path

import pytest

from server.openrouter.agent import LOCAL_TOOL_SCHEMAS
from shared.tools import AGENT_TOOLS, PLAN_TOOLS, TOOL_NAMES, ToolRequest

CANONICAL_TOOLS = ("read", "write", "edit", "bash", "grep", "find", "ls")
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
    "apply_patch",
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


def test_gateway_service_does_not_construct_removed_tools() -> None:
    source = Path("server/service.py").read_text(encoding="utf-8")
    offenders = sorted(
        name for name in LEGACY_TOOL_NAMES if f'"{name}"' in source
    )
    assert not offenders, (
        "server/service.py still contains removed model-facing tool names: "
        + ", ".join(offenders)
    )


def test_gateway_and_agent_tool_vocabularies_cannot_diverge() -> None:
    source = Path("server/openrouter/agent.py").read_text(encoding="utf-8")
    assert 'AGENT_TOOL_PROFILE = "pi-compatible"' in source
    assert '"inspect_workspace"' not in source
    assert '"run_command"' not in source
    assert '"apply_patch"' not in source


def test_gateway_status_advertises_canonical_local_tools() -> None:
    source = Path("server/service.py").read_text(encoding="utf-8")
    assert '("read", "write", "edit", "bash", "grep", "find", "ls")' in source
    assert '"investigate_repository"' not in source
    assert '"inspect_workspace"' not in source
    assert '"inspect_checkpoint"' not in source
    assert '"apply_patch"' not in source
    assert '"run_command"' not in source


def test_staging_inspection_uses_executor_status_not_a_removed_tool() -> None:
    source = Path("server/service.py").read_text(encoding="utf-8")
    start = source.index("    async def inspect_staging")
    end = source.index("    async def get_models", start)
    method = source[start:end]
    assert "await self.executor.status()" in method
    assert "ToolRequest(" not in method
    assert "inspect_workspace" not in method


def test_auto_preflight_uses_executor_status_not_a_removed_tool() -> None:
    source = Path("server/service.py").read_text(encoding="utf-8")
    start = source.index("    async def start_agent")
    end = source.index("    async def resume_agent", start)
    method = source[start:end]
    assert "await self.executor.status()" in method
    assert "inspect_workspace" not in method
    assert "auto-preflight" not in method
