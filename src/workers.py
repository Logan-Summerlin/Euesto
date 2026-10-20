from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, TypeVar

from PySide6.QtCore import QThread, Signal

from shared.events import EventEnvelope
from shared.tools import PublishManifest

from .gateway_client import GatewayClient, GatewayError
from .models import ModelOption, RequestOptions, ServerToolOptions
from .workspace_broker import BrokerError, PublicationLedger, WorkspaceBroker, describe_progress

T = TypeVar("T")
# Failures a worker reports as-is; anything else is reported as unexpected.
EXPECTED_ERRORS = (GatewayError, KeyError, TypeError, ValueError)


def _attempt(action: Callable[[], T], unexpected: str) -> tuple[T | None, str | None]:
    """Run ``action`` and return its result, or ``None`` and a user-facing error."""
    try:
        return action(), None
    except EXPECTED_ERRORS as exc:
        return None, str(exc)
    except Exception as exc:
        return None, f"{unexpected}: {exc}"


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
        except EXPECTED_ERRORS as exc:
            self.failed.emit(str(exc))


class _StreamWorker(QThread):
    """Consume one gateway stream off the UI thread, emitting its text as a single chunk."""

    chunk = Signal(str)
    runStarted = Signal(str)
    complete = Signal(dict, bool)
    failed = Signal(str)

    def __init__(self, client: GatewayClient):
        super().__init__()
        self.client = client
        self.stop_event = threading.Event()
        self._chunks: list[str] = []
        self._run_id: str | None = None

    def stop(self) -> None:
        self.stop_event.set()
        self.client.cancel()

    def _note_run(self, run_id: str | None) -> None:
        if run_id and run_id != self._run_id:
            self._run_id = str(run_id)
            self.runStarted.emit(self._run_id)

    def _flush(self) -> None:
        if self._chunks:
            self.chunk.emit("".join(self._chunks))

    def _finish(self, result: dict[str, Any]) -> None:
        if self._run_id:
            result["run_id"] = self._run_id
        self._flush()
        self.complete.emit(result, self.stop_event.is_set())

    def _fail(self, message: str) -> None:
        self._flush()
        self.failed.emit(message)


class ChatWorker(_StreamWorker):
    def __init__(
        self,
        client: GatewayClient,
        api_key: str,
        model: str,
        messages: list[dict[str, str]],
        options: RequestOptions,
        server_tools: ServerToolOptions,
        supported_parameters: frozenset[str],
    ):
        super().__init__(client)
        self.api_key = api_key
        self.model = model
        self.messages = messages
        self.options = options
        self.server_tools = server_tools
        self.supported_parameters = supported_parameters

    def run(self) -> None:
        result: dict[str, Any] = {}
        try:
            for event in self.client.stream_chat(
                api_key=self.api_key,
                model=self.model,
                messages=self.messages,
                stop_event=self.stop_event,
                options=self.options,
                server_tools=self.server_tools,
                supported_parameters=self.supported_parameters,
            ):
                self._note_run(event.run_id)
                if event.text:
                    self._chunks.append(event.text)
                result.update(event.usage)
                for key, value in (
                    ("actual_model", event.model_id),
                    ("provider", event.provider_id),
                    ("finish_reason", event.finish_reason),
                ):
                    if value:
                        result[key] = value
        except Exception as exc:
            self._fail(str(exc) if isinstance(exc, GatewayError) else f"Unexpected error: {exc}")
            return
        self._finish(result)


AgentStream = Callable[[threading.Event], Iterator[EventEnvelope]]


class AgentWorker(_StreamWorker):
    eventReceived = Signal(object)

    def __init__(
        self,
        client: GatewayClient,
        stream: AgentStream,
        *,
        failure_context: str = "Agent",
        auto_approve: bool = False,
    ):
        super().__init__(client)
        self._stream = stream
        self.failure_context = failure_context
        self.auto_approve = auto_approve
        self.last_usage: dict[str, Any] = {}

    @classmethod
    def for_run(cls, client: GatewayClient, **request: Any) -> AgentWorker:
        """A worker for a new run; ``request`` holds ``GatewayClient.stream_agent`` arguments."""
        return cls(
            client,
            lambda stop_event: client.stream_agent(stop_event=stop_event, **request),
            auto_approve=request.get("approval_policy") == "auto",
        )

    @classmethod
    def for_resume(cls, client: GatewayClient, run_id: str, api_key: str) -> AgentWorker:
        return cls(
            client,
            lambda stop_event: client.resume_agent(run_id, api_key=api_key, stop_event=stop_event),
            failure_context="Agent resume",
        )

    def run(self) -> None:
        usage: dict[str, Any] = {}
        try:
            for event in self._stream(self.stop_event):
                self._note_run(event.run_id)
                if event.type == "model.delta" and event.payload.get("text"):
                    self._chunks.append(str(event.payload["text"]))
                elif event.type in {"usage.updated", "run.completed"}:
                    usage.update(event.payload)
                elif event.type not in {"tool.output", "tool.completed"}:
                    self.eventReceived.emit(event)
                    if event.type == "run.failed":
                        raise GatewayError(str(event.payload.get("message") or "Agent run failed."))
        except Exception as exc:
            self.last_usage = dict(usage)
            self._fail(
                str(exc)
                if isinstance(exc, GatewayError)
                else f"{self.failure_context} failed safely: {exc}"
            )
            return
        self._finish(usage)
        self.last_usage = dict(usage)


