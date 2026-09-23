from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QTimer, Slot

from ..approval_display import approval_display
from ..commands import expand_prompt_command
from ..controllers import GenerationController
from ..extensions import load_selected_skills
from ..gateway_client import GatewayClient, GatewayConnection, GatewayError
from ..models import ServerToolOptions
from ..settings import app_data_dir, get_api_key
from ..storage import Storage
from ..transcript import compact_activity_event
from ..workers import AgentWorker, ChatWorker
from .host import BridgeHost

MODES = ("chat", "plan", "agent")
APPROVAL_DECISIONS = frozenset({"deny", "allow_once", "allow_run", "allow_rule"})


class GenerationService(QObject):
    """Chat/Plan/Agent runs: the selected mode, the session approval tier, the worker that
    streams a run, tool approvals, and the queued-input loop.

    The approval tier is session state that never outlives the workspace, the Agent mode, a
    resume, or the app: ``auto`` also stops on any run or publication failure, while
    ``accept_edits`` only ever relaxes prompts for staged file edits.
    """

    def __init__(self, host: BridgeHost, storage: Storage):
        super().__init__(host)  # type: ignore[arg-type]
        self.host = host
        self.storage = storage
        self.controller = GenerationController(storage)
        self.worker: ChatWorker | AgentWorker | None = None
        self.mode = "chat"
        self.auto_mode = False
        # Middle approval tier: staged file edits run without prompts, Bash still asks.
        self.accept_edits = False
        self.live_events: list[dict[str, object]] = []
        self._pending_approvals: dict[str, dict[str, Any]] = {}

    @property
    def running(self) -> bool:
        return self.worker is not None

    # -- mode and approval tier --------------------------------------------------------

    def approval_policy(self) -> str:
        if self.auto_mode:
            return "auto"
        return "accept_edits" if self.accept_edits else "prompt"

    def stop_auto(self) -> None:
        self.auto_mode = False

    def reset_for_workspace_change(self) -> None:
        self.auto_mode = False
        self.accept_edits = False
        self.mode = "chat"

    def select_mode(self, mode: str) -> None:
        if mode not in MODES or self.running or self.host.staging.busy:
            return
        runtime = self.host.runtime
        if mode == "chat":
            self.mode = mode
            self.auto_mode = False
            self.accept_edits = False
            self.host.stateChanged.emit()
            return
        if not runtime.workspace_path:
            self.host.errorRequested.emit("Workspace required", "Select one project workspace first.")
            return
        if not runtime.workspace_ready():
            self.host.errorRequested.emit("Workspace is still preparing", runtime.detail or "Wait for the isolated executor to become ready, then try again.")
            return
        if not runtime.mode_available(mode):
            self.host.errorRequested.emit(
                "Workspace runtime unavailable",
                "The isolated executor is not ready for the selected workspace. Retry the local runtime setup.",
            )
            return
        self.mode = mode
        if mode != "agent":
            self.auto_mode = False
            self.accept_edits = False
        self.host.stateChanged.emit()

    def _tier_available(self, capability: str) -> bool:
        runtime = self.host.runtime
        return (
            self.mode == "agent"
            and runtime.has_capability(capability)
            and runtime.workspace_ready()
            and not self.running
            and not self.host.staging.busy
        )

    def request_auto_mode(self, enabled: bool) -> None:
        if not enabled:
            self.auto_mode = False
            self.host.set_status("Auto disabled")
            return
        if not self._tier_available("agent_auto"):
            self.host.errorRequested.emit("Auto unavailable", "Select Agent mode with a ready, idle workspace and compatible gateway.")
            return

        def enable() -> None:
            self.auto_mode = True
            self.host.set_status("Auto enabled for this Agent session")

        self.host.confirm(
            f"enable-auto:{self.host.runtime.workspace_path}",
            "Enable Auto for this Agent session?",
            "Valid tools and successful staged publication will run without further approval. "
            "Workspace, command, network, resource, hash, and broker safety limits still apply. "
            "Auto stops on failure, resume, workspace change, or app restart.",
            enable,
            self.host.stateChanged.emit,
        )

    def request_accept_edits(self, enabled: bool) -> None:
        if not enabled:
            self.accept_edits = False
            self.host.set_status("Staged edits require approval again")
            return
        if not self._tier_available("agent_accept_edits"):
            self.host.errorRequested.emit("Accept edits unavailable", "Select Agent mode with a ready, idle workspace and compatible gateway.")
            return

        def enable() -> None:
            self.accept_edits = True
            self.host.set_status("Staged edits are accepted without prompts for this Agent session")

        self.host.confirm(
            f"enable-accept-edits:{self.host.runtime.workspace_path}",
            "Accept staged edits for this Agent session?",
            "write, edit, and apply_patch will change the private staged copy without asking. "
            "Bash commands still require approval, and publishing to the host is still reviewed separately. "
            "This stops on resume, mode or workspace change, or app restart.",
            enable,
            self.host.stateChanged.emit,
        )

    # -- starting runs -----------------------------------------------------------------

    def send_message(self, text: str, steer: bool = False) -> None:
        text = text.strip()
        if not text:
            return
        if self.host.staging.publishing:
            self.host.set_status("Wait for staged publication to finish…")
            return
        resolved = self._handle_prompt_command(text)
        if resolved is None:
            return
        if self.running:
            self.controller.enqueue(resolved, steered=steer, front=steer)
            self.host.set_status(f"Queued {self.controller.pending_count} message(s)")
            return
        self.start_user_turn(resolved)

    def _handle_prompt_command(self, text: str) -> str | None:
        if not text.startswith("/"):
            return text
        name, _, arguments = text[1:].partition(" ")
        name = name.casefold()
        custom = next((item for item in self.storage.list_prompt_commands() if item["name"] == name), None)
        if custom:
            expanded = expand_prompt_command(custom["template"], arguments)
            self.host.confirm(f"command:{name}:{id(expanded)}", f"Run /{name}?", expanded[:8_000], lambda: self.start_user_turn(expanded))
            return None
        history = self.host.history
        actions = {
            "new": history.new_conversation,
            "fork": history.fork,
            "compact": history.compact_context,
            "context": history.inspect_context,
            "cost": history.show_usage,
            "status": self.host.runtime.show_status,
            "stop": self.stop,
            "pause": self.pause,
        }
        if name == "mode":
            self.select_mode(arguments.strip().casefold())
        elif name in actions:
            actions[name]()
        else:
            self.host.set_status(f"Unknown command: /{name}")
        return None

    def _credentials(self, title: str, detail: str) -> tuple[str, GatewayConnection] | None:
        api_key = get_api_key()
        connection = self.host.runtime.connection()
        if not api_key or not connection:
            self.host.errorRequested.emit(title, detail)
            return None
        return api_key, connection

    def start_user_turn(self, text: str) -> None:
        history = self.host.history
        conversation = history.current()
        if conversation is None:
            history.new_conversation()
            conversation = history.current()
        api_key = get_api_key()
        if not api_key:
            self.host.errorRequested.emit("OpenRouter API key required", "Open Settings → Connection and store an API key.")
            return
        connection = self.host.runtime.connection()
        if connection is None:
            self.host.errorRequested.emit("Gateway connection required", "Open Settings → Connection and store the local gateway token.")
            return
        assert conversation is not None
        had_user = any(item.role == "user" for item in self.storage.list_all_messages(conversation.id))
        model = history.current_model()
        message = history.controller.add_user_turn(conversation.id, text, model)
        if not had_user:
            history.load(conversation.id)
        history.refresh_transcript()
        self.start_generation(api_key, connection, model, message.id, self.storage.list_messages(conversation.id))

    def edit_message(self, message_id: int, value: str) -> None:
        original = self.storage.get_message(message_id)
        if self.running or original is None or original.role != "user" or not value.strip():
            return
        credentials = self._credentials("Connection required", "Configure the gateway and API key first.")
        if credentials is None:
            return
        edited = self.storage.edit_user_message(message_id, value.strip())
        self.host.history.refresh_transcript(force_reset=True)
        self.start_generation(*credentials, self.host.history.current_model(), edited.id, self.storage.list_messages(original.conversation_id))

    def regenerate_message(self, message_id: int) -> None:
        original = self.storage.get_message(message_id)
        if self.running or original is None or original.role != "assistant":
            return
        credentials = self._credentials("Connection required", "Configure the gateway and API key first.")
        if credentials is None:
            return
        context = self.storage.list_branch_to(original.conversation_id, original.parent_message_id)
        self.start_generation(*credentials, self.host.history.current_model(), original.parent_message_id, context)

    def start_generation(self, api_key: str, connection: GatewayConnection, model: str, parent_message_id: int | None, context_messages: list[Any]) -> None:
        conversation = self.host.history.current()
        if conversation is None:
            return
        settings = self.host.settings
        prepared = self.controller.prepare(conversation, context_messages, model, settings.catalog.models())
        self.controller.begin(conversation.id, parent_message_id, model, self.mode)
        self.live_events = []
        options = settings.request_options(model)
        privacy = {"data_collection": options.data_collection, "zdr": options.zero_data_retention}
        if self.mode == "chat":
            worker: ChatWorker | AgentWorker = ChatWorker(
                GatewayClient(connection),
                api_key,
                model,
                prepared.messages,
                options,
                ServerToolOptions(**settings.server_tools()),
                prepared.supported_parameters,
            )
        else:
            runtime = self.host.runtime
            if not runtime.workspace_path:
                return
            identity = runtime.identity()
            config = self.storage.workspace_config(identity)
            selected = [str(item) for item in config.get("active_skills") or ()]
            skills = load_selected_skills(app_data_dir() / "skills", Path(runtime.workspace_path), selected)
            worker = AgentWorker.for_run(
                GatewayClient(connection),
                api_key=api_key,
                model=model,
                messages=prepared.messages,
                mode=self.mode,
                workspace_id=identity,
                approval_policy=self.approval_policy(),
                session_id=conversation.id,
                context_limit_tokens=prepared.context_limit,
                skills=skills,
                workspace_config=config,
                provider_preferences=privacy,
                investigation_model_id=settings.investigation_model or None,
            )
            worker.eventReceived.connect(self.on_agent_event)
        self._attach(worker)
        suffix = f" · compacted {prepared.removed_messages}" if prepared.removed_messages else ""
        self.host.set_status(f"≈{prepared.estimated_tokens:,} input tokens{suffix}")
        self.host.history.refresh_transcript()
        worker.start()

    def _attach(self, worker: ChatWorker | AgentWorker) -> None:
        self.worker = worker
        worker.runStarted.connect(self.on_run_started)
        worker.chunk.connect(self.on_stream_chunk)
        worker.complete.connect(self.on_stream_complete)
        worker.failed.connect(self.on_stream_error)
        worker.finished.connect(self.on_worker_finished)

    def resume_run(self, run_id: str) -> None:
        connection = self.host.runtime.connection()
        api_key = get_api_key()
        conversation = self.host.history.current()
        if self.running or not connection or not api_key or not conversation or not run_id:
            return
        # Session approval tiers never survive a resume; the gateway enforces this too.
        self.auto_mode = False
        self.accept_edits = False
        self.controller.begin(conversation.id, conversation.active_leaf_id, conversation.model, "agent")
        worker = AgentWorker.for_resume(GatewayClient(connection), run_id, api_key)
        worker.eventReceived.connect(self.on_agent_event)
        self._attach(worker)
        self.host.stateChanged.emit()
        worker.start()

    # -- run events --------------------------------------------------------------------

    @Slot(str)
    def on_run_started(self, run_id: str) -> None:
        self.controller.start_run(run_id)

    @Slot(object)
    def on_agent_event(self, event: object) -> None:
        if not hasattr(event, "type"):
            return
        event_type = str(event.type)
        payload = event.payload
        compact: dict[str, object] | None = None
        try:
            if event_type not in {"model.delta", "tool.output", "tool.completed"}:
                self.controller.save_event(event)
            compact = compact_activity_event({"run_id": event.run_id, "event_id": event.event_id, "type": event.type, "payload": payload})
            if compact is not None:
                self.live_events.append(compact)
        except (TypeError, ValueError):
            pass
        if event_type == "tool.started":
            self.host.set_status(f"Running {payload.get('tool', 'tool')} in the isolated executor…")
        elif event_type == "approval.required":
            self._request_approval(event)
        elif event_type == "checkpoint.created" and payload.get("publish_manifest"):
            self.host.staging.handle_manifest(
                payload["publish_manifest"],
                token=f"publish:{event.run_id}:{event.event_id}",
                auto=bool(payload.get("auto_publish")),
                auto_authorized=self.auto_mode and isinstance(self.worker, AgentWorker) and self.worker.auto_approve,
            )
        elif event_type == "publication.failed":
            self.auto_mode = False
            self.host.set_status("Agent completed; host publication is unavailable")
        if compact is not None:
            self.host.history.schedule_transcript_refresh()

    def _request_approval(self, event: Any) -> None:
        payload = event.payload
        approval_id = str(payload.get("approval_id") or "")
        kind = str(payload.get("kind") or "tool")
        detail = payload.get("manifest") if kind == "publish" else payload.get("arguments")
        summary, full_detail = approval_display(kind, str(payload.get("tool") or "tool"), detail)
        key = f"{event.run_id}:{approval_id}"
        self._pending_approvals[key] = {"runId": event.run_id, "approvalId": approval_id, "kind": kind}
        self.host.approvalRequested.emit(
            {
                "key": key,
                "title": "Publish staged changes?" if kind == "publish" else "Approve isolated tool?",
                "summary": summary,
                "details": full_detail,
                "allowRule": kind == "tool",
            }
        )

    def resolve_approval(self, key: str, decision: str) -> None:
        pending = self._pending_approvals.pop(key, None)
        if not pending or not isinstance(self.worker, AgentWorker):
            return
        if decision not in APPROVAL_DECISIONS:
            decision = "deny"
        try:
            self.worker.client.resolve_approval(str(pending["runId"]), str(pending["approvalId"]), decision)
        except GatewayError as exc:
            self.host.errorRequested.emit("Approval failed", str(exc))

    @Slot(str)
    def on_stream_chunk(self, text: str) -> None:
        self.controller.append(text)

    @Slot(dict, bool)
    def on_stream_complete(self, usage: dict[str, Any], cancelled: bool) -> None:
        cancelled = cancelled or self.controller.state.cancel_requested
        if cancelled and isinstance(self.worker, AgentWorker) and self.worker.auto_approve:
            self.auto_mode = False
        if usage.get("run_id") and self.controller.state.run_id is None:
            self.on_run_started(str(usage["run_id"]))
        saved = self.controller.save_assistant(usage, status="cancelled" if cancelled else "completed")
        if saved is None:
            self.controller.finish_without_message("cancelled" if cancelled else "completed")
        self.controller.state.clear_stream()
        self.live_events = []
        self.host.set_status(GenerationController.format_usage(usage, cancelled))
        self.host.history.refresh_transcript()
        self.host.stateChanged.emit()
        self.host.settingsChanged.emit()

    @Slot(str)
    def on_stream_error(self, message: str) -> None:
        if isinstance(self.worker, AgentWorker) and self.worker.auto_approve:
            self.auto_mode = False
        usage = self.worker.last_usage if isinstance(self.worker, AgentWorker) else {}
        saved = self.controller.save_assistant(usage, finish_reason="error", status="failed")
        if saved is None:
            self.controller.finish_without_message("failed", message)
        self.controller.state.clear_stream()
        self.live_events = []
        self.host.set_status("Request failed")
        self.host.history.refresh_transcript()
        self.host.stateChanged.emit()
        self.host.errorRequested.emit("Gateway request failed", message)

    @Slot()
    def on_worker_finished(self) -> None:
        if self.worker:
            self.worker.deleteLater()
        self.worker = None
        self.host.stateChanged.emit()
        if self.host.staging.publishing:
            return
        self.continue_queued_input()

    def continue_queued_input(self) -> None:
        queued = self.controller.next_input()
        if queued:
            self.host.set_status("Applying steering…" if queued.steered else "Sending queued message…")
            QTimer.singleShot(0, lambda value=queued.text: self.start_user_turn(value))

    # -- control -----------------------------------------------------------------------

    def stop(self) -> None:
        if self.worker:
            self.host.set_status("Stopping…")
            self.controller.request_cancel()
            self.worker.stop()
            self.host.stateChanged.emit()

    def pause(self) -> None:
        if not isinstance(self.worker, AgentWorker):
            self.host.set_status("No active agent to pause")
            return
        try:
            if self.worker.client.pause():
                self.host.set_status("Pausing at the next safe model boundary…")
        except GatewayError as exc:
            self.host.errorRequested.emit("Could not pause agent", str(exc))

    def shutdown(self) -> None:
        if self.worker:
            self.worker.stop()
            self.worker.wait(1500)
