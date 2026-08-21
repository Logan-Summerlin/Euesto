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
    """QML controller; security-sensitive authority remains in Python backends."""

    conversationsChanged = Signal()
    transcriptChanged = Signal()
    modelsChanged = Signal()
    stateChanged = Signal()
    settingsChanged = Signal()
    permissionsChanged = Signal()
    commandsChanged = Signal()
    skillsChanged = Signal()
    presetsChanged = Signal()
    focusComposerRequested = Signal()
    infoRequested = Signal(str, str)
    errorRequested = Signal(str, str)
    runtimeSetupStarted = Signal()
    runtimeSetupFinished = Signal(bool)
    confirmRequested = Signal(str, str, str)
    approvalRequested = Signal("QVariantMap")
    fileExported = Signal(str)
    fileImported = Signal(str)

    def __init__(self, storage: Storage | None = None):
        super().__init__()
        self.storage = storage or Storage(database_path())
        self.catalog = ModelCatalog(self.storage)
        self.conversation_controller = ConversationController(self.storage)
        self.generation = GenerationController(self.storage)
        self.worker: ChatWorker | AgentWorker | None = None
        self.catalog_worker: CatalogWorker | None = None
        self.staging_discard_worker: StagingDiscardWorker | None = None
        self.staging_inspect_worker: StagingInspectWorker | None = None
        self.publication_worker: PublicationWorker | None = None
        self.health_worker: GatewayHealthWorker | None = None
        self.runtime_manager = RuntimeManager(app_data_dir(), self)
        self.runtime_automatic = bool(getattr(sys, "frozen", False))
        self.current_conversation_id: str | None = None
        self.current_mode = "chat"
        self._auto_mode = False
        self.workspace_path = self.storage.get_setting("workspace_path", "") or ""
        self.show_archived = False
        self.search_query = ""
        self.status_text = "Ready"
        self.gateway_text = "Gateway: checking"
        self.gateway_detail = ""
        self.gateway_health_state = HealthState.STARTING
        self.last_gateway_status: object | None = None
        self.runtime_state = "starting" if self.runtime_automatic else "manual"
        self.runtime_detail = (
            "Preparing the local runtime…"
            if self.runtime_automatic
            else "The developer runtime is managed by scripts."
        )
        self.runtime_target_identity: str | None = None
        self.live_events: list[dict[str, object]] = []
        self._conversations: list[dict[str, Any]] = []
        self._transcript: list[dict[str, Any]] = []
        self._transcript_model = TranscriptListModel(self)
        self._transcript_html_cache: dict[tuple[str, str], str] = {}
        self._models: list[dict[str, Any]] = []
        self._permissions: list[dict[str, Any]] = []
        self._commands: list[dict[str, Any]] = []
        self._skills: list[dict[str, Any]] = []
        self._presets: list[dict[str, Any]] = []
        self._pending_confirmation: dict[str, tuple[str, Any]] = {}
        self._pending_approvals: dict[str, dict[str, Any]] = {}
        self._gateway_token = get_gateway_session_token() or get_gateway_token() or ""
        self._window: QWindow | None = None
        self.tray: TrayService | None = None
        self.hotkey: GlobalQuickChatHotkey | None = None
        self._closing = False
        self._catalog_autorefresh_attempted = False
        self._gateway_recheck_requested = False
        self._transcript_refresh_timer = QTimer(self)
        self._transcript_refresh_timer.setSingleShot(True)
        self._transcript_refresh_timer.setInterval(50)
        self._transcript_refresh_timer.timeout.connect(self._refresh_transcript)
        self._reload_models()
        self._reload_commands()
        self._reload_presets()
        self._load_conversations()
        self.health_timer = QTimer(self)
        self.health_timer.setInterval(10_000)
        self.health_timer.timeout.connect(self.checkGateway)
        self.health_timer.start()
        self.runtime_manager.progressChanged.connect(self._runtime_progress)
        self.runtime_manager.ready.connect(self._runtime_ready)
        self.runtime_manager.failed.connect(self._runtime_failed)
        self.runtime_manager.setupStarted.connect(self.runtimeSetupStarted)
        self.runtime_manager.setupFinished.connect(self.runtimeSetupFinished)
        QTimer.singleShot(
            0,
            self._start_runtime_for_saved_workspace if self.runtime_automatic else self.checkGateway,
        )

    @Property("QVariantList", notify=conversationsChanged)
    def conversations(self) -> list[dict[str, Any]]:
        return self._conversations

    @Property("QVariantList", notify=transcriptChanged)
    def transcript(self) -> list[dict[str, Any]]:
        return self._transcript

    @Property(QObject, constant=True)
    def transcriptModel(self) -> QObject:
        return self._transcript_model

    @Property("QVariantList", notify=modelsChanged)
    def models(self) -> list[dict[str, Any]]:
        return self._models

    @Property("QVariantList", notify=permissionsChanged)
    def permissions(self) -> list[dict[str, Any]]:
        return self._permissions

    @Property("QVariantList", notify=commandsChanged)
    def commands(self) -> list[dict[str, Any]]:
        return self._commands

    @Property("QVariantList", notify=skillsChanged)
    def skills(self) -> list[dict[str, Any]]:
        return self._skills

    @Property("QVariantList", notify=presetsChanged)
    def presets(self) -> list[dict[str, Any]]:
        return self._presets

    @Property(str, notify=stateChanged)
    def currentConversationId(self) -> str:
        return self.current_conversation_id or ""

    @Property(str, notify=stateChanged)
    def currentTitle(self) -> str:
        conversation = self.current_conversation()
        return conversation.title if conversation else ""

    @Property(str, notify=stateChanged)
    def currentModel(self) -> str:
        conversation = self.current_conversation()
        return conversation.model if conversation else DEFAULT_MODELS[0].id

    @Property(str, notify=stateChanged)
    def currentMode(self) -> str:
        return self.current_mode

    @Property(bool, notify=stateChanged)
    def autoModeEnabled(self) -> bool:
        return self._auto_mode

    @Property(bool, notify=stateChanged)
    def autoModeAvailable(self) -> bool:
        status = self.last_gateway_status
        capabilities = getattr(status, "capabilities", ()) if status else ()
        return any(
            isinstance(item, dict) and item.get("name") == "agent_auto"
            for item in capabilities
        )

    @Property(str, notify=stateChanged)
    def workspacePath(self) -> str:
        return self.workspace_path

    @Property(str, notify=stateChanged)
    def statusText(self) -> str:
        return self.status_text

    @Property(str, notify=stateChanged)
    def gatewayText(self) -> str:
        return self.gateway_text

    @Property(str, notify=stateChanged)
    def gatewayDetail(self) -> str:
        return self.gateway_detail

    @Property(str, notify=stateChanged)
    def runtimeState(self) -> str:
        return self.runtime_state

    @Property(str, notify=stateChanged)
    def runtimeDetail(self) -> str:
        return self.runtime_detail

    @Property(bool, notify=stateChanged)
    def runtimeBusy(self) -> bool:
        return self.runtime_state in {
            "starting",
            "stopping",
            "pulling",
            "building",
            "checking",
        }

    @Property(bool, notify=stateChanged)
    def workspaceReady(self) -> bool:
        return self._workspace_status_ready(self.last_gateway_status)

    @Property(bool, notify=stateChanged)
    def generating(self) -> bool:
        return self.worker is not None

    @Property(bool, notify=stateChanged)
    def stagingBusy(self) -> bool:
        return any(
            (
                self.staging_discard_worker,
                self.staging_inspect_worker,
                self.publication_worker,
            )
        )

    @Property(bool, notify=stateChanged)
    def archivedView(self) -> bool:
        return self.show_archived

    @Property(str, notify=settingsChanged)
    def theme(self) -> str:
        return self.storage.get_setting("theme", "dark") or "dark"

    @Property(str, notify=settingsChanged)
    def systemPrompt(self) -> str:
        conversation = self.current_conversation()
        return conversation.system_prompt if conversation else ""

    @Property(str, notify=settingsChanged)
    def reasoningEffort(self) -> str:
        value = GenerationController.request_options(self.storage, self.currentModel)
        return value.reasoning_effort or "default"

    @Property("QVariantMap", notify=settingsChanged)
    def modelOptions(self) -> dict[str, Any]:
        return asdict(GenerationController.request_options(self.storage, self.currentModel))

    @Property("QVariantMap", notify=settingsChanged)
    def serverTools(self) -> dict[str, bool]:
        return {
            key: self.storage.get_setting(f"server_tool:{key}", "0") == "1"
            for key in ("web_search", "web_fetch", "datetime")
        }

    @Property(str, notify=settingsChanged)
    def investigationModel(self) -> str:
        return self.storage.get_setting("investigation_model_id", "") or ""

    @Slot(str)
    def saveInvestigationModel(self, model_id: str) -> None:
        self.storage.set_setting("investigation_model_id", str(model_id or "").strip())
        self.settingsChanged.emit()

    @Property("QVariantMap", notify=settingsChanged)
    def gatewaySettings(self) -> dict[str, Any]:
        return {
            "url": self.storage.get_setting("gateway_url", DEFAULT_GATEWAY_URL)
            or DEFAULT_GATEWAY_URL,
            "hasToken": bool(self._gateway_token),
            "hasApiKey": bool(get_api_key()),
        }

    @Property("QVariantMap", notify=settingsChanged)
    def usage(self) -> dict[str, Any]:
        return dict(self.storage.usage_summary(self.current_conversation_id))

    @Slot(QWindow)
    def attachWindow(self, window: QWindow) -> None:
        if self._window is window:
            return
        self._window = window
        self.tray = TrayService(
            window,
            show_quick_chat=self.showQuickChat,
            toggle_visibility=self.toggleWindowVisibility,
            close=self.shutdown,
        )
        self.hotkey = GlobalQuickChatHotkey(window, self.showQuickChat)
        application = QGuiApplication.instance()
        if application:
            application.installNativeEventFilter(self.hotkey)
        self.hotkey.register()

    @Slot()
    def newConversation(self) -> None:
        if self.worker:
            return
        conversation = self.conversation_controller.create_new(
            self.current_conversation(), self.storage.recent_model_ids(1)
        )
        self.current_conversation_id = conversation.id
        self.show_archived = False
        self.search_query = ""
        self._load_conversations(conversation.id)
        self.focusComposerRequested.emit()

    @Slot(str)
    def selectConversation(self, conversation_id: str) -> None:
        if self.worker or not self.storage.get_conversation(conversation_id):
            return
        self.current_conversation_id = conversation_id
        self._refresh_transcript(force_reset=True)
        self.stateChanged.emit()
        self.settingsChanged.emit()

    @Slot(str)
    def setConversationSearch(self, query: str) -> None:
        self.search_query = query.strip()
        self._load_conversations()

    @Slot()
    def toggleArchived(self) -> None:
        if self.worker:
            return
        self.show_archived = not self.show_archived
        self.current_conversation_id = None
        self._load_conversations()
        self._set_status("Archived conversations" if self.show_archived else "Ready")

    @Slot(str)
    def renameConversation(self, title: str) -> None:
        conversation = self.current_conversation()
        title = title.strip()
        if conversation and title and not self.worker:
            self.storage.update_conversation(conversation.id, title=title[:200])
            self._load_conversations(conversation.id)

    @Slot()
    def togglePin(self) -> None:
        conversation = self.current_conversation()
        if conversation and not self.worker:
            self.storage.pin_conversation(conversation.id, not bool(conversation.pinned_at))
            self._load_conversations(conversation.id)

    @Slot()
    def toggleArchive(self) -> None:
        conversation = self.current_conversation()
        if conversation and not self.worker:
            self.storage.archive_conversation(
                conversation.id, not bool(conversation.archived_at)
            )
            self.current_conversation_id = None
            self._load_conversations()

    @Slot()
    def requestDeleteConversation(self) -> None:
        conversation = self.current_conversation()
        if not conversation or self.worker:
            return
        token = f"delete:{conversation.id}"
        self._pending_confirmation[token] = ("delete", conversation.id)
        self.confirmRequested.emit(
            token,
            "Delete conversation?",
            f"Delete “{conversation.title}”? This cannot be undone.",
        )

    @Slot(str, bool)
    def resolveConfirmation(self, token: str, accepted: bool) -> None:
        pending = self._pending_confirmation.pop(token, None)
        if not pending:
            return
        action, value = pending
        if not accepted:
            if action == "enable-auto":
                self.stateChanged.emit()
            return
        if action == "delete":
            self.storage.delete_conversation(str(value))
            self.current_conversation_id = None
            self._load_conversations()
        elif action == "custom-command":
            self._start_user_turn(str(value))
        elif action == "publish":
            self._start_publication(value, auto=False)
        elif action == "discard-staging":
            self._discard_staging()
        elif action == "enable-auto":
            self._auto_mode = True
            self._set_status("Auto enabled for this Agent session")
            self.stateChanged.emit()

    def _start_publication(self, value: object, *, auto: bool) -> None:
        try:
            manifest = (
                value
                if isinstance(value, PublishManifest)
                else PublishManifest.from_dict(value)
            )
            if not self.workspace_path:
                raise BrokerError("Select the workspace that produced this manifest first")
            if self.publication_worker:
                raise BrokerError("Another publication is already running")
            connection = self._gateway_connection() if auto else None
            if auto and connection is None:
                raise BrokerError("Auto publication requires the local gateway")
            self.publication_worker = PublicationWorker(
                manifest,
                Path(self.workspace_path),
                app_data_dir() / "recovery",
                reseed_client=(GatewayClient(connection) if connection else None),
            )
            self.publication_worker.complete.connect(self._on_publication_complete)
            self.publication_worker.failed.connect(self._on_publication_failed)
            self.publication_worker.finished.connect(self._on_publication_finished)
            self._set_status("Publishing validated staged changes…")
            self.stateChanged.emit()
            self.publication_worker.start()
        except (BrokerError, OSError, TypeError, ValueError) as exc:
            if auto:
                self._auto_mode = False
                self.stateChanged.emit()
            self.errorRequested.emit("Publication blocked", str(exc))

    @Slot(dict)
    def _on_publication_complete(self, result: dict[str, Any]) -> None:
        completed = result.get("completed_paths") or ()
        checkpoint = str(result.get("checkpoint_id") or "")
        self._set_status(
            f"Published {len(completed)} file(s); checkpoint {checkpoint[:8]}"
        )
        if result.get("reseed_error"):
            self._auto_mode = False
            self.errorRequested.emit(
                "Published, but Auto stopped",
                "Host files were updated, but staging could not be reseeded: "
                + str(result["reseed_error"]),
            )
        self.stateChanged.emit()

    @Slot(str)
    def _on_publication_failed(self, message: str) -> None:
        self._auto_mode = False
        self.stateChanged.emit()
        self.errorRequested.emit("Publication blocked", message)

    @Slot()
    def _on_publication_finished(self) -> None:
        if self.publication_worker:
            self.publication_worker.deleteLater()
        self.publication_worker = None
        self.stateChanged.emit()
        if self.worker is None:
            self._continue_queued_input()

    @Slot()
    def requestDiscardStaging(self) -> None:
        if self.worker or self.staging_discard_worker or not self.workspace_path:
            return
        token = f"discard-staging:{self.workspace_path}"
        self._pending_confirmation[token] = ("discard-staging", None)
        self.confirmRequested.emit(
            token,
            "Discard staged changes?",
            "This deletes the private staged copy and reseeds it from the selected source workspace. "
            "It does not change host files, but staged edits will be lost.",
        )

    def _discard_staging(self) -> None:
        connection = self._gateway_connection()
        if not connection or not self.workspace_path:
            self.errorRequested.emit("Workspace runtime required", "Select a ready workspace first.")
            return
        identity = workspace_id(Path(self.workspace_path))
        self.staging_discard_worker = StagingDiscardWorker(GatewayClient(connection), identity)
        self.staging_discard_worker.complete.connect(self._on_staging_discarded)
        self.staging_discard_worker.failed.connect(self._on_staging_discard_failed)
        self.staging_discard_worker.finished.connect(self._on_staging_discard_finished)
        self._set_status("Discarding staged changes and reseeding workspace…")
        self.stateChanged.emit()
        self.staging_discard_worker.start()

    @Slot(dict)
    def _on_staging_discarded(self, result: dict[str, Any]) -> None:
        self._set_status(
            "Staging reseeded"
            + (f" · {int(result.get('file_count') or 0):,} files" if result else "")
        )

    @Slot(str)
    def _on_staging_discard_failed(self, message: str) -> None:
        self.errorRequested.emit("Could not discard staging", message)

    @Slot()
    def _on_staging_discard_finished(self) -> None:
        if self.staging_discard_worker:
            self.staging_discard_worker.deleteLater()
        self.staging_discard_worker = None
        self.stateChanged.emit()

    @Slot()
    def reviewStaging(self) -> None:
        if self.worker or self.stagingBusy or not self.workspace_path:
            return
        connection = self._gateway_connection()
        if not connection:
            self.errorRequested.emit("Workspace runtime required", "Select a ready workspace first.")
            return
        identity = workspace_id(Path(self.workspace_path))
        self.staging_inspect_worker = StagingInspectWorker(GatewayClient(connection), identity)
        self.staging_inspect_worker.complete.connect(self._on_staging_inspected)
        self.staging_inspect_worker.failed.connect(self._on_staging_inspect_failed)
        self.staging_inspect_worker.finished.connect(self._on_staging_inspect_finished)
        self._set_status("Reviewing staged changes…")
        self.stateChanged.emit()
        self.staging_inspect_worker.start()

    @Slot(dict)
    def _on_staging_inspected(self, result: dict[str, Any]) -> None:
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        changes = data.get("changes") if isinstance(data.get("changes"), list) else []
        lines = [f"Staged changes: {len(changes)} shown"]
        for item in changes[:500]:
            if isinstance(item, dict):
                lines.append(
                    f"{item.get('operation', '?')}: {item.get('path', '?')}"
                )
        if data.get("truncated"):
            lines.append("More changes are available through the bounded cursor.")
        self.infoRequested.emit("Staged workspace review", "\n".join(lines))
        self._set_status(f"Reviewed {len(changes)} staged change(s)")

    @Slot(str)
    def _on_staging_inspect_failed(self, message: str) -> None:
        self.errorRequested.emit("Could not review staging", message)

    @Slot()
    def _on_staging_inspect_finished(self) -> None:
        if self.staging_inspect_worker:
            self.staging_inspect_worker.deleteLater()
        self.staging_inspect_worker = None
        self.stateChanged.emit()

    @Slot()
    def forkConversation(self) -> None:
        conversation = self.current_conversation()
        if conversation and not self.worker:
            fork = self.conversation_controller.fork(conversation.id)
            self.current_conversation_id = fork.id
            self._load_conversations(fork.id)
            self._set_status("Conversation forked")

    @Slot(str)
    def selectModel(self, model_id: str) -> None:
        conversation = self.current_conversation()
        model_id = self.storage.resolve_model_id(model_id.strip())
        if not conversation or not model_id or self.worker:
            return
        self.storage.update_conversation(conversation.id, model=model_id)
        self.storage.record_recent_model(model_id)
        self._reload_models()
        self.stateChanged.emit()
        self.settingsChanged.emit()

    @Slot(str)
    def setReasoningEffort(self, effort: str) -> None:
        options = GenerationController.request_options(self.storage, self.currentModel)
        options.reasoning_effort = None if effort in {"", "default"} else effort
        self._save_model_options(options)

    @Slot("QVariantMap")
    def saveModelOptions(self, values: dict[str, Any]) -> None:
        options = GenerationController.request_options(self.storage, self.currentModel)
        options.max_tokens = optional_int(values.get("max_tokens"))
        options.temperature = optional_float(values.get("temperature"))
        options.top_p = optional_float(values.get("top_p"))
        stop = values.get("stop")
        options.stop = [str(item) for item in stop] if isinstance(stop, list) else []
        effort = str(values.get("reasoning_effort") or "")
        options.reasoning_effort = (
            effort if effort in {"minimal", "low", "medium", "high"} else None
        )
        options.data_collection = (
            "allow" if values.get("data_collection") == "allow" else "deny"
        )
        options.zero_data_retention = bool(values.get("zero_data_retention", False))
        self._save_model_options(options)

    def _save_model_options(self, options: RequestOptions) -> None:
        self.storage.set_setting(
            f"model_options:{self.currentModel}", json.dumps(asdict(options))
        )
        self.settingsChanged.emit()
        self._set_status("Model and privacy controls saved")

    @Slot(str, bool)
    def setServerTool(self, name: str, enabled: bool) -> None:
        if name in {"web_search", "web_fetch", "datetime"}:
            self.storage.set_setting(f"server_tool:{name}", "1" if enabled else "0")
            self.settingsChanged.emit()

    @Slot(str)
    def setTheme(self, theme: str) -> None:
        self.storage.set_setting("theme", "light" if theme == "light" else "dark")
        self.settingsChanged.emit()

    @Slot(str)
    def saveSystemPrompt(self, prompt: str) -> None:
        conversation = self.current_conversation()
        if conversation and not self.worker:
            self.storage.update_conversation(conversation.id, system_prompt=prompt[:128_000])
            self.settingsChanged.emit()
            self._set_status("System prompt saved")

    @Slot(str, str)
    def saveGateway(self, url: str, token: str) -> None:
        try:
            new_token = token.strip()
            connection = GatewayConnection(
                url.strip() or DEFAULT_GATEWAY_URL,
                new_token or self._gateway_token,
            )
            self.storage.set_setting("gateway_url", connection.base_url)
            if new_token:
                save_gateway_token(new_token)
                self._gateway_token = new_token
        except Exception as exc:
            self.errorRequested.emit("Invalid gateway settings", str(exc))
            return
        self._catalog_autorefresh_attempted = False
        self.settingsChanged.emit()
        self._set_status("Gateway settings saved; checking connection…")
        if self.health_worker:
            self._gateway_recheck_requested = True
        else:
            self.checkGateway()

    @Slot(str)
    def saveApiKey(self, value: str) -> None:
        try:
            save_api_key(value)
        except Exception as exc:
            self.errorRequested.emit("Could not save API key", str(exc))
            return
        self.settingsChanged.emit()
        self._set_status("API key stored securely")

    @Slot(str)
    def selectWorkspace(self, value: str) -> None:
        if self.worker or self.stagingBusy:
            return
        local = QUrl(value).toLocalFile() if value.startswith("file:") else value
        if not local:
            return
        try:
            path = canonical_workspace(Path(local))
        except BrokerError as exc:
            self.errorRequested.emit("Unsafe workspace", str(exc))
            return
        self.workspace_path = str(path)
        self._auto_mode = False
        self.storage.set_setting("workspace_path", self.workspace_path)
        if self.current_mode != "chat":
            self.current_mode = "chat"
        self.runtime_target_identity = workspace_id(path)
        self.runtime_state = "starting" if self.runtime_automatic else "manual"
        self.runtime_detail = (
            "Preparing the selected workspace…"
            if self.runtime_automatic
            else "Workspace selected. The developer runtime is managed by scripts."
        )
        self.last_gateway_status = None
        self._catalog_autorefresh_attempted = False
        self._reload_skills()
        self.stateChanged.emit()
        self._set_status(
            "Preparing workspace…"
            if self.runtime_automatic
            else "Workspace selected; checking developer runtime…"
        )
        if self.runtime_automatic:
            try:
                self.runtime_manager.ensure(path)
            except (OSError, RuntimeError, ValueError) as exc:
                self._runtime_failed(str(exc))
        elif self.health_worker:
            self._gateway_recheck_requested = True
        else:
            self.checkGateway()

    @Slot()
    def retryRuntime(self) -> None:
        if not self.runtime_automatic:
            self.errorRequested.emit(
                "Developer runtime is managed manually",
                "Run .\\scripts\\dev-up.ps1 for the selected workspace, then check the gateway again.",
            )
            return
        workspace = Path(self.workspace_path) if self.workspace_path else None
        try:
            if workspace is not None:
                workspace = canonical_workspace(workspace)
            self.runtime_target_identity = workspace_id(workspace) if workspace else None
            self.runtime_manager.ensure(workspace)
        except (BrokerError, OSError, RuntimeError, ValueError) as exc:
            self._runtime_failed(str(exc))

    @Slot(str)
    def selectMode(self, mode: str) -> None:
        if mode not in {"chat", "plan", "agent"} or self.worker or self.stagingBusy:
            return
        if mode == "chat":
            self.current_mode = mode
            self._auto_mode = False
            self.stateChanged.emit()
            return
        if not self.workspace_path:
            self.errorRequested.emit(
                "Workspace required", "Select one project workspace first."
            )
            return
        if not self.workspaceReady:
            self.errorRequested.emit(
                "Workspace is still preparing",
                self.runtime_detail
                or "Wait for the isolated executor to become ready, then try again.",
            )
            return
        status = self.last_gateway_status
        identity = workspace_id(Path(self.workspace_path))
        supported = tuple(getattr(status, "supported_modes", ())) if status else ()
        active = getattr(status, "active_workspace", None) if status else None
        if mode not in supported or active != identity:
            self.errorRequested.emit(
                "Workspace runtime unavailable",
                "The isolated executor is not ready for the selected workspace. Retry the local runtime setup.",
            )
            return
        self.current_mode = mode
        if mode != "agent":
            self._auto_mode = False
        self.stateChanged.emit()

    @Slot(bool)
    def requestAutoMode(self, enabled: bool) -> None:
        if not enabled:
            self._auto_mode = False
            self._set_status("Auto disabled")
            self.stateChanged.emit()
            return
        if (
            self.current_mode != "agent"
            or not self.autoModeAvailable
            or not self.workspaceReady
            or self.worker
            or self.stagingBusy
        ):
            self.errorRequested.emit(
                "Auto unavailable",
                "Select Agent mode with a ready, idle workspace and compatible gateway.",
            )
            return
        token = f"enable-auto:{self.workspace_path}"
        self._pending_confirmation[token] = ("enable-auto", None)
        self.confirmRequested.emit(
            token,
            "Enable Auto for this Agent session?",
            "Valid tools and successful staged publication will run without further approval. "
            "Workspace, command, network, resource, hash, and broker safety limits still apply. "
            "Auto stops on failure, resume, workspace change, or app restart.",
        )

    @Slot(str, bool)
    def sendMessage(self, text: str, steer: bool = False) -> None:
        text = text.strip()
        if not text:
            return
        if self.publication_worker:
            self._set_status("Wait for staged publication to finish…")
            return
        resolved = self._handle_prompt_command(text)
        if resolved is None:
            return
        if self.worker:
            self.generation.enqueue(resolved, steered=steer, front=steer)
            self._set_status(f"Queued {self.generation.pending_count} message(s)")
            return
        self._start_user_turn(resolved)

    def _handle_prompt_command(self, text: str) -> str | None:
        if not text.startswith("/"):
            return text
        name, _, arguments = text[1:].partition(" ")
        name = name.casefold()
        custom = next(
            (item for item in self.storage.list_prompt_commands() if item["name"] == name),
            None,
        )
        if custom:
            expanded = expand_prompt_command(custom["template"], arguments)
            token = f"command:{name}:{len(self._pending_confirmation)}"
            self._pending_confirmation[token] = ("custom-command", expanded)
            self.confirmRequested.emit(token, f"Run /{name}?", expanded[:8_000])
            return None
        actions = {
            "new": self.newConversation,
            "fork": self.forkConversation,
            "compact": self.compactContext,
            "context": self.inspectContext,
            "cost": self.showUsage,
            "status": self.showGatewayStatus,
            "stop": self.stopGeneration,
            "pause": self.pauseAgent,
        }
        if name == "mode":
            self.selectMode(arguments.strip().casefold())
        elif name in actions:
            actions[name]()
        else:
            self._set_status(f"Unknown command: /{name}")
        return None

    def _start_user_turn(self, text: str) -> None:
        conversation = self.current_conversation()
        if conversation is None:
            self.newConversation()
            conversation = self.current_conversation()
        api_key = get_api_key()
        if not api_key:
            self.errorRequested.emit(
                "OpenRouter API key required",
                "Open Settings → Connection and store an API key.",
            )
            return
        connection = self._gateway_connection()
        if connection is None:
            self.errorRequested.emit(
                "Gateway connection required",
                "Open Settings → Connection and store the local gateway token.",
            )
            return
        assert conversation is not None
        had_user = any(
            item.role == "user" for item in self.storage.list_all_messages(conversation.id)
        )
        message = self.conversation_controller.add_user_turn(
            conversation.id, text, self.currentModel
        )
        if not had_user:
            self._load_conversations(conversation.id)
        self._refresh_transcript()
        self._start_generation(
            api_key,
            connection,
            self.currentModel,
            message.id,
            self.storage.list_messages(conversation.id),
        )

    def _start_generation(
        self,
        api_key: str,
        connection: GatewayConnection,
        model: str,
        parent_message_id: int | None,
        context_messages: list[Any],
    ) -> None:
        conversation = self.current_conversation()
        if conversation is None:
            return
        prepared = self.generation.prepare(
            conversation, context_messages, model, self.catalog.models()
        )
        self.generation.begin(conversation.id, parent_message_id, model, self.current_mode)
        self.live_events = []
        options = GenerationController.request_options(self.storage, model)
        privacy = {
            "data_collection": options.data_collection,
            "zdr": options.zero_data_retention,
        }
        if self.current_mode == "chat":
            worker: ChatWorker | AgentWorker = ChatWorker(
                GatewayClient(connection),
                api_key,
                model,
                prepared.messages,
                options,
                ServerToolOptions(**self.serverTools),
                prepared.supported_parameters,
            )
        else:
            if not self.workspace_path:
                return
            identity = workspace_id(Path(self.workspace_path))
            config = self.storage.workspace_config(identity)
            selected = [str(item) for item in config.get("active_skills") or ()]
            skills = load_selected_skills(
                app_data_dir() / "skills", Path(self.workspace_path), selected
            )
            worker = AgentWorker.for_run(
                GatewayClient(connection),
                api_key=api_key,
                model=model,
                messages=prepared.messages,
                mode=self.current_mode,
                workspace_id=identity,
                approval_policy="auto" if self._auto_mode else "prompt",
                session_id=conversation.id,
                context_limit_tokens=prepared.context_limit,
                skills=skills,
                workspace_config=config,
                provider_preferences=privacy,
                investigation_model_id=self.investigationModel or None,
            )
            worker.eventReceived.connect(self.onAgentEvent)
        self.worker = worker
        worker.runStarted.connect(self.onRunStarted)
        worker.chunk.connect(self.onStreamChunk)
        worker.complete.connect(self.onStreamComplete)
        worker.failed.connect(self.onStreamError)
        worker.finished.connect(self.onWorkerFinished)
        suffix = (
            f" · compacted {prepared.removed_messages}" if prepared.removed_messages else ""
        )
        self.status_text = f"≈{prepared.estimated_tokens:,} input tokens{suffix}"
        self.stateChanged.emit()
        self._refresh_transcript()
        worker.start()

    @Slot(str)
    def onRunStarted(self, run_id: str) -> None:
        self.generation.start_run(run_id)

    @Slot(object)
    def onAgentEvent(self, event: object) -> None:
        if not hasattr(event, "type"):
            return
        event_type = str(event.type)
        compact: dict[str, object] | None = None
        try:
            if event_type not in {"model.delta", "tool.output", "tool.completed"}:
                self.generation.save_event(event)
            compact = compact_activity_event(
                {
                    "run_id": event.run_id,
                    "event_id": event.event_id,
                    "type": event.type,
                    "payload": event.payload,
                }
            )
            if compact is not None:
                self.live_events.append(compact)
        except (TypeError, ValueError):
            pass
        if event_type == "tool.started":
            self._set_status(
                f"Running {event.payload.get('tool', 'tool')} in the isolated executor…"
            )
        elif event_type == "approval.required":
            approval_id = str(event.payload.get("approval_id") or "")
            kind = str(event.payload.get("kind") or "tool")
            detail = (
                event.payload.get("manifest")
                if kind == "publish"
                else event.payload.get("arguments")
            )
            summary, full_detail = approval_display(
                kind, str(event.payload.get("tool") or "tool"), detail
            )
            key = f"{event.run_id}:{approval_id}"
            self._pending_approvals[key] = {
                "runId": event.run_id,
                "approvalId": approval_id,
                "kind": kind,
            }
            self.approvalRequested.emit(
                {
                    "key": key,
                    "title": (
                        "Publish staged changes?"
                        if kind == "publish"
                        else "Approve isolated tool?"
                    ),
                    "summary": summary,
                    "details": full_detail,
                    "allowRule": kind == "tool",
                }
            )
        elif event_type == "checkpoint.created" and event.payload.get("publish_manifest"):
            try:
                manifest = PublishManifest.from_dict(event.payload["publish_manifest"])
                if event.payload.get("auto_publish"):
                    if not (
                        self._auto_mode
                        and isinstance(self.worker, AgentWorker)
                        and self.worker.auto_approve
                    ):
                        raise ValueError("Auto publication is not authorized by this desktop session")
                    self._start_publication(manifest, auto=True)
                else:
                    token = f"publish:{event.run_id}:{event.event_id}"
                    self._pending_confirmation[token] = ("publish", manifest)
                    self.confirmRequested.emit(
                        token,
                        "Publish staged changes to the host?",
                        f"The approved staging checkpoint contains {len(manifest.operations)} file operation(s). "
                        "Confirm to write the exact, hash-checked manifest to the selected workspace.",
                    )
            except (TypeError, ValueError) as exc:
                self._auto_mode = False
                self.stateChanged.emit()
                self.errorRequested.emit("Publication blocked", str(exc))
        elif event_type == "publication.failed":
            self._auto_mode = False
            self._set_status("Agent completed; host publication is unavailable")
        if compact is not None:
            self._schedule_transcript_refresh()

    @Slot(str, str)
    def resolveApproval(self, key: str, decision: str) -> None:
        pending = self._pending_approvals.pop(key, None)
        if not pending or not isinstance(self.worker, AgentWorker):
            return
        if decision not in {"deny", "allow_once", "allow_run", "allow_rule"}:
            decision = "deny"
        try:
            self.worker.client.resolve_approval(
                str(pending["runId"]), str(pending["approvalId"]), decision
            )
        except GatewayError as exc:
            self.errorRequested.emit("Approval failed", str(exc))

    @Slot(str)
    def onStreamChunk(self, text: str) -> None:
        self.generation.append(text)

    @Slot(dict, bool)
    def onStreamComplete(self, usage: dict[str, Any], cancelled: bool) -> None:
        cancelled = cancelled or self.generation.state.cancel_requested
        if cancelled and isinstance(self.worker, AgentWorker) and self.worker.auto_approve:
            self._auto_mode = False
        if usage.get("run_id") and self.generation.state.run_id is None:
            self.onRunStarted(str(usage["run_id"]))
        saved = self.generation.save_assistant(
            usage, status="cancelled" if cancelled else "completed"
        )
        if saved is None:
            self.generation.finish_without_message("cancelled" if cancelled else "completed")
        self.generation.state.clear_stream()
        self.live_events = []
        self.status_text = GenerationController.format_usage(usage, cancelled)
        self._refresh_transcript()
        self.stateChanged.emit()
        self.settingsChanged.emit()

    @Slot(str)
    def onStreamError(self, message: str) -> None:
        if isinstance(self.worker, AgentWorker) and self.worker.auto_approve:
            self._auto_mode = False
        usage = (
            self.worker.last_usage
            if isinstance(self.worker, AgentWorker)
            else {}
        )
        saved = self.generation.save_assistant(
            usage,
            finish_reason="error",
            status="failed",
        )
        if saved is None:
            self.generation.finish_without_message("failed", message)
        self.generation.state.clear_stream()
        self.live_events = []
        self.status_text = "Request failed"
        self._refresh_transcript()
        self.stateChanged.emit()
        self.errorRequested.emit("Gateway request failed", message)

    @Slot()
    def onWorkerFinished(self) -> None:
        if self.worker:
            self.worker.deleteLater()
        self.worker = None
        self.stateChanged.emit()
        if self.publication_worker:
            return
        self._continue_queued_input()

    def _continue_queued_input(self) -> None:
        queued = self.generation.next_input()
        if queued:
            self.status_text = (
                "Applying steering…" if queued.steered else "Sending queued message…"
            )
            QTimer.singleShot(0, lambda value=queued.text: self._start_user_turn(value))

    @Slot()
    def stopGeneration(self) -> None:
        if self.worker:
            self.status_text = "Stopping…"
            self.generation.request_cancel()
            self.worker.stop()
            self.stateChanged.emit()

    @Slot()
    def pauseAgent(self) -> None:
        if not isinstance(self.worker, AgentWorker):
            self._set_status("No active agent to pause")
            return
        try:
            if self.worker.client.pause():
                self._set_status("Pausing at the next safe model boundary…")
        except GatewayError as exc:
            self.errorRequested.emit("Could not pause agent", str(exc))

    @Slot(str)
    def resumeRun(self, run_id: str) -> None:
        connection = self._gateway_connection()
        api_key = get_api_key()
        conversation = self.current_conversation()
        if self.worker or not connection or not api_key or not conversation or not run_id:
            return
        self._auto_mode = False
        self.generation.begin(
            conversation.id,
            conversation.active_leaf_id,
            conversation.model,
            "agent",
        )
        worker = AgentWorker.for_resume(GatewayClient(connection), run_id, api_key)
        worker.runStarted.connect(self.onRunStarted)
        worker.eventReceived.connect(self.onAgentEvent)
        worker.chunk.connect(self.onStreamChunk)
        worker.complete.connect(self.onStreamComplete)
        worker.failed.connect(self.onStreamError)
        worker.finished.connect(self.onWorkerFinished)
        self.worker = worker
        self.stateChanged.emit()
        worker.start()

    @Slot(int, str)
    def editMessage(self, message_id: int, value: str) -> None:
        original = self.storage.get_message(message_id)
        if self.worker or original is None or original.role != "user" or not value.strip():
            return
        api_key = get_api_key()
        connection = self._gateway_connection()
        if not api_key or not connection:
            self.errorRequested.emit(
                "Connection required",
                "Configure the gateway and API key first.",
            )
            return
        edited = self.storage.edit_user_message(message_id, value.strip())
        self._refresh_transcript(force_reset=True)
        self._start_generation(
            api_key,
            connection,
            self.currentModel,
            edited.id,
            self.storage.list_messages(original.conversation_id),
        )

    @Slot(int)
    def regenerateMessage(self, message_id: int) -> None:
        original = self.storage.get_message(message_id)
        if self.worker or original is None or original.role != "assistant":
            return
        api_key = get_api_key()
        connection = self._gateway_connection()
        if not api_key or not connection:
            self.errorRequested.emit(
                "Connection required",
                "Configure the gateway and API key first.",
            )
            return
        context = self.storage.list_branch_to(
            original.conversation_id, original.parent_message_id
        )
        self._start_generation(
            api_key,
            connection,
            self.currentModel,
            original.parent_message_id,
            context,
        )

    @Slot(int, int)
    def navigateBranch(self, message_id: int, direction: int) -> None:
        if not self.worker and self.conversation_controller.branch_target(
            message_id, direction
        ):
            self._refresh_transcript(force_reset=True)

    @Slot()
    def compactContext(self) -> None:
        conversation = self.current_conversation()
        if not conversation:
            return
        messages = self.storage.list_messages(conversation.id)
        raw: list[dict[str, Any]] = []
        if conversation.system_prompt:
            raw.append({"role": "system", "content": conversation.system_prompt})
        raw.extend(
            {"role": item.role, "content": item.content, "_message_id": item.id}
            for item in messages
        )
        limit = max(
            4_000,
            int(model_context_length(conversation.model, self.catalog.models()) * 0.4),
        )
        _context, inspection, covered = compact_messages(raw, limit)
        if not covered:
            self.infoRequested.emit(
                "Context",
                "The active branch is already below the compaction target.",
            )
            return
        self.storage.save_compaction(
            conversation.id,
            conversation.active_leaf_id,
            covered,
            inspection.summary,
            conversation.model,
        )
        self._set_status(f"Compacted {len(covered)} older message(s)")

    @Slot()
    def inspectContext(self) -> None:
        conversation = self.current_conversation()
        if not conversation:
            return
        messages = self.storage.list_messages(conversation.id)
        estimate = sum(estimate_tokens(item.content) for item in messages)
        limit = int(model_context_length(conversation.model, self.catalog.models()) * 0.8)
        compactions = self.storage.list_compactions(conversation.id)
        latest = compactions[-1]["summary"] if compactions else "No saved compaction."
        self.infoRequested.emit(
            "Context inspection",
            f"Active branch: {len(messages)} messages\n"
            f"Estimated text tokens: {estimate:,}\n"
            f"Submission limit: {limit:,}\n"
            f"Saved compactions: {len(compactions)}\n\n"
            f"{str(latest)[:6000]}",
        )

    @Slot()
    def showUsage(self) -> None:
        total = self.storage.usage_summary()
        current = self.storage.usage_summary(self.current_conversation_id)
        self.infoRequested.emit(
            "Usage",
            f"Current conversation\n{_usage_text(current)}\n\n"
            f"All conversations\n{_usage_text(total)}",
        )

    @Slot()
    def showGatewayStatus(self) -> None:
        status = self.last_gateway_status
        if status is None:
            self.infoRequested.emit(
                "Gateway status", self.gateway_detail or "Gateway is offline."
            )
            return
        self.infoRequested.emit(
            "Gateway status",
            f"Gateway {status.gateway_version} · protocol {status.protocol_version}\n"
            f"Modes: {', '.join(status.supported_modes)}\n"
            f"Capabilities: "
            f"{', '.join(str(item.get('name') or '') for item in status.capabilities)}\n"
            f"Resumable runs: {len(status.resumable_runs)}",
        )

    @Slot(result="QVariantList")
    def resumableRuns(self) -> list[str]:
        status = self.last_gateway_status
        return list(getattr(status, "resumable_runs", ())) if status else []

    @Slot(str)
    def importConversation(self, value: str) -> None:
        path = _local_path(value)
        if not path or self.worker:
            return
        try:
            conversation_id = import_from_file(self.storage, path)
        except (OSError, UnicodeError, ImportExportError) as exc:
            self.errorRequested.emit("Import failed", str(exc))
            return
        self.current_conversation_id = conversation_id
        self.show_archived = False
        self.search_query = ""
        self._load_conversations(conversation_id)
        self.fileImported.emit(path.name)

    @Slot(str, str)
    def exportConversation(self, value: str, format_name: str = "json") -> None:
        conversation = self.current_conversation()
        path = _local_path(value)
        if not conversation or not path or self.worker:
            return
        if not path.suffix:
            path = path.with_suffix(".md" if format_name == "markdown" else ".json")
        try:
            export_to_file(self.storage, conversation.id, path)
        except (OSError, ImportExportError) as exc:
            self.errorRequested.emit("Export failed", str(exc))
            return
        self.fileExported.emit(path.name)

    @Slot(str, str, str)
    def savePromptCommand(self, name: str, description: str, template: str) -> None:
        try:
            self.storage.save_prompt_command(name, description, template)
        except ValueError as exc:
            self.errorRequested.emit("Invalid command", str(exc))
            return
        self._reload_commands()

    @Slot(str)
    def deletePromptCommand(self, name: str) -> None:
        self.storage.delete_prompt_command(name)
        self._reload_commands()

    @Slot(str, str, str)
    def savePromptPreset(self, preset_id: str, name: str, content: str) -> None:
        try:
            self.storage.save_prompt_preset(name, content, preset_id or None)
        except ValueError as exc:
            self.errorRequested.emit("Invalid preset", str(exc))
            return
        self._reload_presets()

    @Slot(str)
    def applyPromptPreset(self, preset_id: str) -> None:
        conversation = self.current_conversation()
        preset = self.storage.get_prompt_preset(preset_id)
        if conversation and preset:
            self.storage.update_conversation(
                conversation.id,
                system_prompt=preset.content,
                prompt_preset_id=preset.id,
                prompt_preset_snapshot=preset.content,
            )
            self.settingsChanged.emit()

    @Slot(str)
    def deletePromptPreset(self, preset_id: str) -> None:
        self.storage.delete_prompt_preset(preset_id)
        self._reload_presets()

    @Slot()
    def refreshSkills(self) -> None:
        self._reload_skills()

    @Slot(str)
    def saveActiveSkills(self, csv_names: str) -> None:
        if not self.workspace_path:
            return
        identity = workspace_id(Path(self.workspace_path))
        config = self.storage.workspace_config(identity)
        requested = [item.strip().casefold() for item in csv_names.split(",") if item.strip()]
        known = {item["name"] for item in self._skills}
        unknown = sorted(set(requested) - known)
        if unknown:
            self.errorRequested.emit("Unknown skills", ", ".join(unknown))
            return
        config["active_skills"] = requested
        self.storage.save_workspace_config(identity, self.workspace_path, config)
        self._sync_workspace_config(identity, config)
        self._reload_skills()

    @Slot(str, str)
    def saveWorkspaceConfiguration(self, instructions: str, declarations: str) -> None:
        if not self.workspace_path:
            return
        try:
            custom_tools = json.loads(declarations or "[]")
            if not isinstance(custom_tools, list) or any(
                not isinstance(item, dict) for item in custom_tools
            ):
                raise ValueError("Custom tools must be a JSON array of objects")
        except (json.JSONDecodeError, ValueError) as exc:
            self.errorRequested.emit("Invalid custom capabilities", str(exc))
            return
        identity = workspace_id(Path(self.workspace_path))
        config = self.storage.workspace_config(identity)
        config.update(
            {
                "instructions": instructions[:32_000],
                "custom_tools": custom_tools,
                "context_policy": config.get("context_policy", "automatic"),
            }
        )
        self.storage.save_workspace_config(identity, self.workspace_path, config)
        self._sync_workspace_config(identity, config)
        self._set_status("Workspace configuration saved")

    @Slot(result="QVariantMap")
    def workspaceConfiguration(self) -> dict[str, Any]:
        if not self.workspace_path:
            return {"instructions": "", "custom_tools": []}
        return dict(self.storage.workspace_config(workspace_id(Path(self.workspace_path))))

    @Slot()
    def loadPermissionRules(self) -> None:
        if not self.workspace_path:
            self._permissions = []
            self.permissionsChanged.emit()
            return
        connection = self._gateway_connection()
        if not connection:
            return
        try:
            self._permissions = GatewayClient(connection).permission_rules(
                workspace_id(Path(self.workspace_path))
            )
        except GatewayError as exc:
            self.errorRequested.emit("Could not load permissions", str(exc))
            return
        self.permissionsChanged.emit()

    @Slot(str, bool)
    def setPermissionEnabled(self, rule_id: str, enabled: bool) -> None:
        connection = self._gateway_connection()
        if not connection:
            return
        try:
            GatewayClient(connection).set_permission_rule_enabled(rule_id, enabled)
            self.loadPermissionRules()
        except GatewayError as exc:
            self.errorRequested.emit("Could not update permission", str(exc))

    @Slot(str)
    def deletePermission(self, rule_id: str) -> None:
        connection = self._gateway_connection()
        if not connection:
            return
        try:
            GatewayClient(connection).delete_permission_rule(rule_id)
            self.loadPermissionRules()
        except GatewayError as exc:
            self.errorRequested.emit("Could not delete permission", str(exc))

    @Slot()
    def refreshCatalog(self) -> None:
        self._start_catalog_refresh(report_errors=True)

    def _start_catalog_refresh(self, *, report_errors: bool) -> None:
        if self.catalog_worker:
            return
        connection = self._gateway_connection()
        if not connection:
            if report_errors:
                self.errorRequested.emit(
                    "Gateway token not configured",
                    "Open Settings → Connection and store the local gateway token first.",
                )
            return
        worker = CatalogWorker(GatewayClient(connection))
        worker.complete.connect(self._catalog_complete)
        if report_errors:
            worker.failed.connect(
                lambda message: self.errorRequested.emit("Model refresh failed", message)
            )
        else:
            worker.failed.connect(self._background_catalog_failed)
        worker.finished.connect(self._catalog_finished)
        self.catalog_worker = worker
        worker.start()

    @Slot(str)
    def _background_catalog_failed(self, message: str) -> None:
        self._set_status(f"Model catalog refresh unavailable: {message}")

    @Slot(object)
    def _catalog_complete(self, result: object) -> None:
        if (
            isinstance(result, dict)
            and isinstance(result.get("models"), list)
            and isinstance(result.get("fetched_at"), str)
        ):
            self.catalog.cache(result["models"], result["fetched_at"])
            self._reload_models()

    @Slot()
    def _catalog_finished(self) -> None:
        if self.catalog_worker:
            self.catalog_worker.deleteLater()
        self.catalog_worker = None

    @Slot()
    def _start_runtime_for_saved_workspace(self) -> None:
        workspace: Path | None = None
        if self.workspace_path:
            try:
                workspace = canonical_workspace(Path(self.workspace_path))
            except BrokerError:
                self.workspace_path = ""
                self.storage.set_setting("workspace_path", "")
                self._reload_skills()
            else:
                self.workspace_path = str(workspace)
                self.storage.set_setting("workspace_path", self.workspace_path)
        self.runtime_target_identity = workspace_id(workspace) if workspace else None
        try:
            self.runtime_manager.ensure(workspace)
        except (OSError, RuntimeError, ValueError) as exc:
            self._runtime_failed(str(exc))

    @Slot(str, str)
    def _runtime_progress(self, state: str, message: str) -> None:
        self.runtime_state = state
        self.runtime_detail = message
        labels = {
            "starting": "Docker: starting",
            "stopping": "Docker: stopping",
            "pulling": "Runtime: downloading",
            "building": "Runtime: building",
            "checking": "Runtime: checking",
        }
        self.gateway_text = labels.get(state, "Runtime: working")
        self.gateway_detail = message
        self.last_gateway_status = None
        self.stateChanged.emit()

    @Slot(object)
    def _runtime_ready(self, result: object) -> None:
        if not isinstance(result, RuntimeResult):
            self._runtime_failed("The local runtime returned an invalid readiness result.")
            return
        if result.target.workspace_identity != self.runtime_target_identity:
            return
        self._gateway_token = result.gateway_token
        self.runtime_state = "ready"
        self.runtime_detail = "Local runtime started; checking gateway and executor health…"
        self.gateway_text = "Gateway: checking"
        self.gateway_detail = self.runtime_detail
        self.last_gateway_status = None
        self.settingsChanged.emit()
        self.stateChanged.emit()
        if self.health_worker:
            self._gateway_recheck_requested = True
        else:
            QTimer.singleShot(0, self.checkGateway)

    @Slot(str)
    def _runtime_failed(self, message: str) -> None:
        self.runtime_state = "failed"
        self.runtime_detail = message or "Local runtime setup failed."
        self.gateway_text = "Runtime: setup failed"
        self.gateway_detail = self.runtime_detail
        self.last_gateway_status = None
        self.stateChanged.emit()
        self.errorRequested.emit("Local runtime setup failed", self.runtime_detail)

    def _workspace_status_ready(self, status: object | None) -> bool:
        if (
            not self.workspace_path
            or self.runtime_state not in {"ready", "manual"}
            or status is None
        ):
            return False
        try:
            identity = workspace_id(Path(self.workspace_path))
        except BrokerError:
            return False
        supported = set(getattr(status, "supported_modes", ()))
        return bool(
            getattr(status, "ready", False)
            and getattr(status, "executor_present", False)
            and getattr(status, "executor_status", "") == "ready"
            and getattr(status, "active_workspace", None) == identity
            and {"plan", "agent"}.issubset(supported)
        )

    @Slot()
    def checkGateway(self) -> None:
        if self.health_worker or self._closing:
            return
        session_token = get_gateway_session_token()
        if session_token and session_token != self._gateway_token:
            self._gateway_token = session_token
            self.settingsChanged.emit()
        connection = self._gateway_connection()
        if connection is None:
            self._apply_health(
                HealthResult(
                    HealthState.DISCONNECTED,
                    "Gateway token not configured",
                )
            )
            return
        worker = GatewayHealthWorker(connection)
        worker.complete.connect(self._health_complete)
        worker.finished.connect(self._health_finished)
        self.health_worker = worker
        worker.start()

    @Slot(object)
    def _health_complete(self, result: object) -> None:
        if isinstance(result, HealthResult):
            self._apply_health(result)

    @Slot()
    def _health_finished(self) -> None:
        if self.health_worker:
            self.health_worker.deleteLater()
        self.health_worker = None
        if self._gateway_recheck_requested and not self._closing:
            self._gateway_recheck_requested = False
            QTimer.singleShot(0, self.checkGateway)

    def _apply_health(self, result: HealthResult) -> None:
        if self.runtimeBusy:
            return
        self.gateway_health_state = result.state
        self.last_gateway_status = result.status
        if self.runtime_state == "failed":
            self.gateway_text = "Runtime: setup failed"
            self.gateway_detail = self.runtime_detail
            self.stateChanged.emit()
            return
        labels = {
            HealthState.READY: "Gateway: ready",
            HealthState.STARTING: "Gateway: starting",
            HealthState.DEGRADED: "Gateway: degraded",
            HealthState.INCOMPATIBLE: "Gateway: incompatible",
            HealthState.DISCONNECTED: "Gateway: offline",
        }
        self.gateway_text = labels[result.state]
        self.gateway_detail = result.message
        if (
            result.state == HealthState.READY
            and self.workspace_path
            and not self._workspace_status_ready(result.status)
        ):
            self.gateway_text = "Executor: unavailable"
            self.gateway_detail = (
                "The isolated executor is not ready for this workspace. "
                "Retry the local runtime setup."
            )
        self.stateChanged.emit()
        if (
            result.state in {HealthState.READY, HealthState.DEGRADED}
            and self.catalog.is_stale()
            and not self._catalog_autorefresh_attempted
        ):
            self._catalog_autorefresh_attempted = True
            self._start_catalog_refresh(report_errors=False)

    @Slot()
    def showQuickChat(self) -> None:
        if self._window:
            self._window.showNormal()
            self._window.raise_()
            self._window.requestActivate()
        self.focusComposerRequested.emit()

    @Slot()
    def toggleWindowVisibility(self) -> None:
        if not self._window:
            return
        if (
            self._window.isVisible()
            and self._window.visibility() != QWindow.Visibility.Minimized
        ):
            self._window.hide()
        else:
            self.showQuickChat()

    @Slot()
    def shutdown(self) -> None:
        if self._closing:
            return
        self._closing = True
        self.health_timer.stop()
        if self.worker:
            self.worker.stop()
            self.worker.wait(1500)
        if self.catalog_worker:
            self.catalog_worker.wait(500)
        if self.health_worker:
            self.health_worker.wait(500)
        if self.runtime_automatic:
            self.runtime_manager.shutdown()
        if self.hotkey:
            self.hotkey.unregister()
            application = QGuiApplication.instance()
            if application:
                application.removeNativeEventFilter(self.hotkey)
        if self.tray:
            self.tray.close()
        self.storage.close()
        application = QGuiApplication.instance()
        if application:
            application.quit()

    def current_conversation(self) -> Conversation | None:
        if not self.current_conversation_id:
            return None
        return self.storage.get_conversation(self.current_conversation_id)

    def _load_conversations(self, select_id: str | None = None) -> None:
        previous_id = self.current_conversation_id
        conversations = self.storage.list_conversations(
            query=self.search_query, archived=self.show_archived
        )
        self._conversations = [
            {
                "id": item.id,
                "title": item.title,
                "pinned": bool(item.pinned_at),
                "archived": bool(item.archived_at),
                "model": item.model,
                "updatedAt": item.updated_at,
            }
            for item in conversations
        ]
        if not conversations and not self.search_query and not self.show_archived:
            conversation = self.conversation_controller.create_new(
                self.current_conversation(),
                self.storage.recent_model_ids(1),
            )
            self._load_conversations(conversation.id)
            return
        ids = {item.id for item in conversations}
        wanted = select_id or self.current_conversation_id
        self.current_conversation_id = (
            wanted if wanted in ids else (conversations[0].id if conversations else None)
        )
        self.conversationsChanged.emit()
        self._refresh_transcript(force_reset=self.current_conversation_id != previous_id)
        self.stateChanged.emit()
        self.settingsChanged.emit()

    def _schedule_transcript_refresh(self) -> None:
        self._transcript_refresh_timer.start()

    def _refresh_transcript(self, *, force_reset: bool = False) -> None:
        self._transcript_refresh_timer.stop()
        conversation = self.current_conversation()
        if not conversation:
            self._transcript = []
            self._transcript_html_cache = {}
        else:
            messages = self.storage.list_messages(conversation.id)
            activities = assemble_activities(
                self.storage.list_generation_runs(conversation.id),
                self.storage.list_run_events(
                    conversation.id,
                    event_types=ACTIVITY_EVENT_TYPES,
                    payload_keys=ACTIVITY_PAYLOAD_KEYS,
                ),
            )
            values = assemble_transcript(
                messages,
                activities,
                live_text="",
                live_events=(self.live_events if self.worker else ()),
            )
            html_cache: dict[tuple[str, str], str] = {}
            for item in values:
                content = str(item.get("content") or "")
                cache_key = (str(item.get("key") or ""), content)
                html = self._transcript_html_cache.get(cache_key)
                if html is None:
                    html = render_markdown(content)
                item["html"] = html
                html_cache[cache_key] = html
            self._transcript_html_cache = html_cache
            self._transcript = values
            summary = self.storage.usage_summary(conversation.id)
            if not self.worker and (
                summary["input_tokens"] or summary["output_tokens"] or summary["cost"]
            ):
                self.status_text = _usage_text(summary)
        self._transcript_model.replace(self._transcript, reset=force_reset)
        self.transcriptChanged.emit()
        self.stateChanged.emit()

    def _reload_models(self) -> None:
        favorites = set(self.storage.favorite_model_ids())
        recents = set(self.storage.recent_model_ids())
        alias_by_model = {model: alias for alias, model in self.storage.model_aliases().items()}
        values: list[dict[str, Any]] = []
        for model in self.catalog.models():
            values.append(
                {
                    "id": model.id,
                    "label": alias_by_model.get(model.id, model.label),
                    "description": model.description,
                    "contextLength": model.context_length,
                    "price": model.average_price_per_million,
                    "rank": model.artificial_analysis_rank,
                    "year": model.release_year,
                    "favorite": model.id in favorites,
                    "recent": model.id in recents,
                    "reasoning": model.supports("reasoning"),
                    "textCompatible": model.text_compatible,
                }
            )
        values.sort(
            key=lambda item: (
                not item["favorite"],
                not item["recent"],
                str(item["label"]).casefold(),
            )
        )
        self._models = values
        self.modelsChanged.emit()

    @Slot(str, bool, float, int, int, result="QVariantList")
    def filteredModels(
        self,
        query: str,
        text_only: bool,
        max_price: float,
        max_rank: int,
        year: int,
    ) -> list[dict[str, Any]]:
        by_id = {model.id: model for model in self.catalog.models()}
        return [
            item
            for item in self._models
            if matches_model_filters(
                by_id[item["id"]],
                query=query,
                text_only=text_only,
                max_price_per_million=(max_price if max_price >= 0 else None),
                max_artificial_analysis_rank=(max_rank if max_rank > 0 else None),
                release_year=year if year > 0 else None,
            )
        ]

    @Slot(str)
    def toggleFavoriteModel(self, model_id: str) -> None:
        if model_id in set(self.storage.favorite_model_ids()):
            self.storage.remove_favorite_model(model_id)
        else:
            self.storage.add_favorite_model(model_id)
        self._reload_models()

    @Slot(str, str)
    def saveModelAlias(self, alias: str, model_id: str) -> None:
        try:
            self.storage.save_model_alias(alias, model_id)
        except ValueError as exc:
            self.errorRequested.emit("Invalid alias", str(exc))
            return
        self._reload_models()

    def _reload_commands(self) -> None:
        builtins = [
            {
                "name": name,
                "description": description,
                "builtin": True,
            }
            for name, description in (
                ("new", "Start a new conversation"),
                ("mode", "Switch Chat, Plan, or Agent mode"),
                ("fork", "Fork the active branch"),
                ("compact", "Compact older context"),
                ("context", "Inspect submitted context"),
                ("cost", "Show token and cost usage"),
                ("status", "Show local gateway status"),
                ("stop", "Stop the active run"),
                ("pause", "Pause the agent at a safe boundary"),
            )
        ]
        self._commands = builtins + [
            {**item, "builtin": False} for item in self.storage.list_prompt_commands()
        ]
        self.commandsChanged.emit()

    def _reload_presets(self) -> None:
        self._presets = [asdict(item) for item in self.storage.list_prompt_presets()]
        self.presetsChanged.emit()

    def _reload_skills(self) -> None:
        if not self.workspace_path:
            self._skills = []
        else:
            identity = workspace_id(Path(self.workspace_path))
            active = set(self.storage.workspace_config(identity).get("active_skills") or ())
            self._skills = [
                {
                    "name": skill.name,
                    "description": skill.description,
                    "scope": skill.scope,
                    "active": skill.name in active,
                }
                for skill in available_skills(
                    app_data_dir() / "skills",
                    Path(self.workspace_path),
                )
            ]
        self.skillsChanged.emit()

    def _sync_workspace_config(self, identity: str, config: dict[str, object]) -> None:
        connection = self._gateway_connection()
        if not connection:
            return
        allowed = {
            key: config[key]
            for key in (
                "instructions",
                "active_skills",
                "default_mode",
                "context_policy",
                "custom_tools",
            )
            if key in config
        }
        try:
            GatewayClient(connection).save_workspace_config(identity, allowed)
        except GatewayError:
            pass

    def _gateway_connection(self) -> GatewayConnection | None:
        token = self._gateway_token
        if not token:
            return None
        url = (
            self.storage.get_setting("gateway_url", DEFAULT_GATEWAY_URL) or DEFAULT_GATEWAY_URL
        )
        try:
            return GatewayConnection(url, token)
        except ValueError:
            return None

    def _set_status(self, value: str) -> None:
        self.status_text = value
        self.stateChanged.emit()


def _local_path(value: str) -> Path | None:
    local = QUrl(value).toLocalFile() if value.startswith("file:") else value
    return Path(local) if local else None


def _usage_text(usage: dict[str, Any]) -> str:
    prompt = int(usage.get("input_tokens") or 0)
    completion = int(usage.get("output_tokens") or 0)
    cached = int(usage.get("cached_tokens") or 0)
    reasoning = int(usage.get("reasoning_tokens") or 0)
    cost = float(usage.get("cost") or 0)
    parts = [
        f"{prompt + completion:,} billed tokens",
        f"USD {cost:.6f}",
    ]
    if cached:
        parts.append(f"{cached:,} cached")
    if reasoning:
        parts.append(f"{reasoning:,} reasoning")
    return " · ".join(parts)
