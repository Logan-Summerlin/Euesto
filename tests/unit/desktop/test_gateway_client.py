from __future__ import annotations

import json

import httpx
import pytest

from src.gateway_client import GatewayClient, GatewayConnection, GatewayError, _iter_sse


def _gateway(monkeypatch, handler) -> tuple[GatewayClient, list[tuple[str, str]]]:
    """A client whose requests go to ``handler``; returns it and the (method, path) calls."""
    calls: list[tuple[str, str]] = []

    def record(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        return handler(request)

    gateway = GatewayClient(GatewayConnection(token="t" * 43))
    monkeypatch.setattr(
        gateway,
        "_client",
        lambda **_kwargs: httpx.Client(
            transport=httpx.MockTransport(record), base_url="http://127.0.0.1:8765"
        ),
    )
    return gateway, calls


class _UnreadBody(httpx.SyncByteStream):
    """A body that, like a real network response, is unavailable until read."""

    def __init__(self, payload: dict):
        self.payload = payload

    def __iter__(self):
        yield json.dumps(self.payload).encode()


def test_gateway_connection_accepts_only_numeric_loopback() -> None:
    assert GatewayConnection("http://127.0.0.1:8765", "t" * 43).base_url.endswith("8765")
    assert GatewayConnection("http://[::1]:8765", "t" * 43).base_url.endswith("8765")
    for url in (
        "http://localhost:8765",
        "http://192.168.1.2:8765",
        "https://example.com",
        "http://user:pass@127.0.0.1:8765",
        "http://127.0.0.1:8765/path",
    ):
        with pytest.raises(ValueError):
            GatewayConnection(url, "t" * 43)


def test_desktop_sse_parser_handles_comments_and_multiline_data() -> None:
    lines = [": keep-alive", "", 'data: {"event_id":1,', 'data: "run_id":"r"}', ""]
    assert list(_iter_sse(iter(lines))) == [{"event_id": 1, "run_id": "r"}]


def test_inspect_staging_uses_workspace_scoped_gateway_endpoint(monkeypatch) -> None:
    gateway, calls = _gateway(
        monkeypatch,
        lambda _request: httpx.Response(
            200, json={"ok": True, "data": {"unpublished_changes": False}}
        ),
    )

    result = gateway.inspect_staging("workspace-1")

    assert result["data"]["unpublished_changes"] is False
    assert calls == [("GET", "/v1/workspaces/workspace-1/staging/inspect")]


def test_discard_staging_uses_the_selected_workspace(monkeypatch) -> None:
    gateway, calls = _gateway(
        monkeypatch, lambda _request: httpx.Response(200, json={"ok": True, "file_count": 4})
    )

    assert gateway.discard_staging("workspace-1") == {"ok": True, "file_count": 4}
    assert calls == [("POST", "/v1/workspaces/workspace-1/staging/discard")]


def test_inspect_staging_rejects_malformed_success_response(monkeypatch) -> None:
    gateway, _calls = _gateway(monkeypatch, lambda _request: httpx.Response(200, json={"ok": True}))

    with pytest.raises(GatewayError, match="invalid staging inspection"):
        gateway.inspect_staging("workspace-1")


def test_streamed_error_response_reports_the_gateway_error(monkeypatch) -> None:
    error = {"error": {"code": "rate.limited", "message": "Slow down.", "retryable": True}}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/chat/stream":
            return httpx.Response(429, stream=_UnreadBody(error))
        return httpx.Response(200, json={"configured": True})

    gateway, _calls = _gateway(monkeypatch, handler)

    with pytest.raises(GatewayError, match="Slow down.") as raised:
        list(gateway.stream_chat(api_key="key", model="vendor/model", messages=[]))
    assert (raised.value.code, raised.value.retryable) == ("rate.limited", True)
    assert gateway._active_run_id is None
