from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

from shared.events import EventEnvelope
from shared.permissions import PermissionDecision
from shared.requests import AgentRunRequest, ChatRequest
from shared.responses import GatewayStatus
from shared.tools import PublishManifest

from .agent.approvals import ApprovalCoordinator
from .agent.runtime import AgentRuntime
from .config import GatewayConfig
from .executor import ExecutorClient
from .extensions.capabilities import discover_custom_capabilities
from .journal import JournalStore
from .openrouter.catalog import GatewayCatalog
from .openrouter.client import OpenRouterGatewayClient
from .openrouter.errors import ProviderError


class GatewayServiceError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False, status: int = 400):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.status = status


class GatewayService:
    def __init__(
        self,
        config: GatewayConfig,
        *,
        provider_factory: Callable[[], OpenRouterGatewayClient] = OpenRouterGatewayClient,
        journal: JournalStore | None = None,
        catalog: GatewayCatalog | None = None,
    ):
        self.config = config
        self.journal = journal or JournalStore(
            config.journal_path,
            max_events_per_run=config.max_events_per_run,
            max_runs=config.max_journal_runs,
        )
        self.catalog = catalog or GatewayCatalog(self.journal, config.catalog_ttl_seconds)
        self.provider_factory = provider_factory
        self._client_openrouter_key: str | None = None
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._pause_events: dict[str, asyncio.Event] = {}
        self._conditions: dict[str, asyncio.Condition] = {}
        self.approvals = ApprovalCoordinator()
        self.executor = (
            ExecutorClient(config.executor_socket, config.executor_token)
            if config.executor_socket and config.executor_token else None
        )
        self.agent_runtime = (
            AgentRuntime(
                self.executor,
                self.approvals,
                self._append,
                lambda workspace: tuple(self.journal.permission_rules(workspace)),
                self._save_snapshot,
                self._save_session,
                lambda rule_id: self.journal.touch_permission_rule(rule_id, utc_now()),
                lambda run_id: bool(
                    self._pause_events.get(run_id) and self._pause_events[run_id].is_set()
                ),
            )
            if self.executor
            else None
        )
        self.journal.recover_interrupted_runs(utc_now())

    @property
    def openrouter_key(self) -> str | None:
        return self._client_openrouter_key or self.config.container_openrouter_key

    def configure_client_key(self, api_key: str) -> None:
        value = api_key.strip()
        if len(value) < 8:
            raise GatewayServiceError("provider.invalid_key", "Enter a valid OpenRouter API key.")
        self._client_openrouter_key = value

    def clear_client_key(self) -> None:
        self._client_openrouter_key = None

    def status(self) -> GatewayStatus:
        executor_ready = bool(
            self.agent_runtime and self.config.workspace_id and self.config.executor_socket
            and self.config.executor_socket.exists()
        )
        local_tools = (
            ("read", "write", "edit", "bash", "grep", "find", "ls")
            if executor_ready
            else ()
        )
        capabilities = tuple(
            {
                "name": name,
                "kind": "workspace_tool",
                "modes": ["plan", "agent"] if name in {"read", "grep", "find", "ls"} else ["agent"],
                "requires_approval": name in {"write", "edit", "bash"},
                "custom": False,
            }
            for name in local_tools
        )
        if executor_ready:
            capabilities += (
                {
                    "name": "agent_auto",
                    "kind": "approval_policy",
                    "modes": ["agent"],
                    "requires_approval": False,
                    "custom": False,
                },
            )
        if executor_ready and self.config.workspace_id:
            capabilities += discover_custom_capabilities(
                self.journal.load_workspace_config(self.config.workspace_id)
            )
        return GatewayStatus(
            ready=True,
            supported_tools=(
                "openrouter:web_search", "openrouter:web_fetch", "openrouter:datetime",
                *local_tools,
            ),
            supported_modes=("chat", "plan", "agent") if executor_ready else ("chat",),
            model_catalog_age_seconds=self.catalog.cached_age_seconds(),
            executor_present=executor_ready,
            executor_status="ready" if executor_ready else "unavailable",
            active_workspace=self.config.workspace_id if executor_ready else None,
            openrouter_key_configured=bool(self.openrouter_key),
            capabilities=capabilities,
            resumable_runs=tuple(self.journal.resumable_runs()),
        )

    async def discard_staging(self, workspace_id: str) -> dict[str, Any]:
        if not self.executor or workspace_id != self.config.workspace_id:
            raise GatewayServiceError(
                "workspace.invalid",
                "The selected workspace is not the active isolated executor.",
                status=409,
            )
        try:
            return await self.executor.discard_staging()
        except Exception as exc:
            raise GatewayServiceError(
                "staging.discard_failed",
                "The executor could not reseed the staging workspace.",
                retryable=True,
                status=409,
            ) from exc

    async def mark_staging_published(self, workspace_id: str, manifest: PublishManifest) -> dict[str, Any]:
        if not self.executor or workspace_id != self.config.workspace_id:
            raise GatewayServiceError(
                "workspace.invalid",
                "The selected workspace is not the active isolated executor.",
                status=409,
            )
        if manifest.workspace_id != workspace_id:
            raise GatewayServiceError(
                "workspace.invalid",
                "The publication manifest belongs to a different workspace.",
                status=409,
            )
        try:
            return await self.executor.mark_staging_published(manifest)
        except Exception as exc:
            raise GatewayServiceError(
                "staging.baseline_failed",
                "The executor could not advance the staging baseline.",
                retryable=True,
                status=409,
            ) from exc

    async def inspect_staging(self, workspace_id: str) -> dict[str, Any]:
        if not self.executor or workspace_id != self.config.workspace_id:
            raise GatewayServiceError(
                "workspace.invalid",
                "The selected workspace is not the active isolated executor.",
                status=409,
            )
        status = await self.executor.status()
        environment = status.get("environment") if isinstance(status, dict) else {}
        if not isinstance(environment, dict):
            raise GatewayServiceError(
                "staging.inspect_failed",
                "The executor returned invalid staging status.",
                retryable=True,
                status=409,
            )
        snapshot = environment.get("agent_snapshot")
        if not isinstance(snapshot, dict):
            snapshot = {}
        return {
            "request_id": "staging-status",
            "ok": True,
            "output": "Staging is dirty." if environment.get("unpublished_changes") else "Staging is clean.",
            "data": {
                "unpublished_changes": bool(environment.get("unpublished_changes")),
                "total_known": int(snapshot.get("file_count") or 0),
                "snapshot_id": snapshot.get("snapshot_id"),
                "file_count": int(snapshot.get("file_count") or 0),
                "total_bytes": int(snapshot.get("total_bytes") or 0),
            },
        }

    async def get_models(self, *, refresh: bool = False) -> tuple[list[dict[str, Any]], str]:
        return await self.catalog.models(self.openrouter_key, refresh=refresh)

    async def start_chat(self, request: ChatRequest) -> str:
        if not self.openrouter_key:
            raise GatewayServiceError(
                "provider.key_required",
                "Configure an OpenRouter key before sending a message.",
                status=409,
            )
        run_id = str(__import__('uuid').uuid4())
        timestamp = utc_now()
        self.journal.create_run(run_id, "chat", timestamp)
        self._conditions[run_id] = asyncio.Condition()
        self._cancel_events[run_id] = asyncio.Event()
        self._pause_events[run_id] = asyncio.Event()
        await self._append(run_id, "run.created", {"mode": "chat", "client_request_id": request.client_request_id})
        self._tasks[run_id] = asyncio.create_task(self._run_chat(run_id, request), name=f"chat-{run_id}")
        return run_id

    async def start_agent(self, request: AgentRunRequest) -> str:
        if not self.openrouter_key:
            raise GatewayServiceError("provider.key_required", "Configure an OpenRouter key first.", status=409)
        if not self.agent_runtime or request.workspace_id != self.config.workspace_id:
            raise GatewayServiceError("workspace.invalid", "Recreate the executor for the selected workspace.", status=409)
        if request.approval_policy == "auto":
            assert self.executor is not None
            inspection = await self.executor.status()
            environment = inspection.get("environment") if isinstance(inspection, dict) else {}
            if not isinstance(environment, dict):
                raise GatewayServiceError("staging.inspect_failed", "The executor returned invalid staging status.", retryable=True, status=409)
            if environment.get("unpublished_changes"):
                raise GatewayServiceError(
                    "staging.not_clean",
                    "Auto requires clean staging. Review, publish, or discard existing staged changes first.",
                    status=409,
                )
        if request.workspace_config:
            self.journal.save_workspace_config(
                request.workspace_id, request.workspace_config, utc_now()
            )
        else:
            data = request.to_dict()
            data["workspace_config"] = self.journal.load_workspace_config(request.workspace_id)
            request = AgentRunRequest.from_dict(data)
        run_id = str(__import__('uuid').uuid4())
        timestamp = utc_now()
        self.journal.create_run(run_id, request.mode, timestamp)
        self._conditions[run_id] = asyncio.Condition()
        self._cancel_events[run_id] = asyncio.Event()
        self._pause_events[run_id] = asyncio.Event()
        await self._append(
            run_id,
            "run.created",
            {
                "mode": request.mode,
                "workspace_id": request.workspace_id,
                "approval_policy": request.approval_policy,
            },
        )
        prepared, replayed = self._prepare_agent_context(request)
        if replayed:
            await self._append(
                run_id,
                "session.replayed",
                {
                    "session_id": request.session_id,
                    "restored_messages": len(prepared) - len(request.messages),
                },
            )
        self.journal.save_run_snapshot(
            run_id,
            request.to_dict(),
            prepared,
            [dict(item) for item in request.messages],
            {"iterations": 0, "cost": 0.0, "elapsed_seconds": 0.0},
            safe_to_resume=True,
            updated_at=timestamp,
        )
        self._tasks[run_id] = asyncio.create_task(
            self._run_agent(
                run_id,
                request,
                initial_messages=prepared,
                visible_messages=[dict(item) for item in request.messages],
            ),
            name=f"agent-{run_id}",
        )
        return run_id

    async def resume_agent(self, run_id: str) -> bool:
        if not self.openrouter_key or self.agent_runtime is None:
            raise GatewayServiceError(
                "agent.unavailable", "Configure the key and executor before resuming.", status=409
            )
        if run_id in self._tasks:
            return False
        run = self.journal.get_run(run_id)
        snapshot = self.journal.load_run_snapshot(run_id)
        if not run or run.get("state") != "paused" or not snapshot or not snapshot["safe_to_resume"]:
            raise GatewayServiceError("run.not_resumable", "Run is not at a safe resume point.", status=409)
        request = AgentRunRequest.from_dict(snapshot["request"])
        if request.approval_policy == "auto":
            data = request.to_dict()
            data["approval_policy"] = "prompt"
            request = AgentRunRequest.from_dict(data)
        if request.workspace_id != self.config.workspace_id:
            raise GatewayServiceError("workspace.invalid", "Resume requires the original workspace.", status=409)
        self._conditions[run_id] = asyncio.Condition()
        self._cancel_events[run_id] = asyncio.Event()
        self._pause_events[run_id] = asyncio.Event()
        self._tasks[run_id] = asyncio.create_task(
            self._run_agent(
                run_id,
                request,
                initial_messages=[dict(item) for item in snapshot["messages"]],
                visible_messages=[dict(item) for item in snapshot["visible_messages"]],
                budget_state=dict(snapshot["budget"]),
                resumed=True,
            ),
            name=f"agent-resume-{run_id}",
        )
        return True

    def resolve_approval(self, run_id: str, approval_id: str, decision: str) -> bool:
        try:
            parsed = PermissionDecision(decision)
        except ValueError as exc:
            raise GatewayServiceError("approval.invalid", "Unknown approval decision.", status=422) from exc
        if parsed not in {PermissionDecision.DENY, PermissionDecision.ALLOW_ONCE, PermissionDecision.ALLOW_RUN, PermissionDecision.ALLOW_RULE}:
            raise GatewayServiceError("approval.invalid_scope", "That approval scope is unavailable.", status=422)
        pending = self.approvals.get(run_id, approval_id)
        if parsed == PermissionDecision.ALLOW_RULE:
            if not pending or not pending.request or not pending.workspace_id:
                raise GatewayServiceError("approval.invalid_scope", "Publish approvals cannot become saved rules.", status=422)
            self.journal.save_permission_rule(self.journal.rule_for_request(pending.request, pending.workspace_id))
        return self.approvals.resolve(run_id, approval_id, parsed)

    async def cancel(self, run_id: str) -> bool:
        if self.journal.get_run(run_id) is None:
            return False
        cancel_event = self._cancel_events.get(run_id)
        if cancel_event:
            cancel_event.set()
        task = self._tasks.get(run_id)
        if self.agent_runtime:
            await self.agent_runtime.cancel(run_id)
        if task and not task.done():
            task.cancel()
        return True

    def pause(self, run_id: str) -> bool:
        run = self.journal.get_run(run_id)
        event = self._pause_events.get(run_id)
        if not run or not event or run.get("mode") not in {"plan", "agent"}:
            return False
        event.set()
        return True

    async def events(self, run_id: str, after_id: int = 0) -> AsyncIterator[EventEnvelope | None]:
        if self.journal.get_run(run_id) is None:
            raise GatewayServiceError("run.not_found", "Run not found.", status=404)
        cursor = max(0, after_id)
        while True:
            events = self.journal.events_after(run_id, cursor)
            for event in events:
                cursor = event.event_id
                yield event
            if self.journal.is_terminal(run_id):
                return
            condition = self._conditions.setdefault(run_id, asyncio.Condition())
            try:
                async with condition:
                    if self.journal.events_after(run_id, cursor) or self.journal.is_terminal(run_id):
                        continue
                    await asyncio.wait_for(condition.wait(), timeout=15.0)
            except TimeoutError:
                yield None

    async def close(self) -> None:
        tasks = [task for task in self._tasks.values() if not task.done()]
        for event in self._pause_events.values():
            event.set()
        if tasks:
            _done, pending = await asyncio.wait(tasks, timeout=1.0)
            tasks = list(pending)
        if self.agent_runtime:
            for run_id in tuple(self._tasks):
                await self.agent_runtime.cancel(run_id)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._client_openrouter_key = None
        self.journal.close()

    async def _run_chat(self, run_id: str, request: ChatRequest) -> None:
        cancel_event = self._cancel_events[run_id]
        key = self.openrouter_key
        if key is None:
            await self._append(run_id, "run.failed", {"code": "provider.key_required", "message": "OpenRouter key unavailable.", "retryable": False})
            return
        try:
            await self._append(run_id, "run.started", {"mode": "chat"})
            await self._append(run_id, "model.requested", {"model": request.model})
            async for provider_event in self.provider_factory().stream_chat(request, key, cancel_event):
                if provider_event.text:
                    await self._append(run_id, "model.delta", {"text": provider_event.text})
                if provider_event.done:
                    usage = dict(provider_event.usage)
                    if provider_event.model_id:
                        usage["actual_model"] = provider_event.model_id
                    if provider_event.provider_id:
                        usage["provider"] = provider_event.provider_id
                    if provider_event.finish_reason:
                        usage["finish_reason"] = provider_event.finish_reason
                    if usage:
                        await self._append(run_id, "usage.updated", usage)
                    await self._append(run_id, "model.completed", usage)
            if cancel_event.is_set():
                await self._append(run_id, "run.cancelled", {"partial_output_preserved": True})
            else:
                await self._append(run_id, "run.completed", {})
        except asyncio.CancelledError:
            cancel_event.set()
            if not self.journal.is_terminal(run_id):
                await self._append(run_id, "run.cancelled", {"partial_output_preserved": True})
        except ProviderError as exc:
            payload = {"code": exc.code, "message": str(exc), "retryable": exc.retryable}
            await self._append(run_id, "model.failed", payload)
            await self._append(run_id, "run.failed", payload)
        except Exception:
            payload = {"code": "gateway.internal", "message": "The gateway could not complete the request.", "retryable": False}
            await self._append(run_id, "model.failed", payload)
            await self._append(run_id, "run.failed", payload)
        finally:
            self._tasks.pop(run_id, None)
            self._cancel_events.pop(run_id, None)
            self._pause_events.pop(run_id, None)

    async def _run_agent(
        self,
        run_id: str,
        request: AgentRunRequest,
        *,
        initial_messages: list[dict[str, Any]] | None = None,
        visible_messages: list[dict[str, Any]] | None = None,
        budget_state: dict[str, Any] | None = None,
        resumed: bool = False,
    ) -> None:
        key = self.openrouter_key
        if key is None or self.agent_runtime is None:
            await self._append(run_id, "run.failed", {"code": "agent.unavailable", "message": "Agent runtime unavailable.", "retryable": False})
            return
        try:
            await self.agent_runtime.run(
                run_id,
                request,
                key,
                initial_messages=initial_messages,
                visible_messages=visible_messages,
                budget_state=budget_state,
                resumed=resumed,
            )
        except asyncio.CancelledError:
            if not self.journal.is_terminal(run_id):
                await self._append(run_id, "run.cancelled", {"partial_output_preserved": True, "staging_preserved_until_executor_stop": True})
        finally:
            self._tasks.pop(run_id, None)
            self._cancel_events.pop(run_id, None)
            self._pause_events.pop(run_id, None)

    def _prepare_agent_context(
        self, request: AgentRunRequest
    ) -> tuple[list[dict[str, Any]], bool]:
        incoming = [dict(item) for item in request.messages]
        if not request.session_id:
            return incoming, False
        session = self.journal.load_agent_session(request.session_id)
        if (
            not session
            or session["workspace_id"] != request.workspace_id
            or session["mode"] != request.mode
        ):
            return incoming, False
        previous = session["visible_messages"]
        if not isinstance(previous, list) or incoming[: len(previous)] != previous:
            return incoming, False
        restored = [dict(item) for item in session["internal_messages"]]
        restored.extend(incoming[len(previous) :])
        return restored, True

    def _save_snapshot(
        self,
        run_id: str,
        request: dict[str, Any],
        messages: list[dict[str, Any]],
        visible_messages: list[dict[str, Any]],
        budget: dict[str, Any],
        safe_to_resume: bool,
    ) -> None:
        self.journal.save_run_snapshot(
            run_id,
            request,
            messages,
            visible_messages,
            budget,
            safe_to_resume=safe_to_resume,
            updated_at=utc_now(),
        )

    def _save_session(
        self,
        session_id: str,
        workspace_id: str,
        mode: str,
        internal_messages: list[dict[str, Any]],
        visible_messages: list[dict[str, Any]],
    ) -> None:
        self.journal.save_agent_session(
            session_id,
            workspace_id,
            mode,
            internal_messages,
            visible_messages,
            utc_now(),
        )

    async def _append(self, run_id: str, event_type: str, payload: dict[str, Any]) -> EventEnvelope:
        event = self.journal.append(run_id, event_type, utc_now(), payload)
        condition = self._conditions.get(run_id)
        if condition:
            async with condition:
                condition.notify_all()
        return event


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
