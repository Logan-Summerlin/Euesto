from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from PySide6.QtCore import Property, QObject, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QGuiApplication, QWindow

from shared.coercion import optional_float, optional_int
from shared.tools import PublishManifest

from .approval_display import approval_display
from .commands import expand_prompt_command
from .connection import GatewayHealthWorker, HealthResult, HealthState
from .context_utils import compact_messages, estimate_tokens
from .controllers import ConversationController, GenerationController
from .extensions import available_skills, load_selected_skills
from .gateway_client import (
    DEFAULT_GATEWAY_URL,
    GatewayClient,
    GatewayConnection,
    GatewayError,
)
from .import_export import ImportExportError, export_to_file, import_from_file
from .markdown_renderer import render_markdown
from .model_catalog import ModelCatalog, matches_model_filters
from .models import (
    DEFAULT_MODELS,
    Conversation,
    RequestOptions,
    ServerToolOptions,
    model_context_length,
)
from .runtime_manager import RuntimeManager, RuntimeResult
from .settings import (
    app_data_dir,
    database_path,
    get_api_key,
    get_gateway_session_token,
    get_gateway_token,
    save_api_key,
    save_gateway_token,
)
from .storage import Storage
from .transcript import (
    ACTIVITY_EVENT_TYPES,
    ACTIVITY_PAYLOAD_KEYS,
    assemble_activities,
    assemble_transcript,
    compact_activity_event,
)
from .transcript_model import TranscriptListModel
from .window_services import GlobalQuickChatHotkey, TrayService
from .workers import (
    AgentWorker,
    CatalogWorker,
    ChatWorker,
    PublicationWorker,
    StagingDiscardWorker,
    StagingInspectWorker,
)
from .workspace_broker import BrokerError, canonical_workspace, workspace_id


