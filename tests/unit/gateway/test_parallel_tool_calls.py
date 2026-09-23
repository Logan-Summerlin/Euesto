from __future__ import annotations

import asyncio
import json
import time

from server.agent import runtime as runtime_module
from server.agent.runtime import AgentRuntime, tool_call_groups
from server.openrouter.agent import AgentTurn
from shared.requests import AgentRunRequest
from shared.tools import PARALLEL_SAFE_TOOLS, ToolResult

DELAY = 0.3


class DelayedExecutor:
    """Stub executor: every tool sleeps, and the call log records start/finish order."""

    def __init__(self) -> None:
        self.log: list[tuple[str, str, float]] = []
        self.active = 0
        self.max_active = 0

    async def status(self):
        return {"workspace_id": "workspace", "environment": {"workspace_root": ".", "limits": {}}}

    async def execute(self, request):
        if request.arguments.get("path") == "AGENTS.md":
            return ToolResult(request.request_id, False, output="missing", error_code="path.missing")
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.log.append(("start", request.request_id, time.perf_counter()))
        try:
            await asyncio.sleep(DELAY)
        finally:
            self.active -= 1
        self.log.append(("end", request.request_id, time.perf_counter()))
        data = {"checkpoint_id": f"cp-{request.request_id}", "workspace_status": {"staged": True}} if request.tool in {"write", "edit", "apply_patch"} else {}
        return ToolResult(request.request_id, True, output=f"{request.tool}:{request.request_id}", data=data)


class NoApprovals:
    async def wait(self, *args, **kwargs):
        raise AssertionError("approval should not be requested")


def _call(call_id: str, name: str, arguments: dict) -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _run(calls: tuple[dict, ...], monkeypatch, *, policy: str = "auto") -> tuple[DelayedExecutor, list[dict], float]:
    executor = DelayedExecutor()
    final: list[dict] = []
    turns = []

    async def fake_agent_turn(model, messages, api_key, mode, *args, **kwargs):
        turns.append(1)
        if len(turns) == 1:
            return AgentTurn("", calls, {"role": "assistant", "content": "", "tool_calls": list(calls)}, {"total_tokens": 1})
        final.extend(messages)
        return AgentTurn("done", (), {"role": "assistant", "content": "done"}, {"total_tokens": 1})

    async def append(*_args):
        return None

    async def no_publish(*_args):
        return None

    monkeypatch.setattr(runtime_module, "agent_turn", fake_agent_turn)
    runtime = AgentRuntime(executor, NoApprovals(), append)
    runtime._offer_publish = no_publish
    request = AgentRunRequest(model="m", messages=({"role": "user", "content": "go"},), mode="agent", workspace_id="workspace", approval_policy=policy)
    started = time.perf_counter()
    asyncio.run(runtime.run("run-1", request, "key"))
    return executor, final, time.perf_counter() - started


def test_grouping_keeps_mutations_serialized_and_merges_consecutive_reads() -> None:
    calls = [
        _call("a", "read", {"path": "a"}), _call("b", "grep", {"query": "x"}),
        _call("c", "write", {"path": "c", "content": "x"}),
        _call("d", "ls", {}), _call("e", "status", {}), _call("f", "find", {}),
        _call("g", "bash", {"command": "true"}), _call("h", "investigate_repository", {"query": "q"}),
        _call("i", "read", {"path": "i"}),
    ]
    groups = [[item["id"] for item in group] for group in tool_call_groups(calls)]
    assert groups == [["a", "b"], ["c"], ["d", "e", "f"], ["g"], ["h"], ["i"]]
    assert PARALLEL_SAFE_TOOLS == {"read", "grep", "find", "ls", "status"}


def test_independent_read_only_calls_complete_in_max_not_sum_of_latencies(monkeypatch) -> None:
    calls = tuple(_call(f"r{index}", name, {"path": f"f{index}.txt"} if name == "read" else {"query": "x"} if name == "grep" else {}) for index, name in enumerate(("read", "grep", "find", "ls", "read")))
    executor, final, _elapsed = _run(calls, monkeypatch)
    reads = [entry for entry in executor.log if entry[1].startswith("r")]
    starts = [stamp for kind, _id, stamp in reads if kind == "start"]
    ends = [stamp for kind, _id, stamp in reads if kind == "end"]
    # All five were in flight together: the whole batch took about one latency, not five.
    assert executor.max_active == 5
    assert max(ends) - min(starts) < DELAY * 2.5
    tool_messages = [message["tool_call_id"] for message in final if message.get("role") == "tool"]
    assert tool_messages == [f"r{index}" for index in range(5)]


def test_mixed_batch_applies_mutations_in_call_order_between_read_groups(monkeypatch) -> None:
    calls = (
        _call("r1", "read", {"path": "a.txt"}),
        _call("r2", "grep", {"query": "x"}),
        _call("w1", "write", {"path": "a.txt", "content": "new"}),
        _call("r3", "read", {"path": "a.txt"}),
        _call("r4", "status", {}),
        _call("e1", "edit", {"path": "a.txt", "old_str": "new", "new_str": "newer"}),
    )
    executor, final, _elapsed = _run(calls, monkeypatch)
    order = [(kind, call_id) for kind, call_id, _stamp in executor.log if call_id in {"r1", "r2", "w1", "r3", "r4", "e1"}]
    position = {entry: index for index, entry in enumerate(order)}
    # The write starts only after both earlier reads finish, and later reads start after it ends.
    assert position[("start", "w1")] > max(position[("end", "r1")], position[("end", "r2")])
    assert min(position[("start", "r3")], position[("start", "r4")]) > position[("end", "w1")]
    assert position[("start", "e1")] > max(position[("end", "r3")], position[("end", "r4")])
    # Mutations never overlap anything else.
    assert executor.max_active <= 2
    tool_messages = [message["tool_call_id"] for message in final if message.get("role") == "tool"]
    assert tool_messages == ["r1", "r2", "w1", "r3", "r4", "e1"]
