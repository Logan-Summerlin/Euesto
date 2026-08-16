from __future__ import annotations

import json
import threading
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from shared.coercion import optional_string
from shared.events import EVENT_TYPES, EventEnvelope
from shared.protocol import protocol_is_compatible
from shared.responses import GatewayStatus
from shared.tools import PublishManifest

from .models import RequestOptions, ServerToolOptions

DEFAULT_GATEWAY_URL = "http://127.0.0.1:8765"


class GatewayError(RuntimeError):
    def __init__(self, message: str, *, code: str = "gateway.error", retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class IncompatibleGatewayError(GatewayError):
    pass


@dataclass(frozen=True, slots=True)
class GatewayConnection:
    base_url: str = DEFAULT_GATEWAY_URL
    token: str = ""

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "::1"}:
            raise ValueError("Gateway URL must use a numeric loopback address")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Gateway URL cannot include credentials, query, or fragment")
        if parsed.path not in {"", "/"}:
            raise ValueError("Gateway URL cannot include a path")
        if self.token and len(self.token.encode("utf-8")) < 32:
            raise ValueError("Gateway token must contain at least 256 bits")


@dataclass(slots=True)
class GatewayStreamEvent:
    text: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    model_id: str | None = None
    provider_id: str | None = None
    finish_reason: str | None = None
    done: bool = False
    cancelled: bool = False
    run_id: str | None = None