class DesktopBridge(QObject):
    conversationsChanged = Signal(); transcriptChanged = Signal(); modelsChanged = Signal(); stateChanged = Signal(); settingsChanged = Signal(); permissionsChanged = Signal(); commandsChanged = Signal(); skillsChanged = Signal(); presetsChanged = Signal(); focusComposerRequested = Signal(); infoRequested = Signal(str, str); errorRequested = Signal(str, str); runtimeSetupStarted = Signal(); runtimeSetupFinished = Signal(bool); confirmRequested = Signal(str, str, str); approvalRequested = Signal("QVariantMap"); fileExported = Signal(str); fileImported = Signal(str)

    def __init__(self, storage: Storage | None = None):
        super().__init__(); self.storage = storage or Storage(database_path()); self.catalog = ModelCatalog(self.storage); self.conversation_controller = ConversationController(self.storage); self.generation = GenerationController(self.storage); self.worker: ChatWorker | AgentWorker | None = None; self.catalog_worker: CatalogWorker | None = None; self.staging_discard_worker: StagingDiscardWorker | None = None; self.staging_inspect_worker: StagingInspectWorker | None = None; self.publication_worker: PublicationWorker | None = None; self.health_worker: GatewayHealthWorker | None = None; self.runtime_manager = RuntimeManager(app_data_dir(), self); self.runtime_automatic = bool(getattr(sys, "frozen", False)); self.current_conversation_id: str | None = None; self.current_mode = "chat"; self._auto_mode = False; self.workspace_path = self.storage.get_setting("workspace_path", "") or ""; self.show_archived = False; self.search_query = ""; self.status_text = "Ready"; self.gateway_text = "Gateway: checking"; self.gateway_detail = ""; self.gateway_health_state = HealthState.STARTING; self.last_gateway_status: object | None = None; self.runtime_state = "starting" if self.runtime_automatic else "manual"; self.runtime_detail = "Preparing the local runtime…" if self.runtime_automatic else "The developer runtime is managed by scripts."; self.runtime_target_identity: str | None = None; self.live_events: list[dict[str, object]] = []; self._conversations: list[dict[str, Any]] = []; self._transcript: list[dict[str, Any]] = []; self._transcript_model = TranscriptListModel(self); self._transcript_html_cache: dict[tuple[str, str], str] = {}; self._models: list[dict[str, Any]] = []; self._permissions: list[dict[str, Any]] = []; self._commands: list[dict[str, Any]] = []; self._skills: list[dict[str, Any]] = []; self._presets: list[dict[str, Any]] = []; self._pending_confirmation: dict[str, tuple[str, Any]] = {}; self._pending_approvals: dict[str, dict[str, Any]] = {}; self._gateway_token = get_gateway_session_token() or get_gateway_token() or ""; self._window: QWindow | None = None; self.tray: TrayService | None = None; self.hotkey: GlobalQuickChatHotkey | None = None; self._closing = False; self._catalog_autorefresh_attempted = False; self._gateway_recheck_requested = False; self._transcript_refresh_timer = QTimer(self); self._transcript_refresh_timer.setSingleShot(True); self._transcript_refresh_timer.setInterval(50); self._transcript_refresh_timer.timeout.connect(self._refresh_transcript); self._reload_models(); self._reload_commands(); self._reload_presets(); self._load_conversations(); self.health_timer = QTimer(self); self.health_timer.setInterval(10_000); self.health_timer.timeout.connect(self.checkGateway); self.health_timer.start(); self.runtime_manager.progressChanged.connect(self._runtime_progress); self.runtime_manager.ready.connect(self._runtime_ready); self.runtime_manager.failed.connect(self._runtime_failed); self.runtime_manager.setupStarted.connect(self.runtimeSetupStarted); self.runtime_manager.setupFinished.connect(self.runtimeSetupFinished); QTimer.singleShot(0, self._start_runtime_for_saved_workspace if self.runtime_automatic else self.checkGateway)

    @Property("QVariantList", notify=conversationsChanged)
    def conversations(self): return self._conversations
    @Property("QVariantList", notify=transcriptChanged)
    def transcript(self): return self._transcript
    @Property(QObject, constant=True)
    def transcriptModel(self): return self._transcript_model
    @Property("QVariantList", notify=modelsChanged)
    def models(self): return self._models
    @Property("QVariantList", notify=permissionsChanged)
    def permissions(self): return self._permissions
    @Property("QVariantList", notify=commandsChanged)
    def commands(self): return self._commands
    @Property("QVariantList", notify=skillsChanged)
    def skills(self): return self._skills
    @Property("QVariantList", notify=presetsChanged)
    def presets(self): return self._presets
    @Property(str, notify=stateChanged)
    def currentConversationId(self): return self.current_conversation_id or ""
    @Property(str, notify=stateChanged)
    def currentTitle(self):
        conversation = self.current_conversation(); return conversation.title if conversation else ""
    @Property(str, notify=stateChanged)
    def currentModel(self):
        conversation = self.current_conversation(); return conversation.model if conversation else DEFAULT_MODELS[0].id
    @Property(str, notify=stateChanged)
    def currentMode(self): return self.current_mode
    @Property(bool, notify=stateChanged)
    def autoModeEnabled(self): return self._auto_mode
    @Property(bool, notify=stateChanged)
    def autoModeAvailable(self):
        status = self.last_gateway_status; capabilities = getattr(status, "capabilities", ()) if status else ()
        return any(isinstance(item, dict) and item.get("name") == "agent_auto" for item in capabilities)
    @Property(str, notify=stateChanged)
    def workspacePath(self): return self.workspace_path
    @Property(str, notify=stateChanged)
    def statusText(self): return self.status_text
    @Property(str, notify=stateChanged)
    def gatewayText(self): return self.gateway_text
    @Property(str, notify=stateChanged)
    def gatewayDetail(self): return self.gateway_detail
    @Property(str, notify=stateChanged)
    def runtimeState(self): return self.runtime_state
    @Property(str, notify=stateChanged)
    def runtimeDetail(self): return self.runtime_detail
    @Property(bool, notify=stateChanged)
    def runtimeBusy(self): return self.runtime_state in {"starting", "stopping", "pulling", "building", "checking"}
    @Property(bool, notify=stateChanged)
    def workspaceReady(self): return self._workspace_status_ready(self.last_gateway_status)
    @Property(bool, notify=stateChanged)
    def generating(self): return self.worker is not None
    @Property(bool, notify=stateChanged)
    def stagingBusy(self): return any((self.staging_discard_worker, self.staging_inspect_worker, self.publication_worker))
    @Property(bool, notify=stateChanged)
    def archivedView(self): return self.show_archived
    @Property(str, notify=settingsChanged)
    def theme(self): return self.storage.get_setting("theme", "dark") or "dark"
    @Property(str, notify=settingsChanged)
    def systemPrompt(self):
        conversation = self.current_conversation(); return conversation.system_prompt if conversation else ""
    @Property(str, notify=settingsChanged)
    def reasoningEffort(self): return GenerationController.request_options(self.storage, self.currentModel).reasoning_effort or "default"
    @Property("QVariantMap", notify=settingsChanged)
    def modelOptions(self): return asdict(GenerationController.request_options(self.storage, self.currentModel))
    @Property("QVariantMap", notify=settingsChanged)
    def serverTools(self): return {key: self.storage.get_setting(f"server_tool:{key}", "0") == "1" for key in ("web_search", "web_fetch", "datetime")}
    @Property("QVariantMap", notify=settingsChanged)
    def gatewaySettings(self): return {"url": self.storage.get_setting("gateway_url", DEFAULT_GATEWAY_URL) or DEFAULT_GATEWAY_URL, "hasToken": bool(self._gateway_token), "hasApiKey": bool(get_api_key())}
    @Property("QVariantMap", notify=settingsChanged)
    def usage(self): return dict(self.storage.usage_summary(self.current_conversation_id))

    @Slot(QWindow)
    def attachWindow(self, window: QWindow) -> None:
        if self._window is window: return
        self._window = window; self.tray = TrayService(window, show_quick_chat=self.showQuickChat, toggle_visibility=self.toggleWindowVisibility, close=self.shutdown); self.hotkey = GlobalQuickChatHotkey(window, self.showQuickChat); application = QGuiApplication.instance()
        if application: application.installNativeEventFilter(self.hotkey)
        self.hotkey.register()

    @Slot()
    def newConversation(self) -> None:
        if self.worker: return
        conversation = self.conversation_controller.create_new(self.current_conversation(), self.storage.recent_model_ids(1)); self.current_conversation_id = conversation.id; self.show_archived = False; self.search_query = ""; self._load_conversations(conversation.id); self.focusComposerRequested.emit()

    @Slot(str)
    def selectConversation(self, conversation_id: str) -> None:
        if self.worker or not self.storage.get_conversation(conversation_id): return
        self.current_conversation_id = conversation_id; self._refresh_transcript(force_reset=True); self.stateChanged.emit(); self.settingsChanged.emit()

    @Slot(str)
    def setConversationSearch(self, query: str) -> None: self.search_query = query.strip(); self._load_conversations()

    @Slot()
    def toggleArchived(self) -> None:
        if self.worker: return
        self.show_archived = not self.show_archived; self.current_conversation_id = None; self._load_conversations(); self._set_status("Archived conversations" if self.show_archived else "Ready")

    @Slot(str)
    def renameConversation(self, title: str) -> None:
        conversation = self.current_conversation(); title = title.strip()
        if conversation and title and not self.worker: self.storage.update_conversation(conversation.id, title=title[:200]); self._load_conversations(conversation.id)

    @Slot()
    def togglePin(self) -> None:
        conversation = self.current_conversation()
        if conversation and not self.worker: self.storage.pin_conversation(conversation.id, not bool(conversation.pinned_at)); self._load_conversations(conversation.id)
    @Slot()
    def toggleArchive(self) -> None:
        conversation = self.current_conversation()
        if conversation and not self.worker: self.storage.archive_conversation(conversation.id, not bool(conversation.archived_at)); self.current_conversation_id = None; self._load_conversations()
    @Slot()
    def requestDeleteConversation(self) -> None:
        conversation = self.current_conversation()
        if not conversation or self.worker: return
        token = f"delete:{conversation.id}"; self._pending_confirmation[token] = ("delete", conversation.id); self.confirmRequested.emit(token, "Delete conversation?", f"Delete “{conversation.title}”? This cannot be undone.")

    @Slot(str, bool)
    def resolveConfirmation(self, token: str, accepted: bool) -> None:
        pending = self._pending_confirmation.pop(token, None)
        if not pending: return
        action, value = pending
        if not accepted:
            if action == "enable-auto": self.stateChanged.emit()
            return
        if action == "delete": self.storage.delete_conversation(str(value)); self.current_conversation_id = None; self._load_conversations()
        elif action == "custom-command": self._start_user_turn(str(value))
        elif action == "publish": self._start_publication(value, auto=False)
        elif action == "discard-staging": self._discard_staging()
        elif action == "enable-auto": self._auto_mode = True; self._set_status("Auto enabled for this Agent session"); self.stateChanged.emit()

    def _start_publication(self, value: object, *, auto: bool) -> None:
        try:
            manifest = value if isinstance(value, PublishManifest) else PublishManifest.from_dict(value)
            if not self.workspace_path: raise BrokerError("Select the workspace that produced this manifest first")
            if self.publication_worker: raise BrokerError("Another publication is already running")
            connection = self._gateway_connection()
            if connection is None: raise BrokerError("Publication requires the local gateway")
            self.publication_worker = PublicationWorker(manifest, Path(self.workspace_path), app_data_dir() / "recovery", reseed_client=GatewayClient(connection))
            self.publication_worker.complete.connect(self._on_publication_complete); self.publication_worker.failed.connect(self._on_publication_failed); self.publication_worker.finished.connect(self._on_publication_finished); self._set_status("Publishing validated staged changes…"); self.stateChanged.emit(); self.publication_worker.start()
        except (BrokerError, OSError, TypeError, ValueError) as exc:
            if auto: self._auto_mode = False; self.stateChanged.emit()
            self.errorRequested.emit("Publication blocked", str(exc))

    @Slot(dict)
    def _on_publication_complete(self, result: dict[str, Any]) -> None:
        completed = result.get("completed_paths") or (); checkpoint = str(result.get("checkpoint_id") or ""); self._set_status(f"Published {len(completed)} file(s); checkpoint {checkpoint[:8]}")
        if result.get("reseed_error"): self._auto_mode = False; self.errorRequested.emit("Published, but Auto stopped", "Host files were updated, but staging could not be reseeded: " + str(result["reseed_error"]))
        self.stateChanged.emit()

    @Slot(str)
    def _on_publication_failed(self, message: str) -> None: self._auto_mode = False; self.stateChanged.emit(); self.errorRequested.emit("Publication blocked", message)

    @Slot()
    def _on_publication_finished(self) -> None:
        if self.publication_worker: self.publication_worker.deleteLater()
        self.publication_worker = None; self.stateChanged.emit()
        if self.worker is None: self._continue_queued_input()

    @Slot(str, bool)
    def setServerTool(self, name: str, enabled: bool) -> None:
        if name in {"web_search", "web_fetch", "datetime"}: self.storage.set_setting(f"server_tool:{name}", "1" if enabled else "0"); self.settingsChanged.emit()

    @Slot(bool)
    def requestAutoMode(self, enabled: bool) -> None:
        if not enabled: self._auto_mode = False; self._set_status("Auto disabled"); self.stateChanged.emit(); return
        if self.current_mode != "agent" or not self.autoModeAvailable or not self.workspaceReady or self.worker or self.stagingBusy:
            self.errorRequested.emit("Auto unavailable", "Select Agent mode with a ready, idle workspace and compatible gateway."); return
        token = f"enable-auto:{self.workspace_path}"; self._pending_confirmation[token] = ("enable-auto", None); self.confirmRequested.emit(token, "Enable Auto for this Agent session?", "Valid tools and successful staged publication will run without further approval. Workspace, command, network, resource, hash, and broker safety limits still apply. Auto stops on failure, resume, workspace change, or app restart.")

    @Slot(str, bool)
    def sendMessage(self, text: str, steer: bool = False) -> None:
        text = text.strip()
        if not text: return
        if self.publication_worker: self._set_status("Wait for staged publication to finish…"); return
        resolved = self._handle_prompt_command(text)
        if resolved is None: return
        if self.worker: self.generation.enqueue(resolved, steered=steer, front=steer); self._set_status(f"Queued {self.generation.pending_count} message(s)"); return
        self._start_user_turn(resolved)

    def _handle_prompt_command(self, text: str) -> str | None:
        if not text.startswith("/"): return text
        name, _, arguments = text[1:].partition(" "); name = name.casefold(); custom = next((item for item in self.storage.list_prompt_commands() if item["name"] == name), None)
        if custom:
            expanded = expand_prompt_command(custom["template"], arguments); token = f"command:{name}:{len(self._pending_confirmation)}"; self._pending_confirmation[token] = ("custom-command", expanded); self.confirmRequested.emit(token, f"Run /{name}?", expanded[:8_000]); return None
        actions = {"new": self.newConversation, "fork": self.forkConversation, "compact": self.compactContext, "context": self.inspectContext, "cost": self.showUsage, "status": self.showGatewayStatus, "stop": self.stopGeneration, "pause": self.pauseAgent}
        if name == "mode": self.selectMode(arguments.strip().casefold())
        elif name in actions: actions[name]()
        else: self._set_status(f"Unknown command: /{name}")
        return None

    def _start_user_turn(self, text: str) -> None:
        conversation = self.current_conversation()
        if conversation is None: self.newConversation(); conversation = self.current_conversation()
        api_key = get_api_key()
        if not api_key: self.errorRequested.emit("OpenRouter API key required", "Open Settings → Connection and store an API key."); return
        connection = self._gateway_connection()
        if connection is None: self.errorRequested.emit("Gateway connection required", "Open Settings → Connection and store the local gateway token."); return
        assert conversation is not None
        had_user = any(item.role == "user" for item in self.storage.list_all_messages(conversation.id)); message = self.conversation_controller.add_user_turn(conversation.id, text, self.currentModel)
        if not had_user: self._load_conversations(conversation.id)
        self._refresh_transcript(); self._start_generation(api_key, connection, self.currentModel, message.id, self.storage.list_messages(conversation.id))

    def _start_generation(self, api_key: str, connection: GatewayConnection, model: str, parent_message_id: int | None, context_messages: list[Any]) -> None:
        conversation = self.current_conversation()
        if conversation is None: return
        prepared = self.generation.prepare(conversation, context_messages, model, self.catalog.models()); self.generation.begin(conversation.id, parent_message_id, model, self.current_mode); self.live_events = []; options = GenerationController.request_options(self.storage, model); privacy = {"data_collection": options.data_collection, "zdr": options.zero_data_retention}
        if self.current_mode == "chat":
            worker: ChatWorker | AgentWorker = ChatWorker(GatewayClient(connection), api_key, model, prepared.messages, options, ServerToolOptions(**self.serverTools), prepared.supported_parameters)
        else:
            if not self.workspace_path: return
            identity = workspace_id(Path(self.workspace_path)); config = self.storage.workspace_config(identity); selected = [str(item) for item in config.get("active_skills") or ()]; skills = load_selected_skills(app_data_dir() / "skills", Path(self.workspace_path), selected); worker = AgentWorker.for_run(GatewayClient(connection), api_key=api_key, model=model, messages=prepared.messages, mode=self.current_mode, workspace_id=identity, approval_policy="auto" if self._auto_mode else "prompt", session_id=conversation.id, context_limit_tokens=prepared.context_limit, skills=skills, workspace_config=config, provider_preferences=privacy); worker.eventReceived.connect(self.onAgentEvent)
        self.worker = worker; worker.runStarted.connect(self.onRunStarted); worker.chunk.connect(self.onStreamChunk); worker.complete.connect(self.onStreamComplete); worker.failed.connect(self.onStreamError); worker.finished.connect(self.onWorkerFinished); suffix = f" · compacted {prepared.removed_messages}" if prepared.removed_messages else ""; self.status_text = f"≈{prepared.estimated_tokens:,} input tokens{suffix}"; self.stateChanged.emit(); self._refresh_transcript(); worker.start()
