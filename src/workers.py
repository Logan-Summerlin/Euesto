from __future__ import annotations

import threading
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

from PySide6.QtCore import QThread, Signal

from shared.events import EventEnvelope
from shared.tools import PublishManifest

from .gateway_client import GatewayClient, GatewayError
from .models import ModelOption, RequestOptions, ServerToolOptions
from .workspace_broker import BrokerError, WorkspaceBroker

_last_gateway_client: GatewayClient | None = None


class CatalogWorker(QThread):
    complete = Signal(object)
    failed = Signal(str)

    def __init__(self, gateway: GatewayClient):
        super().__init__()
        self.gateway = gateway

    def run(self) -> None:
        try:
            raw_models, fetched_at = self.gateway.fetch_models(refresh=True)
            models = [ModelOption.from_json(item) for item in raw_models]
            self.complete.emit({"models": models, "fetched_at": fetched_at})
        except (GatewayError, KeyError, TypeError, ValueError) as exc:
            self.failed.emit(str(exc))


class ChatWorker(QThread):
    chunk = Signal(str)
    runStarted = Signal(str)
    complete = Signal(dict, bool)
    failed = Signal(str)

    def __init__(self, client: GatewayClient, api_key: str, model: str, messages: list[dict[str, str]], options: RequestOptions, server_tools: ServerToolOptions, supported_parameters: frozenset[str]):
        super().__init__(); self.client = client; self.api_key = api_key; self.model = model; self.messages = messages; self.options = options; self.server_tools = server_tools; self.supported_parameters = supported_parameters; self.stop_event = threading.Event()
    def stop(self) -> None: self.stop_event.set(); self.client.cancel()
    def run(self) -> None:
        result: dict[str, Any] = {}; chunks: list[str] = []; started_run_id: str | None = None
        try:
            for event in self.client.stream_chat(api_key=self.api_key, model=self.model, messages=self.messages, stop_event=self.stop_event, options=self.options, server_tools=self.server_tools, supported_parameters=self.supported_parameters):
                run_id = getattr(event, "run_id", None)
                if run_id and run_id != started_run_id: started_run_id = str(run_id); self.runStarted.emit(started_run_id)
                if event.text: chunks.append(event.text)
                if event.usage: result.update(event.usage)
                if event.model_id: result["actual_model"] = event.model_id
                if event.provider_id: result["provider"] = event.provider_id
                if event.finish_reason: result["finish_reason"] = event.finish_reason
            if started_run_id: result["run_id"] = started_run_id
            if chunks: self.chunk.emit("".join(chunks))
            self.complete.emit(result, self.stop_event.is_set())
        except GatewayError as exc:
            if chunks: self.chunk.emit("".join(chunks))
            self.failed.emit(str(exc))
        except Exception as exc:
            if chunks: self.chunk.emit("".join(chunks))
            self.failed.emit(f"Unexpected error: {exc}")


AgentStream = Callable[[threading.Event], Iterator[EventEnvelope]]


