from __future__ import annotations

import asyncio
import json

from server.agent import runtime as runtime_module
from server.agent.budgets import (
    EXTENDED_CODING_PROFILE,
    LARGE_CODING_PROFILE,
    STANDARD_CODING_PROFILE,
    BudgetExceededError,
    RunBudget,
)
from server.agent.runtime import AgentRuntime
from server.openrouter.agent import AgentTurn
from shared.requests import AgentRunRequest
from shared.tools import ToolResult


class FakeExecutor:
    async def execute(self, request):
        return ToolResult(request.request_id, True, output=f"read {request.arguments.get('path', '')}")


class FakeApprovals:
    async def wait(self, *args, **kwargs):
        raise AssertionError("approval should not be requested")


def make_request() -> AgentRunRequest:
    return AgentRunRequest(
        model="parent-model",
        messages=({"role": "user", "content": "investigate this"},),
        mode="agent",
        workspace_id="workspace",
        session_id="session-1",
    )


async def record_event(*args):
    return None


def make_runtime() -> AgentRuntime:
    runtime = AgentRuntime(
        FakeExecutor(),
        FakeApprovals(),
        record_event,
    )
    runtime._api_keys["run-1"] = "api-key"
    return runtime


def test_investigator_prompt_explains_plan_harness_and_budget(monkeypatch):
    captured = {}

    async def fake_agent_turn(*args, **kwargs):
        captured["messages"] = args[1]
        captured["allowed_tools"] = kwargs["allowed_tools"]
        return AgentTurn(
            content="The answer is supported by the inspected files.",
            tool_calls=(),
            message={"role": "assistant", "content": "The answer is supported by the inspected files."},
            usage={"total_tokens": 8},
        )

    monkeypatch.setattr(runtime_module, "agent_turn", fake_agent_turn)
    runtime = make_runtime()
    messages = []
    parent_budget = RunBudget(100, 600, 1.0, 100, "test")

    asyncio.run(
        runtime._investigate_repository(
            "run-1",
            make_request(),
            "investigate-1",
            {"arguments": json.dumps({"query": "find the relevant implementation"})},
            messages,
            parent_budget,
        )
    )

    system = captured["messages"][0]["content"]
    assert "bounded plan-mode harness" in system
    assert "read, grep, find, and ls" in system
    assert "36 tool calls" in system
    assert "36 iterations" in system
    assert "stop researching once you have enough evidence" in system
    assert "final available tool call" in system
    assert captured["allowed_tools"] == {"read", "grep", "find", "ls"}


def test_investigator_reserves_final_budget_for_synthesis(monkeypatch):
    monkeypatch.setattr(runtime_module, "INVESTIGATION_MAX_ITERATIONS", 2)
    monkeypatch.setattr(runtime_module, "INVESTIGATION_MAX_TOOL_CALLS", 2)
    calls = []

    async def fake_agent_turn(*args, **kwargs):
        calls.append({"messages": args[1], "allowed_tools": kwargs["allowed_tools"]})
        if len(calls) == 1:
            return AgentTurn(
                content="",
                tool_calls=({"id": "read-1", "function": {"name": "read", "arguments": '{"path":"README.md"}'}},),
                message={"role": "assistant", "content": ""},
                usage={"total_tokens": 8},
            )
        return AgentTurn(
            content="README documents the behavior.",
            tool_calls=(),
            message={"role": "assistant", "content": "README documents the behavior."},
            usage={"total_tokens": 8},
        )

    monkeypatch.setattr(runtime_module, "agent_turn", fake_agent_turn)
    runtime = make_runtime()
    messages = []
    parent_budget = RunBudget(100, 600, 1.0, 100, "test")

    asyncio.run(
        runtime._investigate_repository(
            "run-1",
            make_request(),
            "investigate-1",
            {"arguments": json.dumps({"query": "find the relevant implementation"})},
            messages,
            parent_budget,
        )
    )

    assert len(calls) == 2
    assert calls[0]["allowed_tools"] == {"read", "grep", "find", "ls"}
    assert calls[1]["allowed_tools"] == set()
    assert any(
        message["role"] == "system" and "Stop repository exploration now" in message["content"]
        for message in calls[1]["messages"]
    )
    assert messages[-1]["role"] == "tool"
    result = json.loads(messages[-1]["content"])
    assert result["summary"] == "README documents the behavior."
    assert result["truncated"] is True