class GatewayClient:
    def __init__(self, connection: GatewayConnection):
        self.connection = connection
        self._active_run_id: str | None = None
        self._cancel_sent = False
        self._lock = threading.Lock()

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.connection.token}"}

    def status(self) -> GatewayStatus:
        try:
            with self._client(timeout=5.0) as client:
                response = client.get("/v1/status", headers=self.headers)
                self._raise_for_error(response)
                status = GatewayStatus.from_dict(response.json())
        except GatewayError:
            raise
        except (httpx.HTTPError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise GatewayError(f"Could not connect to the local gateway: {exc}", code="gateway.disconnected", retryable=True) from exc
        if not protocol_is_compatible(status.protocol_version):
            raise IncompatibleGatewayError(
                f"Gateway protocol {status.protocol_version or 'unknown'} is incompatible with this desktop.",
                code="protocol.incompatible",
            )
        return status

    def configure_openrouter_key(self, api_key: str) -> None:
        with self._client(timeout=10.0) as client:
            response = client.put("/v1/session/openrouter-key", headers={**self.headers, "Content-Type": "application/json"}, json={"api_key": api_key})
            self._raise_for_error(response)

    def fetch_models(self, *, refresh: bool = False) -> tuple[list[dict[str, Any]], str]:
        method = "POST" if refresh else "GET"
        path = "/v1/models/refresh" if refresh else "/v1/models"
        headers = {**self.headers, **({"Content-Type": "application/json"} if refresh else {})}
        with self._client(timeout=30.0) as client:
            response = client.request(method, path, headers=headers, json={} if refresh else None)
            self._raise_for_error(response)
            data = response.json()
        models = data.get("data") if isinstance(data, dict) else None
        fetched_at = data.get("fetched_at") if isinstance(data, dict) else None
        if not isinstance(models, list) or not isinstance(fetched_at, str):
            raise GatewayError("Gateway returned an invalid model catalog.", code="protocol.invalid_catalog")
        return [dict(item) for item in models if isinstance(item, dict)], fetched_at

    def inspect_staging(self, workspace_id: str) -> dict[str, Any]:
        with self._client(timeout=10.0) as client:
            response = client.get(f"/v1/workspaces/{workspace_id}/staging/inspect", headers=self.headers)
            self._raise_for_error(response)
            data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get("data"), dict):
            raise GatewayError("Gateway returned invalid staging inspection data.", code="protocol.invalid_staging")
        return dict(data)

    def mark_staging_published(self, manifest: PublishManifest) -> dict[str, Any]:
        with self._client(timeout=30.0) as client:
            response = client.post(
                f"/v1/workspaces/{manifest.workspace_id}/staging/mark-published",
                headers={**self.headers, "Content-Type": "application/json"},
                json=manifest.to_dict(),
            )
            self._raise_for_error(response)
            data = response.json()
        if not isinstance(data, dict):
            raise GatewayError("Gateway returned invalid staging baseline data.", code="protocol.invalid_staging")
        return dict(data)

    def discard_staging(self, workspace_id: str) -> dict[str, Any]:
        with self._client(timeout=30.0) as client:
            response = client.post(f"/v1/workspaces/{workspace_id}/staging/discard", headers=self.headers)
            self._raise_for_error(response)
            data = response.json()
        if not isinstance(data, dict):
            raise GatewayError("Gateway returned invalid staging discard data.", code="protocol.invalid_staging")
        return dict(data)

    def stream_chat(self, *, api_key: str, model: str, messages: Sequence[dict[str, str]], stop_event: threading.Event | None = None, options: RequestOptions | None = None, server_tools: ServerToolOptions | None = None, supported_parameters: frozenset[str] = frozenset()) -> Iterator[GatewayStreamEvent]:
        self.configure_openrouter_key(api_key)
        options = options or RequestOptions()
        payload = {
            "mode": "chat", "model": model, "messages": _public_messages(messages),
            "options": asdict(options), "server_tools": asdict(server_tools or ServerToolOptions()),
            "supported_parameters": sorted(supported_parameters),
            "provider_preferences": {"data_collection": options.data_collection, "zdr": options.zero_data_retention},
        }
        headers = {**self.headers, "Content-Type": "application/json", "Accept": "text/event-stream"}
        last_event_id = 0
        run_id: str | None = None
        for attempt in range(1, 5):
            try:
                with self._client(timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)) as client:
                    context = client.stream("POST", "/v1/chat/stream", headers=headers, json=payload) if run_id is None else client.stream("GET", f"/v1/runs/{run_id}/events", headers={**self.headers, "Accept": "text/event-stream", "Last-Event-ID": str(last_event_id)})
                    with context as response:
                        self._raise_for_error(response)
                        run_id = run_id or response.headers.get("X-Run-ID")
                        if not run_id:
                            raise GatewayError("Gateway stream omitted its run ID.", code="protocol.missing_run_id")
                        with self._lock:
                            self._active_run_id, self._cancel_sent = run_id, False
                        for raw in _iter_sse(response.iter_lines()):
                            if stop_event and stop_event.is_set():
                                self.cancel()
                            try:
                                envelope = EventEnvelope.from_dict(raw)
                            except (KeyError, TypeError, ValueError) as exc:
                                raise GatewayError(f"Gateway returned an invalid event: {exc}", code="protocol.invalid_event") from exc
                            if envelope.type not in EVENT_TYPES:
                                raise GatewayError(f"Unknown gateway event: {envelope.type}", code="protocol.unknown_event")
                            last_event_id = envelope.event_id
                            mapped = _map_event(envelope, run_id=run_id)
                            if mapped is not None:
                                yield mapped
                            if envelope.type in {"run.completed", "run.cancelled"}:
                                with self._lock:
                                    self._active_run_id = None
                                return
                with self._lock:
                    self._active_run_id = None
                return
            except GatewayError:
                with self._lock:
                    self._active_run_id = None
                raise
            except (httpx.HTTPError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                if run_id is None or attempt >= 4:
                    with self._lock:
                        self._active_run_id = None
                    raise GatewayError(f"Gateway stream disconnected: {exc}", code="gateway.stream_disconnected", retryable=True) from exc
            finally:
                if attempt >= 4 or (stop_event and stop_event.is_set()):
                    with self._lock:
                        self._active_run_id = None
        with self._lock:
            self._active_run_id = None

    def stream_agent(self, *, api_key: str, model: str, messages: Sequence[dict[str, Any]], mode: str, workspace_id: str, approval_policy: str = "prompt", stop_event: threading.Event | None = None, session_id: str | None = None, context_limit_tokens: int = 100_000, skills: Sequence[dict[str, Any]] = (), workspace_config: dict[str, Any] | None = None, max_iterations: int = 101, max_tool_calls: int = 100, max_wall_seconds: int = 900, max_cost: float = 1.0, provider_preferences: dict[str, Any] | None = None) -> Iterator[EventEnvelope]:
        self.configure_openrouter_key(api_key)
        payload = {
            "model": model, "messages": _public_messages(messages), "mode": mode, "workspace_id": workspace_id,
            "approval_policy": approval_policy, "session_id": session_id, "context_limit_tokens": context_limit_tokens,
            "skills": [dict(item) for item in skills], "workspace_config": dict(workspace_config or {}),
            "max_iterations": max_iterations, "max_tool_calls": max_tool_calls, "max_wall_seconds": max_wall_seconds,
            "max_cost": max_cost, "provider_preferences": dict(provider_preferences or {}),
        }
        with self._client(timeout=15) as client:
            response = client.post("/v1/runs", headers={**self.headers, "Content-Type": "application/json"}, json=payload)
            self._raise_for_error(response)
            run_id = str(response.json().get("run_id") or "")
        if not run_id:
            raise GatewayError("Gateway omitted the agent run ID.", code="protocol.missing_run_id")
        yield from self._stream_agent_events(run_id, stop_event=stop_event)

    def resolve_approval(self, run_id: str, approval_id: str, decision: str) -> None:
        with self._client(timeout=10) as client:
            response = client.post(f"/v1/runs/{run_id}/approvals/{approval_id}", headers={**self.headers, "Content-Type": "application/json"}, json={"decision": decision})
            self._raise_for_error(response)

    def permission_rules(self, workspace_id: str) -> list[dict[str, Any]]:
        with self._client(timeout=10) as client:
            response = client.get("/v1/permissions", headers=self.headers, params={"workspace_id": workspace_id})
            self._raise_for_error(response)
            rules = response.json().get("rules")
        if not isinstance(rules, list):
            raise GatewayError("Gateway returned invalid permission rules.", code="protocol.invalid_rules")
        return [dict(item) for item in rules if isinstance(item, dict)]

    def delete_permission_rule(self, rule_id: str) -> None:
        with self._client(timeout=10) as client:
            response = client.delete(f"/v1/permissions/{rule_id}", headers=self.headers)
            self._raise_for_error(response)

    def set_permission_rule_enabled(self, rule_id: str, enabled: bool) -> None:
        with self._client(timeout=10) as client:
            response = client.put(f"/v1/permissions/{rule_id}/enabled", headers={**self.headers, "Content-Type": "application/json"}, json={"enabled": enabled})
            self._raise_for_error(response)

    def workspace_config(self, workspace_id: str) -> dict[str, Any]:
        with self._client(timeout=10) as client:
            response = client.get(f"/v1/workspaces/{workspace_id}/config", headers=self.headers)
            self._raise_for_error(response)
            value = response.json().get("config")
        if not isinstance(value, dict):
            raise GatewayError("Gateway returned invalid workspace configuration.")
        return dict(value)

    def save_workspace_config(self, workspace_id: str, config: dict[str, Any]) -> None:
        with self._client(timeout=10) as client:
            response = client.put(f"/v1/workspaces/{workspace_id}/config", headers={**self.headers, "Content-Type": "application/json"}, json=config)
            self._raise_for_error(response)

    def resume_agent(self, run_id: str, *, api_key: str, stop_event: threading.Event | None = None) -> Iterator[EventEnvelope]:
        self.configure_openrouter_key(api_key)
        with self._client(timeout=15) as client:
            response = client.post(f"/v1/runs/{run_id}/resume", headers={**self.headers, "Content-Type": "application/json"}, json={})
            self._raise_for_error(response)
            after_event_id = int(response.json().get("after_event_id") or 0)
        yield from self._stream_agent_events(run_id, stop_event=stop_event, after_event_id=after_event_id)

    def _stream_agent_events(self, run_id: str, *, stop_event: threading.Event | None, after_event_id: int = 0) -> Iterator[EventEnvelope]:
        with self._lock:
            self._active_run_id, self._cancel_sent = run_id, False
        last_event_id = after_event_id
        try:
            while True:
                if stop_event and stop_event.is_set():
                    self.cancel()
                try:
                    with self._client(timeout=httpx.Timeout(connect=10, read=None, write=30, pool=10)) as client:
                        with client.stream("GET", f"/v1/runs/{run_id}/events", headers={**self.headers, "Accept": "text/event-stream", "Last-Event-ID": str(last_event_id)}) as response:
                            self._raise_for_error(response)
                            for raw in _iter_sse(response.iter_lines()):
                                envelope = EventEnvelope.from_dict(raw)
                                last_event_id = envelope.event_id
                                yield envelope
                                if envelope.type in {"run.completed", "run.cancelled", "run.failed", "run.paused"}:
                                    return
                except httpx.HTTPError:
                    if stop_event and stop_event.is_set():
                        return
        finally:
            with self._lock:
                self._active_run_id = None

    def cancel(self) -> None:
        with self._lock:
            run_id = self._active_run_id
            if not run_id or self._cancel_sent:
                return
            self._cancel_sent = True
        threading.Thread(target=self._send_cancel, args=(run_id,), daemon=True).start()

    def pause(self) -> bool:
        with self._lock:
            run_id = self._active_run_id
        if not run_id:
            return False
        with self._client(timeout=5.0) as client:
            response = client.post(f"/v1/runs/{run_id}/pause", headers={**self.headers, "Content-Type": "application/json"}, json={})
            self._raise_for_error(response)
        return True

    def _send_cancel(self, run_id: str) -> None:
        if not run_id:
            return
        try:
            with self._client(timeout=5.0) as client:
                client.post(f"/v1/runs/{run_id}/cancel", headers={**self.headers, "Content-Type": "application/json"}, json={})
        except httpx.HTTPError:
            return

    def _client(self, *, timeout: httpx.Timeout | float) -> httpx.Client:
        return httpx.Client(base_url=self.connection.base_url.rstrip("/"), timeout=timeout, follow_redirects=False)

    @staticmethod
    def _raise_for_error(response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        try:
            payload = response.json()
            error = payload.get("error", {}) if isinstance(payload, dict) else {}
            message = str(error.get("message") or f"Gateway error {response.status_code}")
            code = str(error.get("code") or "gateway.http_error")
            retryable = bool(error.get("retryable"))
        except (ValueError, TypeError):
            message, code, retryable = f"Gateway error {response.status_code}", "gateway.http_error", False
        raise GatewayError(message, code=code, retryable=retryable)


def _iter_sse(lines: Iterator[str]) -> Iterator[dict[str, Any]]:
    data_lines: list[str] = []
    for line in lines:
        if not line:
            if data_lines:
                yield json.loads("\n".join(data_lines))
                data_lines.clear()
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        yield json.loads("\n".join(data_lines))


def _public_messages(messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"role": message.get("role"), "content": message.get("content")} for message in messages]


def _map_event(event: EventEnvelope, *, run_id: str | None = None) -> GatewayStreamEvent | None:
    payload = event.payload
    if event.type == "model.delta":
        return GatewayStreamEvent(text=str(payload.get("text") or ""), run_id=run_id)
    if event.type in {"usage.updated", "model.completed"}:
        return GatewayStreamEvent(usage=dict(payload), run_id=run_id, model_id=optional_string(payload.get("actual_model")), provider_id=optional_string(payload.get("provider")), finish_reason=optional_string(payload.get("finish_reason")), done=event.type == "model.completed")
    if event.type in {"model.failed", "run.failed"}:
        raise GatewayError(str(payload.get("message") or "Gateway run failed."), code=str(payload.get("code") or "gateway.run_failed"), retryable=bool(payload.get("retryable")))
    if event.type == "run.cancelled":
        return GatewayStreamEvent(done=True, cancelled=True, run_id=run_id)
    if event.type == "run.completed":
        return GatewayStreamEvent(done=True, run_id=run_id)
    return None