class AgentWorker(QThread):
    chunk = Signal(str)
    runStarted = Signal(str)
    eventReceived = Signal(object)
    complete = Signal(dict, bool)
    failed = Signal(str)
    def __init__(self, client: GatewayClient, stream: AgentStream, *, failure_context: str = "Agent", auto_approve: bool = False):
        super().__init__(); global _last_gateway_client; _last_gateway_client = client; self.client = client; self._stream = stream; self.failure_context = failure_context; self.auto_approve = auto_approve; self.stop_event = threading.Event(); self.last_usage: dict[str, Any] = {}
    @classmethod
    def for_run(cls, client: GatewayClient, *, api_key: str, model: str, messages: Sequence[dict[str, Any]], mode: str, workspace_id: str, approval_policy: str, session_id: str, context_limit_tokens: int, skills: Sequence[dict[str, Any]], workspace_config: dict[str, Any], provider_preferences: dict[str, Any] | None = None) -> AgentWorker:
        return cls(client, lambda stop_event: client.stream_agent(api_key=api_key, model=model, messages=messages, mode=mode, workspace_id=workspace_id, approval_policy=approval_policy, stop_event=stop_event, session_id=session_id, context_limit_tokens=context_limit_tokens, skills=skills, workspace_config=workspace_config, provider_preferences=provider_preferences), auto_approve=approval_policy == "auto")
    @classmethod
    def for_resume(cls, client: GatewayClient, run_id: str, api_key: str) -> AgentWorker:
        return cls(client, lambda stop_event: client.resume_agent(run_id, api_key=api_key, stop_event=stop_event), failure_context="Agent resume")
    def stop(self) -> None: self.stop_event.set(); self.client.cancel()
    def run(self) -> None:
        usage: dict[str, Any] = {}; chunks: list[str] = []; started_run_id: str | None = None
        try:
            for event in self._stream(self.stop_event):
                if event.run_id and event.run_id != started_run_id: started_run_id = event.run_id; self.runStarted.emit(event.run_id)
                if event.type == "model.delta" and event.payload.get("text"): chunks.append(str(event.payload["text"])); continue
                if event.type in {"usage.updated", "run.completed"}: usage.update(event.payload); continue
                if event.type in {"tool.output", "tool.completed"}: continue
                self.eventReceived.emit(event)
                if event.type == "run.failed": raise GatewayError(str(event.payload.get("message") or "Agent run failed."))
            if started_run_id: usage["run_id"] = started_run_id
            self.last_usage = dict(usage)
            if chunks: self.chunk.emit("".join(chunks))
            self.complete.emit(usage, self.stop_event.is_set())
        except GatewayError as exc:
            self.last_usage = dict(usage)
            if chunks: self.chunk.emit("".join(chunks))
            self.failed.emit(str(exc))
        except Exception as exc:
            self.last_usage = dict(usage)
            if chunks: self.chunk.emit("".join(chunks))
            self.failed.emit(f"{self.failure_context} failed safely: {exc}")


class StagingDiscardWorker(QThread):
    complete = Signal(dict); failed = Signal(str)
    def __init__(self, client: GatewayClient, workspace_id: str): super().__init__(); self.client = client; self.workspace_id = workspace_id
    def run(self) -> None:
        try: self.complete.emit(self.client.discard_staging())
        except (GatewayError, KeyError, TypeError, ValueError) as exc: self.failed.emit(str(exc))


class StagingInspectWorker(QThread):
    complete = Signal(dict); failed = Signal(str)
    def __init__(self, client: GatewayClient, workspace_id: str): super().__init__(); self.client = client; self.workspace_id = workspace_id
    def run(self) -> None:
        try: self.complete.emit(self.client.inspect_staging(self.workspace_id))
        except (GatewayError, KeyError, TypeError, ValueError) as exc: self.failed.emit(str(exc))


class PublicationWorker(QThread):
    complete = Signal(dict); failed = Signal(str)
    def __init__(self, manifest: PublishManifest, workspace_root: Path, recovery_root: Path, *, reseed_client: GatewayClient | None = None):
        super().__init__(); self.manifest = manifest; self.workspace_root = workspace_root; self.recovery_root = recovery_root; self.reseed_client = reseed_client
    def run(self) -> None:
        global _last_gateway_client
        try:
            broker = WorkspaceBroker(self.workspace_root, self.recovery_root)
            published = broker.publish(self.manifest, {item.path for item in self.manifest.operations})
        except (BrokerError, OSError, TypeError, ValueError) as exc:
            self.failed.emit(str(exc)); return
        result: dict[str, Any] = {"completed_paths": list(published.completed_paths), "checkpoint_id": published.checkpoint_id}
        # Publication must reconcile the executor baseline before the handoff is
        # considered complete. The existing staging-inspect endpoint asks the
        # executor to reconcile a host publication and then reports its state.
        reseed_client = self.reseed_client or _last_gateway_client
        if reseed_client:
            try:
                inspection = reseed_client.inspect_staging(self.manifest.workspace_id)
                data = inspection.get("data") if isinstance(inspection, dict) else None
                if not isinstance(data, dict) or bool(data.get("unpublished_changes")):
                    raise GatewayError("Published workspace still has unpublished staging changes.")
                result["reseeded"] = True
            except (GatewayError, KeyError, TypeError, ValueError) as exc:
                result["reseeded"] = False
                result["reseed_error"] = str(exc)
        else:
            result["reseeded"] = False
            result["reseed_error"] = "No gateway client available to advance the staging baseline."
        _last_gateway_client = None
        self.complete.emit(result)