def test_investigation_rejects_hallucinated_tool_and_recovers(monkeypatch):
    calls = []

    async def fake_agent_turn(*args, **kwargs):
        calls.append(args[1])
        if len(calls) == 1:
            return AgentTurn(
                content="",
                tool_calls=({"id": "glob-1", "function": {"name": "glob", "arguments": '{"pattern":"*.py"}'}},),
                message={"role": "assistant", "content": ""},
                usage={"total_tokens": 8},
            )
        if len(calls) == 2:
            return AgentTurn(
                content="",
                tool_calls=({"id": "read-1", "function": {"name": "read", "arguments": '{"path":"README.md"}'}},),
                message={"role": "assistant", "content": ""},
                usage={"total_tokens": 8},
            )
        return AgentTurn(
            content="Recovered after the invalid tool call.",
            tool_calls=(),
            message={"role": "assistant", "content": "Recovered after the invalid tool call."},
            usage={"total_tokens": 8},
        )

    monkeypatch.setattr(runtime_module, "agent_turn", fake_agent_turn)
    runtime = make_runtime()
    executed = []

    async def fake_execute(tool):
        executed.append(tool.tool)
        return ToolResult(tool.request_id, True, output="README contents")

    runtime.executor.execute = fake_execute
    events = []

    async def append_event(*args):
        events.append(args[1:])

    runtime.append = append_event
    messages = []
    parent_budget = RunBudget(100, 600, 1.0, 100, "test")

    asyncio.run(
        runtime._investigate_repository(
            "run-1",
            make_request(),
            "investigate-1",
            {"arguments": json.dumps({"query": "find the relevant implementation"})},
            messages,
            parent_budget,
        )
    )

    assert executed == ["read"]
    assert len(calls) == 3
    assert any("investigation.tool_not_permitted" in message["content"] for message in calls[1] if message.get("role") == "tool")
    event_types = [event[0] for event in events]
    assert event_types.count("subagent.tool_call") == 2
    assert event_types.count("subagent.tool_result") == 2
    assert not any(event_type == "subagent.failed" for event_type in event_types)
    result = json.loads(messages[-1]["content"])
    assert result["summary"] == "Recovered after the invalid tool call."


def test_investigation_rejects_malformed_and_non_object_json_arguments(monkeypatch):
    calls = []

    async def fake_agent_turn(*args, **kwargs):
        calls.append(args[1])
        if len(calls) == 1:
            return AgentTurn(
                content="",
                tool_calls=({"id": "read-bad-1", "function": {"name": "read", "arguments": '{not-json'}},),
                message={"role": "assistant", "content": ""},
                usage={"total_tokens": 8},
            )
        if len(calls) == 2:
            return AgentTurn(
                content="",
                tool_calls=({"id": "read-bad-2", "function": {"name": "read", "arguments": '["README.md"]'}},),
                message={"role": "assistant", "content": ""},
                usage={"total_tokens": 8},
            )
        return AgentTurn(
            content="Recovered after invalid arguments.",
            tool_calls=(),
            message={"role": "assistant", "content": "Recovered after invalid arguments."},
            usage={"total_tokens": 8},
        )

    monkeypatch.setattr(runtime_module, "agent_turn", fake_agent_turn)
    runtime = make_runtime()
    executed = []

    async def fake_execute(tool):
        executed.append(tool.tool)
        return ToolResult(tool.request_id, True, output="unexpected execution")

    runtime.executor.execute = fake_execute
    events = []

    async def append_event(*args):
        events.append(args[1:])

    runtime.append = append_event
    messages = []
    parent_budget = RunBudget(100, 600, 1.0, 100, "test")

    asyncio.run(
        runtime._investigate_repository(
            "run-1",
            make_request(),
            "investigate-1",
            {"arguments": json.dumps({"query": "find the relevant implementation"})},
            messages,
            parent_budget,
        )
    )

    assert executed == []
    assert len(calls) == 3
    assert any("investigation.invalid_tool_arguments" in message["content"] for message in calls[1] if message.get("role") == "tool")
    assert any("investigation.invalid_tool_arguments" in message["content"] for message in calls[2] if message.get("role") == "tool")
    event_types = [event[0] for event in events]
    assert event_types.count("subagent.tool_call") == 2
    assert event_types.count("subagent.tool_result") == 2
    assert not any(event_type == "subagent.failed" for event_type in event_types)
    result = json.loads(messages[-1]["content"])
    assert result["summary"] == "Recovered after invalid arguments."


def test_investigation_budget_exhaustion_returns_partial_success(monkeypatch):
    async def fake_agent_turn(*args, **kwargs):
        return AgentTurn(
            content="",
            tool_calls=({"id": "read-1", "function": {"name": "read", "arguments": '{"path":"README.md"}'}},),
            message={"role": "assistant", "content": ""},
            usage={"total_tokens": 8},
        )

    def fail_investigation_tool_call(self):
        if self.profile_name == "investigation":
            raise BudgetExceededError("tool-call", 37, 36, "tool calls")
        self.tool_calls += 1
        self.check()

    monkeypatch.setattr(runtime_module, "agent_turn", fake_agent_turn)
    monkeypatch.setattr(runtime_module.RunBudget, "consume_tool_call", fail_investigation_tool_call)
    runtime = make_runtime()
    messages = []
    parent_budget = RunBudget(100, 600, 1.0, 100, "test")

    asyncio.run(
        runtime._investigate_repository(
            "run-1",
            make_request(),
            "investigate-1",
            {"arguments": json.dumps({"query": "find the relevant implementation"})},
            messages,
            parent_budget,
        )
    )

    result = json.loads(messages[-1]["content"])
    assert result["truncated"] is True
    assert result["budget_exhausted"] is True
    assert "partial repository analysis" in result["summary"]


