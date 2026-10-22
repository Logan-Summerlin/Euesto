"""Streamed agent turns and their timeouts (harness plan 3.1)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from server.agent import runtime as runtime_module
from server.agent.runtime import AgentRuntime
from server.openrouter.agent import AgentTurn, agent_turn
from server.openrouter.errors import ProviderError
from tests.unit.gateway.test_harness_loop import (
    AllowApprovals,
    ScriptedExecutor,
    make_request,
)


def sse(*chunks: dict | str) -> list[bytes]:
    lines = [b": OPENROUTER PROCESSING\n\n"]
    for chunk in chunks:
        data = chunk if isinstance(chunk, str) else json.dumps(chunk)
        lines.append(f"data: {data}\n\n".encode())
    return lines


def delta(**fields) -> dict:
    return {"choices": [{"delta": fields}]}


def streaming_transport(parts, *, pause: float = 0.0, pause_after: int = 1 << 30, sent=None):
    async def body():
        for index, part in enumerate(parts):
            if index == pause_after:
                await asyncio.sleep(pause)
            yield part

    def handler(request: httpx.Request) -> httpx.Response:
        if sent is not None:
            sent.append(json.loads(request.content))
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=body()
        )

    return httpx.MockTransport(handler)


def turn(transport, **kwargs) -> AgentTurn:
    return asyncio.run(
        agent_turn(
            "some/model",
            [{"role": "user", "content": "q"}],
            "key",
            "agent",
            transport=transport,
            **kwargs,
        )
    )


def test_tool_calls_split_across_chunks_are_assembled_in_order() -> None:
    parts = sse(
        delta(content="Let me "),
        delta(content="look."),
        delta(
            tool_calls=[
                {"index": 0, "id": "c1", "type": "function", "function": {"name": "read"}}
            ]
        ),
        delta(tool_calls=[{"index": 0, "function": {"arguments": '{"pa'}}]),
        delta(
            tool_calls=[
                {"index": 1, "id": "c2", "function": {"name": "grep", "arguments": ""}},
                {"index": 0, "function": {"arguments": 'th":"a.py"}'}},
            ]
        ),
        delta(tool_calls=[{"index": 1, "function": {"arguments": '{"query":"x"}'}}]),
        delta(reasoning="thin", reasoning_details=[{"type": "reasoning.text", "text": "thin"}]),
        delta(reasoning="king", reasoning_details=[{"type": "reasoning.text", "text": "king"}]),
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}], "provider": "P"},
        {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 4}, "model": "m"},
        "[DONE]",
    )
    sent: list[dict] = []
    seen: list[str] = []

    async def on_delta(text: str) -> None:
        seen.append(text)

    result = turn(streaming_transport(parts, sent=sent), on_delta=on_delta)
    assert sent[0]["stream"] is True
    assert result.content == "Let me look." and seen == ["Let me ", "look."]
    assert [call["function"] for call in result.tool_calls] == [
        {"name": "read", "arguments": '{"path":"a.py"}'},
        {"name": "grep", "arguments": '{"query":"x"}'},
    ]
    assert [call["id"] for call in result.tool_calls] == ["c1", "c2"]
    assert result.message["reasoning"] == "thinking"
    assert result.message["reasoning_details"] == [
        {"type": "reasoning.text", "text": "thinking"}
    ]
    assert result.finish_reason == "tool_calls" and result.provider == "P"
    assert result.usage["prompt_tokens"] == 10 and result.model == "m"


def test_an_idle_stream_times_out_as_a_retryable_error() -> None:
    parts = sse(delta(content="a"), delta(content="b"), "[DONE]")
    transport = streaming_transport(parts, pause=0.5, pause_after=2)
    with pytest.raises(ProviderError) as caught:
        turn(transport, idle_timeout=0.05, turn_timeout=5)
    assert caught.value.code == "provider.timeout" and caught.value.retryable
    assert "idle" in str(caught.value)


def test_the_turn_ceiling_bounds_a_slow_but_live_stream() -> None:
    parts = sse(*[delta(content=str(index)) for index in range(40)], "[DONE]")

    async def body():
        for part in parts:
            await asyncio.sleep(0.02)
            yield part

    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=body()
        )
    )
    with pytest.raises(ProviderError) as caught:
        turn(transport, idle_timeout=1, turn_timeout=0.2)
    assert caught.value.code == "provider.timeout" and "ceiling" in str(caught.value)


def test_an_error_chunk_mid_stream_is_retryable() -> None:
    parts = sse(delta(content="a"), {"error": {"message": "upstream reset"}})
    with pytest.raises(ProviderError) as caught:
        turn(streaming_transport(parts))
    assert caught.value.retryable and "upstream reset" in str(caught.value)


def test_a_stream_without_any_message_is_retryable() -> None:
    with pytest.raises(ProviderError) as caught:
        turn(streaming_transport(sse("[DONE]")))
    assert caught.value.code == "provider.incomplete_stream" and caught.value.retryable


def test_cancellation_mid_stream_stops_the_turn() -> None:
    closed: list[bool] = []

    async def body():
        try:
            yield sse(delta(content="partial"))[1]
            await asyncio.sleep(10)
            yield b"data: [DONE]\n\n"
        finally:
            closed.append(True)

    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=body()
        )
    )
    received: list[str] = []

    async def on_delta(text: str) -> None:
        received.append(text)

    async def scenario() -> None:
        task = asyncio.create_task(
            agent_turn(
                "m",
                [{"role": "user", "content": "q"}],
                "key",
                "agent",
                transport=transport,
                on_delta=on_delta,
            )
        )
        while not received:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert received == ["partial"] and closed == [True]


def test_streamed_text_is_journaled_at_a_bounded_rate(monkeypatch) -> None:
    monkeypatch.setattr(runtime_module, "STREAM_FLUSH_SECONDS", 3600)
    monkeypatch.setattr(runtime_module, "STREAM_FLUSH_CHARS", 10)
    monkeypatch.setattr(runtime_module, "STREAM_MAX_CHARS", 25)
    received: list[dict] = []

    async def streaming_provider(model, messages, api_key, mode, *args, **kwargs):
        received.append(kwargs)
        on_delta = kwargs["on_delta"]
        for _ in range(8):
            await on_delta("abcd")
        text = "abcd" * 8
        return AgentTurn(text, (), {"role": "assistant", "content": text}, {}, "stop")

    monkeypatch.setattr(runtime_module, "agent_turn", streaming_provider)
    events: list = []

    async def append(_run_id, kind, payload):
        events.append((kind, payload))

    runtime = AgentRuntime(ScriptedExecutor(), AllowApprovals(), append)
    asyncio.run(runtime.run("run-1", make_request(), "key"))
    streamed = [payload for kind, payload in events if kind == "model.stream"]
    # Coalesced into 12-character events, capped at 25 characters per turn.
    assert [item["text"] for item in streamed] == ["abcdabcdabcd", "abcdabcdabcd", "a"]
    assert streamed[-1]["capped"] is True and {item["turn"] for item in streamed} == {1}
    assert received[0]["idle_timeout"] == 120 and received[0]["turn_timeout"] == 600
    assert ("model.delta", {"text": "abcd" * 8}) in events
