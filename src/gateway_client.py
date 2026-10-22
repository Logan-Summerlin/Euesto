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
from shared.tools import PublicationReceipt, PublishManifest

from .models import RequestOptions, ServerToolOptions

DEFAULT_GATEWAY_URL = "http://127.0.0.1:8765"
JSON = {"Content-Type": "application/json"}
STREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)
CHAT_STREAM_ATTEMPTS = 4
AGENT_TERMINAL_EVENTS = frozenset(
    {"run.completed", "run.cancelled", "run.failed", "run.paused"}
)


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
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
            "127.0.0.1",
            "::1",
        }:
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
            status = GatewayStatus.from_dict(
                self._send("GET", "/v1/status", timeout=5.0).json()
            )
        except GatewayError:
            raise
        except (httpx.HTTPError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise GatewayError(
                f"Could not connect to the local gateway: {exc}",
                code="gateway.disconnected",
                retryable=True,
            ) from exc
        if not protocol_is_compatible(status.protocol_version):
            raise IncompatibleGatewayError(
                f"Gateway protocol {status.protocol_version or 'unknown'} is incompatible "
                "with this desktop.",
                code="protocol.incompatible",
            )
        return status

    def configure_openrouter_key(self, api_key: str) -> None:
        self._send("PUT", "/v1/session/openrouter-key", json={"api_key": api_key})

    def fetch_models(self, *, refresh: bool = False) -> tuple[list[dict[str, Any]], str]:
        if refresh:
            response = self._send("POST", "/v1/models/refresh", timeout=30.0, json={})
        else:
            response = self._send("GET", "/v1/models", timeout=30.0)
        data = response.json()
        models = data.get("data") if isinstance(data, dict) else None
        fetched_at = data.get("fetched_at") if isinstance(data, dict) else None
        if not isinstance(models, list) or not isinstance(fetched_at, str):
            raise GatewayError(
                "Gateway returned an invalid model catalog.", code="protocol.invalid_catalog"
            )
        return [dict(item) for item in models if isinstance(item, dict)], fetched_at

    def inspect_staging(self, workspace_id: str) -> dict[str, Any]:
        data = self._send("GET", f"/v1/workspaces/{workspace_id}/staging/inspect").json()
        if not isinstance(data, dict) or not isinstance(data.get("data"), dict):
            raise _invalid_staging("Gateway returned invalid staging inspection data.")
        return dict(data)

    def mark_staging_published(self, manifest: PublishManifest) -> dict[str, Any]:
        response = self._send(
            "POST",
            f"/v1/workspaces/{manifest.workspace_id}/staging/mark-published",
            timeout=30.0,
            # Paths, hashes, and modes only: the baseline never needs the content again.
            json=PublicationReceipt.from_manifest(manifest).to_dict(),
        )
        return _staging_object(response, "Gateway returned invalid staging baseline data.")

    def next_publication_batch(
        self, manifest: PublishManifest, paths: Sequence[str] | None = None
    ) -> PublishManifest:
        """Request the next batch of a multi-batch publication after ``manifest`` published;
        a partial publication passes its ``paths`` selection again."""
        body: dict[str, Any] = {
            "run_id": manifest.run_id,
            "publication_id": manifest.publication_id,
            "batch_index": manifest.batch_index + 1,
        }
        if paths:
            body["paths"] = list(paths)
        response = self._send(
            "POST",
            f"/v1/workspaces/{manifest.workspace_id}/staging/manifest",
            timeout=60.0,
            json=body,
        )
        data = _staging_object(response, "Gateway returned an invalid publication batch.")
        try:
            return PublishManifest.from_dict(data)
        except (TypeError, ValueError) as exc:
            raise _invalid_staging(
                f"Gateway returned an invalid publication batch: {exc}"
            ) from exc

    def discard_staging(self, workspace_id: str) -> dict[str, Any]:
        response = self._send(
            "POST", f"/v1/workspaces/{workspace_id}/staging/discard", timeout=30.0
        )
        return _staging_object(response, "Gateway returned invalid staging discard data.")

    def rebase_staging(
        self,
        workspace_id: str,
        *,
        keep_staged: Sequence[str] = (),
        take_host: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Absorb host changes into staging, resolving the named conflicts (plan 2.4)."""
        response = self._send(
            "POST",
            f"/v1/workspaces/{workspace_id}/staging/rebase",
            timeout=120.0,
            json={"keep_staged": list(keep_staged), "take_host": list(take_host)},
        )
        return _staging_object(response, "Gateway returned invalid staging rebase data.")

    def staged_publication(
        self, workspace_id: str, paths: Sequence[str] | None = None
    ) -> PublishManifest:
        """The first batch publishing staged work, or only ``paths`` (partial publication)."""
        response = self._send(
            "POST",
            f"/v1/workspaces/{workspace_id}/staging/publication",
            timeout=60.0,
            json={"paths": list(paths)} if paths else {},
        )
        data = _staging_object(response, "Gateway returned an invalid publication.")
        try:
            return PublishManifest.from_dict(data)
        except (TypeError, ValueError) as exc:
            raise _invalid_staging(f"Gateway returned an invalid publication: {exc}") from exc

    def review_staging(self, workspace_id: str, cursor: str | None = None) -> dict[str, Any]:
        """Staged changes with per-file diffs, the fingerprint, and declared check results."""
        response = self._send(
            "POST",
            f"/v1/workspaces/{workspace_id}/staging/review",
            timeout=60.0,
            json={"cursor": cursor} if cursor else {},
        )
        return _staging_object(response, "Gateway returned an invalid staging review.")

    def run_check(self, workspace_id: str, name: str) -> dict[str, Any]:
        """Run one declared check against staging in the runner (plan 3.4)."""
        response = self._send(
            "POST", f"/v1/workspaces/{workspace_id}/checks/{name}/run", timeout=1_000.0, json={}
        )
        return _staging_object(response, "Gateway returned an invalid check result.")

    def acknowledge_notice(self, notice_id: str) -> None:
        self._send("POST", f"/v1/notices/{notice_id}/acknowledge", timeout=5.0, json={})

    def steer(self, run_id: str, text: str) -> None:
        """Queue guidance for the active run; it arrives before the next model turn (0.8)."""
        self._send("POST", f"/v1/runs/{run_id}/messages", timeout=5.0, json={"content": text})

    def stream_chat(
        self,
        *,
        api_key: str,
        model: str,
        messages: Sequence[dict[str, str]],
        stop_event: threading.Event | None = None,
        options: RequestOptions | None = None,
        server_tools: ServerToolOptions | None = None,
        supported_parameters: frozenset[str] = frozenset(),
    ) -> Iterator[GatewayStreamEvent]:
        """Stream a chat run, reconnecting to its event stream after a dropped connection."""
        self.configure_openrouter_key(api_key)
        options = options or RequestOptions()
        payload = {
            "mode": "chat",
            "model": model,
            "messages": _public_messages(messages),
            "options": asdict(options),
            "server_tools": asdict(server_tools or ServerToolOptions()),
            "supported_parameters": sorted(supported_parameters),
            "provider_preferences": {
                "data_collection": options.data_collection,
                "zdr": options.zero_data_retention,
            },
        }
        last_event_id = 0
        run_id: str | None = None
        try:
            for attempt in range(1, CHAT_STREAM_ATTEMPTS + 1):
                try:
                    with self._client(timeout=STREAM_TIMEOUT) as client:
                        if run_id is None:
                            stream = client.stream(
                                "POST",
                                "/v1/chat/stream",
                                headers={**self.headers, **JSON, "Accept": "text/event-stream"},
                                json=payload,
                            )
                        else:
                            stream = client.stream(
                                "GET",
                                f"/v1/runs/{run_id}/events",
                                headers=self._event_headers(last_event_id),
                            )
                        with stream as response:
                            self._raise_for_error(response)
                            run_id = run_id or response.headers.get("X-Run-ID")
                            if not run_id:
                                raise GatewayError(
                                    "Gateway stream omitted its run ID.",
                                    code="protocol.missing_run_id",
                                )
                            self._activate(run_id)
                            for raw in _iter_sse(response.iter_lines()):
                                if stop_event and stop_event.is_set():
                                    self.cancel()
                                envelope = _chat_envelope(raw)
                                last_event_id = envelope.event_id
                                mapped = _map_event(envelope, run_id=run_id)
                                if mapped is not None:
                                    yield mapped
                                if envelope.type in {"run.completed", "run.cancelled"}:
                                    return
                    return
                except GatewayError:
                    raise
                except (
                    httpx.HTTPError,
                    json.JSONDecodeError,
                    KeyError,
                    TypeError,
                    ValueError,
                ) as exc:
                    if run_id is None or attempt >= CHAT_STREAM_ATTEMPTS:
                        raise GatewayError(
                            f"Gateway stream disconnected: {exc}",
                            code="gateway.stream_disconnected",
                            retryable=True,
                        ) from exc
        finally:
            self._activate(None)

    def stream_agent(
        self,
        *,
        api_key: str,
        model: str,
        messages: Sequence[dict[str, Any]],
        mode: str,
        workspace_id: str,
        approval_policy: str = "prompt",
        network_mode: str = "none",
        stop_event: threading.Event | None = None,
        session_id: str | None = None,
        context_limit_tokens: int = 100_000,
        skills: Sequence[dict[str, Any]] = (),
        workspace_config: dict[str, Any] | None = None,
        investigation_model_id: str | None = None,
        provider_preferences: dict[str, Any] | None = None,
    ) -> Iterator[EventEnvelope]:
        self.configure_openrouter_key(api_key)
        payload = {
            "model": model,
            "messages": _public_messages(messages),
            "mode": mode,
            "workspace_id": workspace_id,
            "approval_policy": approval_policy,
            "network_mode": network_mode,
            "session_id": session_id,
            "context_limit_tokens": context_limit_tokens,
            "skills": [dict(item) for item in skills],
            "workspace_config": dict(workspace_config or {}),
            "investigation_model_id": investigation_model_id,
            "provider_preferences": dict(provider_preferences or {}),
        }
        run_id = str(
            self._send("POST", "/v1/runs", timeout=15, json=payload).json().get("run_id") or ""
        )
        if not run_id:
            raise GatewayError(
                "Gateway omitted the agent run ID.", code="protocol.missing_run_id"
            )
        yield from self._stream_agent_events(run_id, stop_event=stop_event)

    def resolve_approval(self, run_id: str, approval_id: str, decision: str) -> None:
        self._send(
            "POST", f"/v1/runs/{run_id}/approvals/{approval_id}", json={"decision": decision}
        )

    def permission_rules(self, workspace_id: str) -> list[dict[str, Any]]:
        response = self._send("GET", "/v1/permissions", params={"workspace_id": workspace_id})
        rules = response.json().get("rules")
        if not isinstance(rules, list):
            raise GatewayError(
                "Gateway returned invalid permission rules.", code="protocol.invalid_rules"
            )
        return [dict(item) for item in rules if isinstance(item, dict)]

    def create_permission_rule(self, workspace_id: str, spec: dict[str, Any]) -> dict[str, Any]:
        """Save a rule from the rules screen: tool, path prefix, or Bash command prefix."""
        response = self._send(
            "POST", "/v1/permissions", json={**spec, "workspace_id": workspace_id}
        )
        rule = response.json().get("rule")
        if not isinstance(rule, dict):
            raise GatewayError(
                "Gateway returned an invalid rule.", code="protocol.invalid_rules"
            )
        return dict(rule)

    def delete_permission_rule(self, rule_id: str) -> None:
        self._send("DELETE", f"/v1/permissions/{rule_id}")

    def set_permission_rule_enabled(self, rule_id: str, enabled: bool) -> None:
        self._send("PUT", f"/v1/permissions/{rule_id}/enabled", json={"enabled": enabled})

    def save_workspace_config(self, workspace_id: str, config: dict[str, Any]) -> None:
        self._send("PUT", f"/v1/workspaces/{workspace_id}/config", json=config)

    def resume_agent(
        self,
        run_id: str,
        *,
        api_key: str,
        stop_event: threading.Event | None = None,
        new_budget: bool = False,
    ) -> Iterator[EventEnvelope]:
        """Resume a paused or interrupted run; ``new_budget`` continues a run that stopped at
        its budget with a fresh one (plan 3.3)."""
        self.configure_openrouter_key(api_key)
        response = self._send(
            "POST",
            f"/v1/runs/{run_id}/resume",
            timeout=15,
            json={"new_budget": True} if new_budget else {},
        )
        after_event_id = int(response.json().get("after_event_id") or 0)
        yield from self._stream_agent_events(
            run_id, stop_event=stop_event, after_event_id=after_event_id
        )

    def _stream_agent_events(
        self, run_id: str, *, stop_event: threading.Event | None, after_event_id: int = 0
    ) -> Iterator[EventEnvelope]:
        """Follow a run's events until it ends, reconnecting after dropped connections."""
        self._activate(run_id)
        last_event_id = after_event_id
        try:
            while True:
                if stop_event and stop_event.is_set():
                    self.cancel()
                try:
                    with self._client(timeout=STREAM_TIMEOUT) as client:
                        with client.stream(
                            "GET",
                            f"/v1/runs/{run_id}/events",
                            headers=self._event_headers(last_event_id),
                        ) as response:
                            self._raise_for_error(response)
                            for raw in _iter_sse(response.iter_lines()):
                                envelope = EventEnvelope.from_dict(raw)
                                last_event_id = envelope.event_id
                                yield envelope
                                if envelope.type in AGENT_TERMINAL_EVENTS:
                                    return
                except httpx.HTTPError:
                    if stop_event and stop_event.is_set():
                        return
        finally:
            self._activate(None)

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
        self._send("POST", f"/v1/runs/{run_id}/pause", timeout=5.0, json={})
        return True

    def _send_cancel(self, run_id: str) -> None:
        try:
            with self._client(timeout=5.0) as client:
                client.post(
                    f"/v1/runs/{run_id}/cancel", headers={**self.headers, **JSON}, json={}
                )
        except httpx.HTTPError:
            return

    def _activate(self, run_id: str | None) -> None:
        """Record the run that ``cancel`` and ``pause`` act on (``None`` when idle)."""
        with self._lock:
            self._active_run_id = run_id
            if run_id is not None:
                self._cancel_sent = False

    def _event_headers(self, last_event_id: int) -> dict[str, str]:
        return {
            **self.headers,
            "Accept": "text/event-stream",
            "Last-Event-ID": str(last_event_id),
        }

    def _send(
        self,
        method: str,
        path: str,
        *,
        timeout: float = 10.0,
        json: Any = None,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        """One authenticated request; gateway error responses raise ``GatewayError``."""
        headers = self.headers if json is None else {**self.headers, **JSON}
        with self._client(timeout=timeout) as client:
            response = client.request(method, path, headers=headers, json=json, params=params)
        self._raise_for_error(response)
        return response

    def _client(self, *, timeout: httpx.Timeout | float) -> httpx.Client:
        return httpx.Client(
            base_url=self.connection.base_url.rstrip("/"),
            timeout=timeout,
            follow_redirects=False,
        )

    @staticmethod
    def _raise_for_error(response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        message, code, retryable = (
            f"Gateway error {response.status_code}",
            "gateway.http_error",
            False,
        )
        try:
            # A streamed response has no body until read; the error envelope is small.
            response.read()
            payload = response.json()
            error = payload.get("error", {}) if isinstance(payload, dict) else {}
            message = str(error.get("message") or message)
            code = str(error.get("code") or code)
            retryable = bool(error.get("retryable"))
        except (ValueError, TypeError):
            pass
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
    return [
        {"role": message.get("role"), "content": message.get("content")} for message in messages
    ]


def _chat_envelope(raw: dict[str, Any]) -> EventEnvelope:
    try:
        envelope = EventEnvelope.from_dict(raw)
    except (KeyError, TypeError, ValueError) as exc:
        raise GatewayError(
            f"Gateway returned an invalid event: {exc}", code="protocol.invalid_event"
        ) from exc
    if envelope.type not in EVENT_TYPES:
        raise GatewayError(
            f"Unknown gateway event: {envelope.type}", code="protocol.unknown_event"
        )
    return envelope


def _map_event(event: EventEnvelope, *, run_id: str | None = None) -> GatewayStreamEvent | None:
    payload = event.payload
    if event.type == "model.delta":
        return GatewayStreamEvent(text=str(payload.get("text") or ""), run_id=run_id)
    if event.type in {"usage.updated", "model.completed"}:
        return GatewayStreamEvent(
            usage=dict(payload),
            run_id=run_id,
            model_id=optional_string(payload.get("actual_model")),
            provider_id=optional_string(payload.get("provider")),
            finish_reason=optional_string(payload.get("finish_reason")),
            done=event.type == "model.completed",
        )
    if event.type in {"model.failed", "run.failed"}:
        raise GatewayError(
            str(payload.get("message") or "Gateway run failed."),
            code=str(payload.get("code") or "gateway.run_failed"),
            retryable=bool(payload.get("retryable")),
        )
    if event.type in {"run.cancelled", "run.completed"}:
        return GatewayStreamEvent(done=True, run_id=run_id)
    return None


def _invalid_staging(message: str) -> GatewayError:
    return GatewayError(message, code="protocol.invalid_staging")


def _staging_object(response: httpx.Response, message: str) -> dict[str, Any]:
    data = response.json()
    if not isinstance(data, dict):
        raise _invalid_staging(message)
    return dict(data)
