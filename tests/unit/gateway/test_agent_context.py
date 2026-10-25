from __future__ import annotations

import asyncio

from server.agent import runtime as runtime_module
from server.agent.context import compact_agent_context, estimate_message_tokens
from server.agent.runtime import AgentRuntime
from server.openrouter.agent import AgentTurn
from shared.requests import AgentRunRequest


def _turn(index: int) -> list[dict]:
    call = {
        "id": f"c{index}",
        "type": "function",
        "function": {"name": "read", "arguments": '{"path":"a.py"}'},
    }
    return [
        {"role": "assistant", "content": f"step {index}", "tool_calls": [call]},
        {"role": "tool", "tool_call_id": f"c{index}", "content": "x" * 5_000},
    ]


def test_small_context_is_returned_as_an_unchanged_copy() -> None:
    messages = [{"role": "user", "content": "hi"}]
    compacted = compact_agent_context(messages, 1_000)
    assert compacted == messages and compacted[0] is not messages[0]


def test_large_context_is_bounded_without_orphaning_tool_results() -> None:
    messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "go"}]
    for index in range(12):
        messages.extend(_turn(index))
    compacted = compact_agent_context(messages, 4_000)
    assert estimate_message_tokens(compacted) <= 4_000
    assert compacted[0] == {"role": "system", "content": "rules"}
    call_ids = {call["id"] for item in compacted for call in item.get("tool_calls") or ()}
    assert all(item["tool_call_id"] in call_ids for item in compacted if item["role"] == "tool")
    assert compacted[-1]["role"] == "tool" and compacted[-1]["tool_call_id"] == "c11"


def test_resuming_a_legacy_snapshot_moves_it_into_the_cache_stable_layout(monkeypatch) -> None:
    sent: list[list[dict]] = []

    class Executor:
        async def status(self):
            return {
                "workspace_id": "workspace",
                "environment": {"workspace_root": ".", "limits": {}},
            }

    async def fake_agent_turn(model, messages, *args, **kwargs):
        sent.append(list(messages))
        return AgentTurn(
            "done", (), {"role": "assistant", "content": "done"}, {"total_tokens": 1}
        )

    async def append(*_args):
        return None

    monkeypatch.setattr(runtime_module, "agent_turn", fake_agent_turn)
    request = AgentRunRequest(
        model="m",
        messages=({"role": "user", "content": "go"},),
        mode="plan",
        workspace_id="workspace",
    )
    saved = [
        {"role": "system", "content": "LOCAL EXECUTOR CONTEXT (saved before the pause)"},
        {"role": "user", "content": "go"},
    ]

    asyncio.run(
        AgentRuntime(Executor(), None, append).run(
            "run-1", request, "key", initial_messages=saved, resumed=True
        )
    )

    contents = [str(item.get("content") or "") for item in sent[0]]
    assert not any(item.startswith("LOCAL EXECUTOR CONTEXT") for item in contents)
    assert [item["role"] for item in sent[0]] == ["system", "system", "user", "user"]
    assert contents[1].startswith("SESSION CONTEXT")
    assert contents[2] == "go" and contents[3].startswith("RUN NOTES")
