from __future__ import annotations

import asyncio
from types import SimpleNamespace

from server.agent import runtime as runtime_module
from server.agent.runtime import AgentRuntime
from server.openrouter.agent import AgentTurn
from shared.requests import AgentRunRequest
from shared.tools import ToolResult


class FakeExecutor:
    def __init__(self, manifest=None, mutation_result=None) -> None:
        self.manifest_value = manifest
        self.mutation_result = mutation_result
        self.executed = []

    async def status(self):
        return {"workspace_id": "workspace", "environment": {"workspace_root": "/work", "limits": {}}}

    async def execute(self, request):
        self.executed.append(request)
        if request.tool == "read":
            return ToolResult(request.request_id, True, output="")
        if self.mutation_result is not None:
            return self.mutation_result
        raise AssertionError(f"unexpected executor call: {request.tool}")

    async def cancel(self, request_id):
        raise AssertionError("cancel should not be called")

    async def manifest(self, run_id, approval_id):
        return self.manifest_value or SimpleNamespace(operations=())


class FakeApprovals:
    async def wait(self, *args, **kwargs):
        raise AssertionError("approval should not be requested")


def request(*, mode: str = "plan", approval_policy: str = "prompt") -> AgentRunRequest:
    return AgentRunRequest(
        model="test-model",
        messages=({"role": "user", "content": "do the task"},),
        mode=mode,
        workspace_id="workspace",
        approval_policy=approval_policy,
        session_id="session-1",
    )


async def record_event(events, run_id, event_type, payload):
    events.append((event_type, payload))


def test_final_agent_turn_emits_response_before_completion_and_persists_it(monkeypatch) -> None:
    async def fake_agent_turn(*args, **kwargs):
        return AgentTurn(
            content="Task complete.",
            tool_calls=(),
            message={"role": "assistant", "content": "Task complete."},
            usage={"input_tokens": 12, "output_tokens": 4, "total_tokens": 16},
        )

    monkeypatch.setattr(runtime_module, "agent_turn", fake_agent_turn)
    events = []
    saved = []
    runtime = AgentRuntime(
        FakeExecutor(),
        FakeApprovals(),
        lambda run_id, event_type, payload: record_event(events, run_id, event_type, payload),
        session_saver=lambda *args: saved.append(args),
    )

    asyncio.run(runtime.run("run-1", request(), "api-key"))

    event_types = [event[0] for event in events]
    assert "run.failed" not in event_types
    assert event_types.index("model.delta") < event_types.index("usage.updated")
    assert event_types.index("usage.updated") < event_types.index("run.completed")
    assert [payload["text"] for kind, payload in events if kind == "model.delta"] == ["Task complete."]
    assert len(saved) == 1
    assert saved[0][0:3] == ("session-1", "workspace", "plan")
    assert saved[0][4][-1] == {"role": "assistant", "content": "Task complete."}


def test_agent_completion_does_not_emit_publication_for_a_tool_free_turn_even_if_manifest_has_stale_changes(monkeypatch) -> None:
    async def fake_agent_turn(*args, **kwargs):
        return AgentTurn(
            content="No files need changing.",
            tool_calls=(),
            message={"role": "assistant", "content": "No files need changing."},
            usage={"total_tokens": 8},
        )

    manifest = SimpleNamespace(
        approval_id="approval-stale",
        operations=(SimpleNamespace(path="blackjack.py"),),
        to_dict=lambda: {"manifest_id": "stale", "operations": [{"path": "blackjack.py"}]},
    )
    monkeypatch.setattr(runtime_module, "agent_turn", fake_agent_turn)
    events = []
    runtime = AgentRuntime(
        FakeExecutor(manifest),
        FakeApprovals(),
        lambda run_id, event_type, payload: record_event(events, run_id, event_type, payload),
    )

    asyncio.run(runtime.run("run-stale", request(mode="agent", approval_policy="prompt"), "api-key"))

    assert not any(
        kind == "checkpoint.created" and "publish_manifest" in payload
        for kind, payload in events
    )
    assert "run.completed" in [kind for kind, _ in events]


def test_agent_completion_emits_desktop_publication_manifest_after_a_successful_mutation(monkeypatch) -> None:
    calls = 0

    async def fake_agent_turn(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return AgentTurn(
                content="",
                tool_calls=({"id": "write-1", "function": {"name": "write", "arguments": '{"path":"created.txt","content":"hello"}'}},),
                message={"role": "assistant", "content": ""},
                usage={"total_tokens": 8},
            )
        return AgentTurn(
            content="Implemented the change.",
            tool_calls=(),
            message={"role": "assistant", "content": "Implemented the change."},
            usage={"total_tokens": 8},
        )

    manifest = SimpleNamespace(
        approval_id="approval-1",
        operations=(SimpleNamespace(path="created.txt"),),
        to_dict=lambda: {"manifest_id": "manifest-1", "operations": [{"path": "created.txt"}]},
    )
    mutation_result = ToolResult(
        "write-1",
        True,
        output="created.txt written",
        data={"workspace_status": {"staged": True, "created": ["created.txt"]}, "checkpoint_id": "tool-checkpoint"},
    )
    monkeypatch.setattr(runtime_module, "agent_turn", fake_agent_turn)
    events = []
    runtime = AgentRuntime(
        FakeExecutor(manifest, mutation_result),
        FakeApprovals(),
        lambda run_id, event_type, payload: record_event(events, run_id, event_type, payload),
    )

    asyncio.run(runtime.run("run-2", request(mode="agent", approval_policy="auto"), "api-key"))

    publication_events = [
        payload for kind, payload in events
        if kind == "checkpoint.created" and "publish_manifest" in payload
    ]
    assert len(publication_events) == 1
    assert publication_events[0]["checkpoint_id"] == "approval-1"
    assert publication_events[0]["publish_manifest"]["manifest_id"] == "manifest-1"
    assert publication_events[0]["auto_publish"] is True

    completed_index = next(i for i, (kind, _) in enumerate(events) if kind == "run.completed")
    publication_index = next(
        i for i, (kind, payload) in enumerate(events)
        if kind == "checkpoint.created" and "publish_manifest" in payload
    )
    assert publication_index < completed_index
    assert "run.failed" not in [kind for kind, _ in events]