class StagingWorker(QThread):
    """Run one staging request (``discard`` or ``inspect``) against the gateway off the UI thread."""

    complete = Signal(dict)
    failed = Signal(str)

    def __init__(self, client: GatewayClient, workspace_id: str, action: str):
        super().__init__()
        self.client = client
        self.workspace_id = workspace_id
        self.action = action

    def run(self) -> None:
        if self.action == "discard":
            request, description = self.client.discard_staging, "discard"
        else:
            request, description = self.client.inspect_staging, "inspection"
        result, error = _attempt(
            lambda: request(self.workspace_id), f"Unexpected staging {description} error"
        )
        if error is None:
            self.complete.emit(result)
        else:
            self.failed.emit(error)


class PublicationWorker(QThread):
    """Publish one reviewed batch, advance the staging baseline, and fetch the next batch.

    Multi-batch publications record each batch in the PublicationLedger. When more batches
    remain, the next manifest is returned as ``next_manifest`` for its own approval; nothing
    beyond the approved batch is ever written by this worker.
    """

    complete = Signal(dict)
    failed = Signal(str)

    def __init__(
        self,
        manifest: PublishManifest,
        workspace_root: Path,
        recovery_root: Path,
        *,
        reseed_client: GatewayClient | None = None,
    ):
        super().__init__()
        self.manifest = manifest
        self.workspace_root = workspace_root
        self.recovery_root = recovery_root
        self.reseed_client = reseed_client
        self.continuation_client: GatewayClient | None = None
        self.multi_batch = int(getattr(manifest, "batch_count", 1) or 1) > 1

    def run(self) -> None:
        try:
            broker = WorkspaceBroker(self.workspace_root, self.recovery_root)
            published = broker.publish(
                self.manifest, {item.path for item in self.manifest.operations}
            )
        except Exception as exc:
            known = isinstance(exc, BrokerError | OSError | TypeError | ValueError)
            self._record("failed", error=str(exc))
            message = str(exc) if known else f"Unexpected publication error: {exc}"
            self.failed.emit(self._with_progress(message))
            return
        result: dict[str, Any] = {
            "completed_paths": list(published.completed_paths),
            "checkpoint_id": published.checkpoint_id,
        }
        client = self.reseed_client
        if client is None:
            reseed_error = "No gateway client available to advance the staging baseline."
        else:
            _, reseed_error = _attempt(
                lambda: client.mark_staging_published(self.manifest),
                "Unexpected staging baseline error",
            )
        result["reseeded"] = reseed_error is None
        if reseed_error is not None:
            result["reseed_error"] = reseed_error
        if self.multi_batch:
            record = self._record(
                "published" if reseed_error is None else "baseline_failed",
                checkpoint_id=published.checkpoint_id,
                error=reseed_error,
            )
            result.update({
                "publication_id": self.manifest.publication_id,
                "batch_index": self.manifest.batch_index,
                "batch_count": self.manifest.batch_count,
                "progress": describe_progress(record),
            })
            if client is not None and reseed_error is None and self.manifest.has_more_batches:
                following, batch_error = _attempt(
                    lambda: client.next_publication_batch(self.manifest),
                    "Unexpected publication batch error",
                )
                if batch_error is not None:
                    result["next_batch_error"] = batch_error
                elif following.operations:
                    result["next_manifest"] = following.to_dict()
                    self.continuation_client = client
        self.complete.emit(result)

    def _record(
        self, status: str, *, checkpoint_id: str | None = None, error: str | None = None
    ) -> dict | None:
        if not self.multi_batch:
            return None
        try:
            return PublicationLedger(self.recovery_root).record(
                self.manifest, status, checkpoint_id=checkpoint_id, error=error
            )
        except (BrokerError, OSError, ValueError):
            return None

    def _with_progress(self, message: str) -> str:
        if not self.multi_batch:
            return message
        progress = describe_progress(
            PublicationLedger(self.recovery_root).load(self.manifest.publication_id)
        )
        return f"{message} {progress} Retry to resume with batch {self.manifest.batch_index}.".strip()
