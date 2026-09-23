from __future__ import annotations

from server.agent.context import compact_agent_context, estimate_message_tokens


def _turn(index: int) -> list[dict]:
    call = {"id": f"c{index}", "type": "function", "function": {"name": "read", "arguments": '{"path":"a.py"}'}}
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
