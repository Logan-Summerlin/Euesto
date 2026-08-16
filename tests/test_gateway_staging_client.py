from __future__ import annotations

from contextlib import contextmanager

import httpx
import pytest

from src.gateway_client import GatewayClient, GatewayConnection, GatewayError


class FakeHttpClient:
    def __init__(self, responses: dict[tuple[str, str], httpx.Response]):
        self.responses = responses
        self.calls: list[tuple[str, str]] = []

    def get(self, path: str, **_kwargs):
        self.calls.append(("GET", path))
        return self.responses[("GET", path)]

    def post(self, path: str, **_kwargs):
        self.calls.append(("POST", path))
        return self.responses[("POST", path)]


@contextmanager
def fake_client(client: FakeHttpClient):
    yield client


def test_inspect_staging_uses_workspace_scoped_gateway_endpoint(monkeypatch) -> None:
    http = FakeHttpClient(
        {
            ("GET", "/v1/workspaces/workspace-1/staging/inspect"): httpx.Response(
                200, json={"ok": True, "data": {"unpublished_changes": False}}
            )
        }
    )
    gateway = GatewayClient(GatewayConnection(token="t" * 43))
    monkeypatch.setattr(gateway, "_client", lambda **_kwargs: fake_client(http))

    result = gateway.inspect_staging("workspace-1")

    assert result["data"]["unpublished_changes"] is False
    assert http.calls == [("GET", "/v1/workspaces/workspace-1/staging/inspect")]


def test_discard_staging_uses_the_selected_workspace(monkeypatch) -> None:
    http = FakeHttpClient(
        {
            ("POST", "/v1/workspaces/workspace-1/staging/discard"): httpx.Response(
                200, json={"ok": True, "file_count": 4}
            )
        }
    )
    gateway = GatewayClient(GatewayConnection(token="t" * 43))
    monkeypatch.setattr(gateway, "_client", lambda **_kwargs: fake_client(http))

    result = gateway.discard_staging("workspace-1")

    assert result == {"ok": True, "file_count": 4}
    assert http.calls == [("POST", "/v1/workspaces/workspace-1/staging/discard")]


def test_inspect_staging_rejects_malformed_success_response(monkeypatch) -> None:
    http = FakeHttpClient(
        {
            ("GET", "/v1/workspaces/workspace-1/staging/inspect"): httpx.Response(
                200, json={"ok": True}
            )
        }
    )
    gateway = GatewayClient(GatewayConnection(token="t" * 43))
    monkeypatch.setattr(gateway, "_client", lambda **_kwargs: fake_client(http))

    with pytest.raises(GatewayError, match="invalid staging inspection"):
        gateway.inspect_staging("workspace-1")
