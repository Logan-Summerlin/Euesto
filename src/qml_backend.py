from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from PySide6.QtCore import Property, QObject, QTimer, Signal, Slot
from PySide6.QtGui import QGuiApplication, QWindow

from .desktop import (
    ConversationService,
    GenerationService,
    PolicyService,
    RuntimeService,
    SettingsService,
    StagingPublicationService,
)
from .model_catalog import AGENT_MODES, filter_model_entries
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
    meterChanged = Signal()
    conflictsChanged = Signal()
    leaveRequested = Signal(str, str)
    policyChanged = Signal()
    reviewChanged = Signal()

    def __init__(self, storage: Storage | None = None):
        super().__init__()
        self.storage = storage or Storage(database_path())
        self.status_text = "Ready"
        self._models: list[dict[str, Any]] = []
        self._pending_confirmation: dict[
            str, tuple[Callable[[], None], Callable[[], None] | None]
        ] = {}
        self._window: QWindow | None = None
        self.tray: TrayService | None = None
        self.hotkey: GlobalQuickChatHotkey | None = None
        self._closing = False
        self.runtime = RuntimeService(self, self.storage)
        self.settings = SettingsService(self, self.storage)
        self.history = ConversationService(self, self.storage)
        self.generation = GenerationService(self, self.storage)
        self.staging = StagingPublicationService(self)
        self.policy = PolicyService(self, self.storage)
        self.reload_models()
        self.settings.reload_commands()
        self.settings.reload_presets()
        self.history.load()
        self.runtime.start()

    # -- host interface used by the services -------------------------------------------

    def set_status(self, text: str) -> None:
        self.status_text = text
        self.stateChanged.emit()

    def confirm(
        self,
        token: str,
        title: str,
        body: str,
        on_accept: Callable[[], None],
        on_decline: Callable[[], None] | None = None,
    ) -> None:
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
    def sandboxEnabled(self) -> bool:
        return self.generation.sandbox

    @Property(bool, notify=stateChanged)
    def sandboxAvailable(self) -> bool:
        return self.runtime.has_capability("agent_sandbox")

    @Property(str, notify=stateChanged)
    def networkMode(self) -> str:
        return self.runtime.network_mode

    @Property(bool, notify=policyChanged)
    def registriesAllowed(self) -> bool:
        return self.policy.allows_registries() and self.runtime.automatic

    @Property("QVariantMap", notify=stateChanged)
    def agentPlan(self) -> dict[str, Any]:
        return dict(self.generation.plan or {"steps": [], "note": ""})

    @Property(bool, notify=stateChanged)
    def budgetContinuationAvailable(self) -> bool:
        return bool(self.generation.budget_paused_run) and not self.generation.running

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

    @Property("QVariantMap", notify=meterChanged)
    def runMeter(self) -> dict[str, Any]:
        return self.generation.meter.to_dict()

    @Property("QVariantList", notify=conflictsChanged)
    def stagingConflicts(self) -> list[dict[str, Any]]:
        return list(self.staging.conflicts)

    @Property(bool, notify=stateChanged)
    def archivedView(self) -> bool:
        return self.history.show_archived

    @Property("QVariantMap", notify=reviewChanged)
    def stagingReview(self) -> dict[str, Any]:
        return self.staging.review_view()

    @Property("QVariantMap", notify=policyChanged)
    def workspacePolicy(self) -> dict[str, Any]:
        return self.policy.view()

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
        return (
            self.settings.request_options(self.history.current_model()).reasoning_effort
            or "default"
        )

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
    def runtimeSettings(self) -> dict[str, Any]:
        return self.settings.runtime_settings_view()

    @Property("QVariantList", notify=settingsChanged)
    def stagedSessions(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self.runtime.staged_sessions]

    @Slot("QVariantMap", result=bool)
    def saveRuntimeSettings(self, values: dict[str, Any]) -> bool:
        return self.settings.save_runtime_settings(dict(values))

    @Slot()
    def refreshStagedSessions(self) -> None:
        self.runtime.refresh_staged_sessions()

    @Slot(str)
    def removeStagedSession(self, key: str) -> None:
        self.runtime.request_remove_staged_session(key)

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

    @Slot(bool)
    def setWebTools(self, enabled: bool) -> None:
        self.settings.set_web_tools(enabled)

    @Slot(str)
    def saveUnredactedPaths(self, text: str) -> None:
        self.settings.save_unredacted_paths(text)

    @Slot("QVariantMap")
    def addPermissionRule(self, spec: dict[str, Any]) -> None:
        self.settings.add_permission_rule(dict(spec))

    @Slot()
    def refreshPolicy(self) -> None:
        self.policy.refresh()

    @Slot()
    def confirmPolicy(self) -> None:
        self.policy.request_confirm()

    @Slot(bool)
    def runDependencySetup(self, full: bool) -> None:
        self.policy.request_setup(full)

    @Slot()
    def loadEgressAudit(self) -> None:
        self.policy.load_audit()

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
    def filteredModels(
        self, query: str, text_only: bool, max_price: float, max_rank: int, year: int
    ) -> list[dict[str, Any]]:
        return filter_model_entries(
            self._models,
            query,
            text_only,
            max_price,
            max_rank,
            year,
            agent_only=self.generation.mode in AGENT_MODES,
        )

    @Slot(str)
    def toggleFavoriteModel(self, model_id: str) -> None:
        self.settings.toggle_favorite_model(model_id)

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
    def requestSandbox(self, enabled: bool) -> None:
        self.generation.request_sandbox(enabled)

    @Slot(bool)
    def requestRegistryAccess(self, enabled: bool) -> None:
        self.generation.request_registry_access(enabled)

    @Slot()
    def continueWithBudget(self) -> None:
        self.generation.continue_with_budget()

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

    @Slot()
    def publishStaged(self) -> None:
        self.staging.publish_staged()

    @Slot()
    def openReview(self) -> None:
        self.staging.open_review()

    @Slot()
    def closeReview(self) -> None:
        self.staging.close_review()

    @Slot(str, bool)
    def setReviewSelected(self, path: str, selected: bool) -> None:
        self.staging.set_review_selected(path, selected)

    @Slot(bool)
    def setReviewAll(self, selected: bool) -> None:
        self.staging.set_review_all(selected)

    @Slot()
    def publishSelection(self) -> None:
        self.staging.publish_selection()

    @Slot(str)
    def runCheck(self, name: str) -> None:
        self.staging.run_check(name)

    @Slot()
    def rebaseStaging(self) -> None:
        self.staging.rebase()

    @Slot(str, str)
    def resolveConflict(self, path: str, choice: str) -> None:
        self.staging.resolve_conflict(path, choice)

    @Slot(str)
    def resolveLeave(self, choice: str) -> None:
        self.staging.resolve_leave(choice)

    # -- workspace runtime -------------------------------------------------------------

    @Slot(str)
    def selectWorkspace(self, value: str) -> None:
        if self.generation.running or self.staging.busy:
            return
        if self.runtime.is_current_workspace(value):
            self.runtime.select_workspace(value)
            return
        self.staging.guard_leave(lambda: self.runtime.select_workspace(value), closing=False)

    @Slot()
    def retryRuntime(self) -> None:
        self.runtime.retry()

    @Slot()
    def showGatewayStatus(self) -> None:
        self.runtime.show_status()

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
        self.tray = TrayService(
            window,
            show_quick_chat=self.showQuickChat,
            toggle_visibility=self.toggleWindowVisibility,
            close=self.requestClose,
        )
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
        if (
            self._window.isVisible()
            and self._window.visibility() != QWindow.Visibility.Minimized
        ):
            self._window.hide()
        else:
            self.showQuickChat()

    @Slot()
    def requestClose(self) -> None:
        """Close the app once staged work is published, discarded, or kept (plan 0.6)."""
        if not self._closing:
            self.staging.guard_leave(self.shutdown, closing=True)

    @Slot()
    def shutdown(self) -> None:
        if self._closing:
            return
        self._closing = True
        self.generation.shutdown()
        self.settings.shutdown()
        self.policy.shutdown()
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