def test_parent_and_investigation_budgets_are_tripled():
    assert (STANDARD_CODING_PROFILE.max_iterations, STANDARD_CODING_PROFILE.max_tool_calls) == (600, 900)
    assert (EXTENDED_CODING_PROFILE.max_iterations, EXTENDED_CODING_PROFILE.max_tool_calls) == (1_200, 1_800)
    assert (LARGE_CODING_PROFILE.max_iterations, LARGE_CODING_PROFILE.max_tool_calls) == (1_800, 2_700)
    assert (runtime_module.INVESTIGATION_MAX_ITERATIONS, runtime_module.INVESTIGATION_MAX_TOOL_CALLS) == (36, 36)


def test_investigation_child_wall_time_is_capped_independently_of_parent(monkeypatch):
    events = []

    async def fake_agent_turn(*args, **kwargs):
        return AgentTurn(content="Done.", tool_calls=(), message={"role": "assistant", "content": "Done."}, usage={"total_tokens": 8})

    async def append_event(run_id, event_type, payload):
        events.append((event_type, payload))

    monkeypatch.setattr(runtime_module, "agent_turn", fake_agent_turn)
    runtime = AgentRuntime(FakeExecutor(), FakeApprovals(), append_event)
    runtime._api_keys["run-1"] = "api-key"
    parent_budget = RunBudget(100, 1_500, 1.0, 100, "test")

    asyncio.run(runtime._investigate_repository("run-1", make_request(), "investigate-1", {"arguments": json.dumps({"query": "q"})}, [], parent_budget))

    started = next(payload for kind, payload in events if kind == "subagent.started")
    assert started["budget"]["max_wall_seconds"] == runtime_module.INVESTIGATION_MAX_WALL_SECONDS
    assert runtime_module.INVESTIGATION_MAX_WALL_SECONDS == 300


def test_investigation_wall_seconds_never_exceeds_ceiling_for_any_parent_remaining_time():
    for remaining in (0, 5, 10, 299, 300, 301, 1_500, 5_400):
        parent = RunBudget(100, remaining, 1.0, 100, "test")
        wall = runtime_module.investigation_wall_seconds(parent)
        assert runtime_module.INVESTIGATION_MIN_WALL_SECONDS <= wall <= runtime_module.INVESTIGATION_MAX_WALL_SECONDS
        if runtime_module.INVESTIGATION_MIN_WALL_SECONDS < remaining < runtime_module.INVESTIGATION_MAX_WALL_SECONDS:
            assert wall <= remaining


class TurnExecutor(FakeExecutor):
    async def status(self):
        return {"workspace_id": "workspace", "environment": {"workspace_root": "/work", "limits": {}}}


def _investigate_calls(turn: int, count: int) -> tuple[dict, ...]:
    return tuple({"id": f"inv-{turn}-{index}", "function": {"name": "investigate_repository", "arguments": json.dumps({"query": f"question {index}"})}} for index in range(count))


def test_investigation_call_budget_resets_each_turn(monkeypatch):
    parent_turns = []
    final_messages = []

    async def fake_agent_turn(model, messages, api_key, mode, *args, **kwargs):
        if model != "parent-model":
            return AgentTurn(content="Found it.", tool_calls=(), message={"role": "assistant", "content": "Found it."}, usage={"total_tokens": 8})
        parent_turns.append(len(parent_turns) + 1)
        if len(parent_turns) == 1:
            calls = _investigate_calls(1, 5)
        elif len(parent_turns) == 2:
            calls = _investigate_calls(2, 4)
        else:
            final_messages.extend(messages)
            return AgentTurn(content="All done.", tool_calls=(), message={"role": "assistant", "content": "All done."}, usage={"total_tokens": 8})
        return AgentTurn(content="", tool_calls=calls, message={"role": "assistant", "content": "", "tool_calls": list(calls)}, usage={"total_tokens": 8})

    events = []

    async def append_event(run_id, event_type, payload):
        events.append((event_type, payload))

    monkeypatch.setattr(runtime_module, "agent_turn", fake_agent_turn)
    runtime = AgentRuntime(TurnExecutor(), FakeApprovals(), append_event)
    asyncio.run(runtime.run("run-1", make_request(), "api-key"))

    assert "run.failed" not in [kind for kind, _ in events]
    results = {message["tool_call_id"]: json.loads(message["content"]) for message in final_messages if message.get("role") == "tool"}
    turn_one = [results[f"inv-1-{index}"] for index in range(5)]
    turn_two = [results[f"inv-2-{index}"] for index in range(4)]
    assert all(item.get("summary") == "Found it." for item in turn_one[:4])
    assert "Investigation call budget exhausted (4 calls per turn)." == turn_one[4]["error"]
    assert all(item.get("summary") == "Found it." for item in turn_two)
    assert runtime._investigation_calls == {}
