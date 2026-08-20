from __future__ import annotations

import asyncio

from server.agent.budgets import RunBudget
from server.agent.runtime import AgentRuntime
from server.openrouter.agent import AgentTurn
from shared.requests import AgentRunRequest
from shared.tools import ToolResult


def test_repository_investigation_uses_request_model(monkeypatch) -> None:
    seen_models: list[str] = []

    async def fake_agent_turn(model, *args, **kwargs):
        seen_models.append(model)
        return AgentTurn(
            content="investigation complete",
            tool_calls=(),
            message={"role": "assistant", "content": "investigation complete"},
            usage={},
        )

    class FakeExecutor:
        async def execute(self, _tool):
            return ToolResult("child", True, output="ok", data={})

    async def append(*_args, **_kwargs):
        return None

    monkeypatch.setattr("server.agent.runtime.agent_turn", fake_agent_turn)
    runtime = AgentRuntime(FakeExecutor(), object(), append)
    runtime._api_keys["run"] = "test-key"
    runtime._tool_result_bytes["run"] = 0

    request = AgentRunRequest(
        model="vendor/manager",
        messages=({"role": "user", "content": "Investigate this repository."},),
        mode="agent",
        workspace_id="workspace",
        investigation_model_id="xiaomi/mimo-v2.5",
    )
    messages: list[dict[str, object]] = []
    budget = RunBudget(10, 120, 1.0, 10, "test")

    result = asyncio.run(
        runtime._investigate_repository(
            "run", request, "parent", {"arguments": '{"query":"find the entry point"}'}, messages, budget
        )
    )

    assert result is False
    assert seen_models == ["xiaomi/mimo-v2.5"]
