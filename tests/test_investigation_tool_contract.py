from __future__ import annotations

import asyncio
import json

from server.agent.budgets import RunBudget
from server.agent.runtime import AgentRuntime
from server.openrouter.agent import AgentTurn, LOCAL_TOOL_SCHEMAS
from shared.requests import AgentRunRequest
from shared.tools import ToolResult


def _investigation_schema() -> dict:
    return next(item for item in LOCAL_TOOL_SCHEMAS if item["function"]["name"] == "investigate_repository")


def test_investigation_repository_schema_only_accepts_query() -> None:
    schema = _investigation_schema()["function"]
    assert schema["parameters"]["required"] == ["query"]
    assert set(schema["parameters"]["properties"]) == {"query"}
    assert "path_hint" not in schema["parameters"]["properties"]
    assert "path hints" in schema["description"].lower()


def test_repository_investigation_uses_the_request_as_the_child_prompt(monkeypatch) -> None:
    prompts: list[str] = []

    async def fake_agent_turn(model, messages, *args, **kwargs):
        prompts.append(str(messages[-1]["content"]))
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
    query = "Find the entry point and explain how requests reach the agent runtime."

    result = asyncio.run(
        runtime._investigate_repository(
            "run", request, "parent", {"arguments": json.dumps({"query": query})}, messages, budget
        )
    )

    assert result is False
    assert len(prompts) == 1
    assert query in prompts[0]
    assert "Path hints:" not in prompts[0]
