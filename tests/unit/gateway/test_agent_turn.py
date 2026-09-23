from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from server.openrouter import agent as agent_module
from server.openrouter.agent import agent_turn
from server.openrouter.errors import ProviderError


def _serve(monkeypatch, handler) -> list[dict]:
    sent: list[dict] = []
    real_client = httpx.AsyncClient

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return handler(request)

    monkeypatch.setattr(agent_module.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs))
    return sent


def _turn(**kwargs):
    return asyncio.run(agent_turn("some/model", [{"role": "user", "content": "q"}], "key", "plan", **kwargs))


def _ok(message: dict) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": message}], "usage": {"total_tokens": 3}})


def test_empty_tool_allowance_declares_plan_tools_but_forbids_calls(monkeypatch):
    sent = _serve(monkeypatch, lambda _request: _ok({"content": "summary"}))
    turn = _turn(allowed_tools=set())
    assert turn.content == "summary"
    assert sent[0]["tool_choice"] == "none"
    assert {tool["function"]["name"] for tool in sent[0]["tools"]} == {"read", "grep", "find", "ls"}


def test_plan_allowance_keeps_auto_tool_choice(monkeypatch):
    sent = _serve(monkeypatch, lambda _request: _ok({"content": "x"}))
    _turn(allowed_tools={"read", "grep"})
    assert sent[0]["tool_choice"] == "auto"
    assert {tool["function"]["name"] for tool in sent[0]["tools"]} == {"read", "grep"}


def test_reasoning_is_preserved_for_the_next_turn(monkeypatch):
    details = [{"type": "reasoning.encrypted", "data": "opaque"}]
    call = {"id": "c1", "type": "function", "function": {"name": "read", "arguments": "{\"path\":\"a\"}"}}
    _serve(monkeypatch, lambda _request: _ok({"content": None, "reasoning": "thinking", "reasoning_details": details, "tool_calls": [call]}))
    turn = _turn()
    assert turn.message["reasoning_details"] == details
    assert turn.message["reasoning"] == "thinking"
    assert turn.message["tool_calls"][0]["id"] == "c1"


@pytest.mark.parametrize(("status", "retryable"), [(400, False), (401, False), (408, True), (429, True), (502, True)])
def test_http_errors_carry_provider_detail_and_retryability(monkeypatch, status, retryable):
    _serve(monkeypatch, lambda _request: httpx.Response(status, json={"error": {"message": "upstream said no"}}))
    with pytest.raises(ProviderError) as caught:
        _turn()
    assert caught.value.retryable is retryable
    assert f"({status})" in str(caught.value) and "upstream said no" in str(caught.value)


def test_error_body_with_ok_status_is_retryable(monkeypatch):
    _serve(monkeypatch, lambda _request: httpx.Response(200, json={"error": {"message": "provider disconnected"}}))
    with pytest.raises(ProviderError) as caught:
        _turn()
    assert caught.value.retryable is True
    assert "provider disconnected" in str(caught.value)
