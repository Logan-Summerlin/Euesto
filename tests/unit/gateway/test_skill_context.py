"""Active skills render only when the run's mode offers every tool they require."""

from __future__ import annotations

import asyncio

import pytest

from server.agent.budgets import resolve_budget_profile
from server.agent.runtime import AgentRuntime
from shared.requests import AgentRunRequest
from shared.tools import ToolResult

STATUS_SKILL = {
    "name": "review",
    "description": "Review staged work.",
    "instructions": "Call status before finishing.",
    "required_tools": ["status"],
    "scope": "global",
}


class NoInstructionsExecutor:
    async def execute(self, request):
        return ToolResult(
            request.request_id, False, output="missing", error_code="path.missing"
        )


async def _no_events(*_args) -> None:
    return None


def _context(mode: str) -> list[dict]:
    request = AgentRunRequest(
        model="model",
        messages=({"role": "user", "content": "hello"},),
        mode=mode,
        workspace_id="workspace",
        skills=(STATUS_SKILL,),
    )
    runtime = AgentRuntime(NoInstructionsExecutor(), object(), _no_events)
    return asyncio.run(
        runtime._session_layout(
            "run", request, list(request.messages), {}, resolve_budget_profile("coding")
        )
    )


def test_agent_mode_renders_a_skill_that_requires_status() -> None:
    messages = _context("agent")
    assert [item["role"] for item in messages] == ["system", "system", "user"]
    assert "## ACTIVE SKILLS\nSKILL [global] review" in messages[1]["content"]
    assert messages[-1] == {"role": "user", "content": "hello"}


def test_plan_mode_rejects_a_skill_that_requires_a_tool_plan_does_not_offer() -> None:
    with pytest.raises(ValueError, match="requires unavailable tools"):
        _context("plan")
