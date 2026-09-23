from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from PySide6.QtCore import Property, QObject, QTimer, Signal, Slot
from PySide6.QtGui import QGuiApplication, QWindow

from .desktop import (
    ConversationService,
    GenerationService,
    RuntimeService,
    SettingsService,
    StagingPublicationService,
)
from .model_catalog import filter_model_entries
from .settings import database_path
from .storage import Storage
from .window_services import GlobalQuickChatHotkey, TrayService


class DesktopBridge(QObject):
    """Thin QML adapter over the desktop services in ``src/desktop/``.

    The bridge owns only what QML binds to (signals, properties, and slots), the status line,
    pending confirmations, and window integration. Behavior lives in the composed services:
    ``runtime``, ``settings``, ``history`` (conversations), ``generation``, and ``staging``.
    Security-sensitive authority remains in the Python backends those services call.
    """

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
        self.status_text = "Ready"
        self._models: list[dict[str, Any]] = []
        self._pending_confirmation: dict[str, tuple[Callable[[], None], Callable[[], None] | None]] = {}
        self._window: QWindow | None = None
        self.tray: TrayService | None = None
        self.hotkey: GlobalQuickChatHotkey | None = None
        self._closing = False
        self.runtime = RuntimeService(self, self.storage)
        self.settings = SettingsService(self, self.storage)
        self.history = ConversationService(self, self.storage)
        self.generation = GenerationService(self, self.storage)
        self.staging = StagingPublicationService(self)
        self.reload_models()
        self.settings.reload_commands()
        self.settings.reload_presets()
        self.history.load()
        self.runtime.start()

    # -- host interface used by the services -------------------------------------------

    def set_status(self, text: str) -> None:
        self.status_text = text
        self.stateChanged.emit()

    def confirm(self, token: str, title: str, body: str, on_accept: Callable[[], None], on_decline: Callable[[], None] | None = None) -> None:
        self._pending_confirmation[token] = (on_accept, on_decline)
        self.confirmRequested.emit(token, title, body)

    def reload_models(self) -> None:
        self._models = self.settings.model_entries()
        self.modelsChanged.emit()

    # -- lists -------------------------------------------------------------------------

    @Property("QVariantList", notify=conversationsChanged)
    def conversations(self) -> list[dict[str, Any]]:
        return self.history.conversations

    @Property("QVariantList", notify=transcriptChanged)
    def transcript(self) -> list[dict[str, Any]]:
        return self.history.transcript

    @Property(QObject, constant=True)
    def transcriptModel(self) -> QObject:
        return self.history.transcript_model

    @Property("QVariantList", notify=modelsChanged)
    def models(self) -> list[dict[str, Any]]:
        return self._models

    @Property("QVariantList", notify=permissionsChanged)
    def permissions(self) -> list[dict[str, Any]]:
        return self.settings.permissions

    @Property("QVariantList", notify=commandsChanged)
    def commands(self) -> list[dict[str, Any]]:
        return self.settings.commands

    @Property("QVariantList", notify=skillsChanged)
    def skills(self) -> list[dict[str, Any]]:
        return self.settings.skills

    @Property("QVariantList", notify=presetsChanged)
    def presets(self) -> list[dict[str, Any]]:
        return self.settings.presets

    # -- session state -----------------------------------------------------------------

    @Property(str, notify=stateChanged)
    def currentConversationId(self) -> str:
        return self.history.current_id or ""

    @Property(str, notify=stateChanged)
    def currentTitle(self) -> str:
        conversation = self.history.current()
        return conversation.title if conversation else ""

    @Property(str, notify=stateChanged)
    def currentModel(self) -> str:
        return self.history.current_model()

    @Property(str, notify=stateChanged)
    def currentMode(self) -> str:
        return self.generation.mode

    @Property(bool, notify=stateChanged)
    def autoModeEnabled(self) -> bool:
        return self.generation.auto_mode

    @Property(bool, notify=stateChanged)
    def autoModeAvailable(self) -> bool:
        return self.runtime.has_capability("agent_auto")

    @Property(bool, notify=stateChanged)
    def acceptEditsEnabled(self) -> bool:
        return self.generation.accept_edits

    @Property(bool, notify=stateChanged)
    def acceptEditsAvailable(self) -> bool:
        return self.runtime.has_capability("agent_accept_edits")

    @Property(str, notify=stateChanged)
    def approvalPolicy(self) -> str:
        return self.generation.approval_policy()

    @Property(str, notify=stateChanged)
    def workspacePath(self) -> str:
        return self.runtime.workspace_path

    @Property(str, notify=stateChanged)
    def statusText(self) -> str:
        return self.status_text

    @Property(str, notify=stateChanged)
    def gatewayText(self) -> str:
        return self.runtime.gateway_text

    @Property(str, notify=stateChanged)
    def gatewayDetail(self) -> str:
        return self.runtime.gateway_detail

    @Property(str, notify=stateChanged)
    def runtimeState(self) -> str:
        return self.runtime.state

    @Property(str, notify=stateChanged)
    def runtimeDetail(self) -> str:
        return self.runtime.detail

    @Property(bool, notify=stateChanged)
    def runtimeBusy(self) -> bool:
        return self.runtime.busy

    @Property(bool, notify=stateChanged)
    def workspaceReady(self) -> bool:
        return self.runtime.workspace_ready()

    @Property(bool, notify=stateChanged)
    def generating(self) -> bool:
        return self.generation.running

    @Property(bool, notify=stateChanged)
    def stagingBusy(self) -> bool:
        return self.staging.busy

    @Property(bool, notify=stateChanged)
    def archivedView(self) -> bool:
        return self.history.show_archived

    # -- settings ----------------------------------------------------------------------

    @Property(str, notify=settingsChanged)
    def theme(self) -> str:
        return self.settings.theme

    @Property(str, notify=settingsChanged)
    def systemPrompt(self) -> str:
        conversation = self.history.current()
        return conversation.system_prompt if conversation else ""

    @Property(str, notify=settingsChanged)
    def reasoningEffort(self) -> str:
        return self.settings.request_options(self.history.current_model()).reasoning_effort or "default"

    @Property("QVariantMap", notify=settingsChanged)
    def modelOptions(self) -> dict[str, Any]:
        return asdict(self.settings.request_options(self.history.current_model()))

    @Property("QVariantMap", notify=settingsChanged)
    def serverTools(self) -> dict[str, bool]:
        return self.settings.server_tools()

    @Property(str, notify=settingsChanged)
    def investigationModel(self) -> str:
        return self.settings.investigation_model

    @Property("QVariantMap", notify=settingsChanged)
    def gatewaySettings(self) -> dict[str, Any]:
        return self.settings.gateway_settings()

    @Property("QVariantMap", notify=settingsChanged)
    def usage(self) -> dict[str, Any]:
        return dict(self.storage.usage_summary(self.history.current_id))

    @Slot(str)
    def saveInvestigationModel(self, model_id: str) -> None:
        self.settings.save_investigation_model(model_id)

    @Slot(str)
    def setReasoningEffort(self, effort: str) -> None:
        self.settings.set_reasoning_effort(self.history.current_model(), effort)

    @Slot("QVariantMap")
    def saveModelOptions(self, values: dict[str, Any]) -> None:
        self.settings.save_model_options(self.history.current_model(), values)

    @Slot(str, bool)
    def setServerTool(self, name: str, enabled: bool) -> None:
        self.settings.set_server_tool(name, enabled)

    @Slot(str)
    def setTheme(self, theme: str) -> None:
        self.settings.set_theme(theme)

    @Slot(str, str)
    def saveGateway(self, url: str, token: str) -> None:
        self.settings.save_gateway(url, token)

    @Slot(str)
    def saveApiKey(self, value: str) -> None:
        self.settings.save_api_key(value)

    @Slot(str, str, str)
    def savePromptCommand(self, name: str, description: str, template: str) -> None:
        self.settings.save_prompt_command(name, description, template)

    @Slot(str)
    def deletePromptCommand(self, name: str) -> None:
        self.settings.delete_prompt_command(name)

    @Slot(str, str, str)
    def savePromptPreset(self, preset_id: str, name: str, content: str) -> None:
        self.settings.save_prompt_preset(preset_id, name, content)

    @Slot(str)
    def applyPromptPreset(self, preset_id: str) -> None:
        self.history.apply_prompt_preset(preset_id)

    @Slot(str)
    def deletePromptPreset(self, preset_id: str) -> None:
        self.settings.delete_prompt_preset(preset_id)

    @Slot()
    def refreshSkills(self) -> None:
        self.settings.reload_skills()

    @Slot(str)
    def saveActiveSkills(self, csv_names: str) -> None:
        self.settings.save_active_skills(csv_names)

    @Slot(str, str)
    def saveWorkspaceConfiguration(self, instructions: str, declarations: str) -> None:
        self.settings.save_workspace_configuration(instructions, declarations)

    @Slot(result="QVariantMap")
    def workspaceConfiguration(self) -> dict[str, Any]:
        return self.settings.workspace_configuration()

    @Slot()
    def loadPermissionRules(self) -> None:
        # Deferred so a rule change reloads after the gateway call that triggered it returns.
        QTimer.singleShot(0, self.settings.load_permission_rules)

    @Slot(str, bool)
    def setPermissionEnabled(self, rule_id: str, enabled: bool) -> None:
        self.settings.set_permission_enabled(rule_id, enabled)

    @Slot(str)
    def deletePermission(self, rule_id: str) -> None:
        self.settings.delete_permission(rule_id)

    @Slot()
    def refreshCatalog(self) -> None:
        self.settings.start_catalog_refresh(report_errors=True)

    @Slot(str, bool, float, int, int, result="QVariantList")
    def filteredModels(self, query: str, text_only: bool, max_price: float, max_rank: int, year: int) -> list[dict[str, Any]]:
        return filter_model_entries(self._models, query, text_only, max_price, max_rank, year)

    @Slot(str)
    def toggleFavoriteModel(self, model_id: str) -> None:
        self.settings.toggle_favorite_model(model_id)

    @Slot(str, str)
    def saveModelAlias(self, alias: str, model_id: str) -> None:
        self.settings.save_model_alias(alias, model_id)

    # -- conversations -----------------------------------------------------------------

    @Slot()
    def newConversation(self) -> None:
        self.history.new_conversation()

    @Slot(str)
    def selectConversation(self, conversation_id: str) -> None:
        self.history.select(conversation_id)

    @Slot(str)
    def setConversationSearch(self, query: str) -> None:
        self.history.set_search(query)

    @Slot()
    def toggleArchived(self) -> None:
        self.history.toggle_archived_view()

    @Slot(str)
    def renameConversation(self, title: str) -> None:
        self.history.rename(title)

    @Slot()
    def togglePin(self) -> None:
        self.history.toggle_pin()

    @Slot()
    def toggleArchive(self) -> None:
        self.history.toggle_archive()

    @Slot()
    def requestDeleteConversation(self) -> None:
        self.history.request_delete()

    @Slot()
    def forkConversation(self) -> None:
        self.history.fork()

    @Slot(str)
    def selectModel(self, model_id: str) -> None:
        self.history.select_model(model_id)

    @Slot(str)
    def saveSystemPrompt(self, prompt: str) -> None:
        self.history.save_system_prompt(prompt)

    @Slot(int, int)
    def navigateBranch(self, message_id: int, direction: int) -> None:
        self.history.navigate_branch(message_id, direction)

    @Slot()
    def compactContext(self) -> None:
        self.history.compact_context()

    @Slot()
    def inspectContext(self) -> None:
        self.history.inspect_context()

    @Slot()
    def showUsage(self) -> None:
        self.history.show_usage()

    @Slot(str)
    def importConversation(self, value: str) -> None:
        self.history.import_file(value)

    @Slot(str, str)
    def exportConversation(self, value: str, format_name: str = "json") -> None:
        self.history.export_file(value, format_name)

    # -- generation --------------------------------------------------------------------

    @Slot(str)
    def selectMode(self, mode: str) -> None:
        self.generation.select_mode(mode)

    @Slot(bool)
    def requestAutoMode(self, enabled: bool) -> None:
        self.generation.request_auto_mode(enabled)

    @Slot(bool)
    def requestAcceptEdits(self, enabled: bool) -> None:
        self.generation.request_accept_edits(enabled)

    @Slot(str, bool)
    def sendMessage(self, text: str, steer: bool = False) -> None:
        self.generation.send_message(text, steer)

    @Slot(str, str)
    def resolveApproval(self, key: str, decision: str) -> None:
        self.generation.resolve_approval(key, decision)

    @Slot()
    def stopGeneration(self) -> None:
        self.generation.stop()

    @Slot()
    def pauseAgent(self) -> None:
        self.generation.pause()

    @Slot(str)
    def resumeRun(self, run_id: str) -> None:
        self.generation.resume_run(run_id)

    @Slot(int, str)
    def editMessage(self, message_id: int, value: str) -> None:
        self.generation.edit_message(message_id, value)

    @Slot(int)
    def regenerateMessage(self, message_id: int) -> None:
        self.generation.regenerate_message(message_id)

    # -- staging and publication -------------------------------------------------------

    @Slot()
    def requestDiscardStaging(self) -> None:
        self.staging.request_discard()

    @Slot()
    def reviewStaging(self) -> None:
        self.staging.review()

    # -- workspace runtime -------------------------------------------------------------

    @Slot(str)
    def selectWorkspace(self, value: str) -> None:
        if not (self.generation.running or self.staging.busy):
            self.runtime.select_workspace(value)

    @Slot()
    def retryRuntime(self) -> None:
        self.runtime.retry()

    @Slot()
    def checkGateway(self) -> None:
        self.runtime.check_gateway()

    @Slot()
    def showGatewayStatus(self) -> None:
        self.runtime.show_status()

    @Slot(result="QVariantList")
    def resumableRuns(self) -> list[str]:
        return self.runtime.resumable_runs()

    # -- confirmations and window ------------------------------------------------------

    @Slot(str, bool)
    def resolveConfirmation(self, token: str, accepted: bool) -> None:
        pending = self._pending_confirmation.pop(token, None)
        if not pending:
            return
        on_accept, on_decline = pending
        if accepted:
            on_accept()
        elif on_decline is not None:
            on_decline()

    @Slot(QWindow)
    def attachWindow(self, window: QWindow) -> None:
        if self._window is window:
            return
        self._window = window
        self.tray = TrayService(window, show_quick_chat=self.showQuickChat, toggle_visibility=self.toggleWindowVisibility, close=self.shutdown)
        self.hotkey = GlobalQuickChatHotkey(window, self.showQuickChat)
        application = QGuiApplication.instance()
        if application:
            application.installNativeEventFilter(self.hotkey)
        self.hotkey.register()

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
        if self._window.isVisible() and self._window.visibility() != QWindow.Visibility.Minimized:
            self._window.hide()
        else:
            self.showQuickChat()

    @Slot()
    def shutdown(self) -> None:
        if self._closing:
            return
        self._closing = True
        self.generation.shutdown()
        self.settings.shutdown()
        self.runtime.shutdown()
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
