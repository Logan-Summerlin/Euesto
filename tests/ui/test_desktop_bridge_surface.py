"""The QML-facing surface of ``DesktopBridge`` is a contract.

``DesktopBridge`` is a thin adapter over the desktop services in ``src/desktop/``. Refactoring
those services must not change what QML sees: every property (type, notify signal), signal,
and public slot signature below is pinned, and every ``backend.<name>`` the QML files use must
exist on the bridge.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PySide6.QtCore import QMetaMethod, QObject

    from app import DesktopBridge as AppDesktopBridge
    from src.qml_backend import DesktopBridge
except ImportError as exc:  # pragma: no cover - Qt unavailable
    pytest.skip(f"Desktop Qt bridge unavailable: {exc}", allow_module_level=True)

ROOT = Path(__file__).resolve().parents[2]

PROPERTIES = {
    'acceptEditsAvailable': ('bool', 'stateChanged', False),
    'acceptEditsEnabled': ('bool', 'stateChanged', False),
    'approvalPolicy': ('QString', 'stateChanged', False),
    'archivedView': ('bool', 'stateChanged', False),
    'autoModeAvailable': ('bool', 'stateChanged', False),
    'autoModeEnabled': ('bool', 'stateChanged', False),
    'commands': ('QVariantList', 'commandsChanged', False),
    'conversations': ('QVariantList', 'conversationsChanged', False),
    'currentConversationId': ('QString', 'stateChanged', False),
    'currentMode': ('QString', 'stateChanged', False),
    'currentModel': ('QString', 'stateChanged', False),
    'currentTitle': ('QString', 'stateChanged', False),
    'gatewayDetail': ('QString', 'stateChanged', False),
    'gatewaySettings': ('QVariantMap', 'settingsChanged', False),
    'gatewayText': ('QString', 'stateChanged', False),
    'generating': ('bool', 'stateChanged', False),
    'investigationModel': ('QString', 'settingsChanged', False),
    'modelOptions': ('QVariantMap', 'settingsChanged', False),
    'models': ('QVariantList', 'modelsChanged', False),
    'permissions': ('QVariantList', 'permissionsChanged', False),
    'presets': ('QVariantList', 'presetsChanged', False),
    'reasoningEffort': ('QString', 'settingsChanged', False),
    'runtimeBusy': ('bool', 'stateChanged', False),
    'runtimeDetail': ('QString', 'stateChanged', False),
    'runtimeState': ('QString', 'stateChanged', False),
    'serverTools': ('QVariantMap', 'settingsChanged', False),
    'skills': ('QVariantList', 'skillsChanged', False),
    'stagingBusy': ('bool', 'stateChanged', False),
    'statusText': ('QString', 'stateChanged', False),
    'systemPrompt': ('QString', 'settingsChanged', False),
    'theme': ('QString', 'settingsChanged', False),
    'transcript': ('QVariantList', 'transcriptChanged', False),
    'transcriptModel': ('QObject*', None, True),
    'usage': ('QVariantMap', 'settingsChanged', False),
    'workspacePath': ('QString', 'stateChanged', False),
    'workspaceReady': ('bool', 'stateChanged', False),
}
SIGNALS = {
    'approvalRequested(QVariantMap)',
    'commandsChanged()',
    'confirmRequested(QString,QString,QString)',
    'conversationsChanged()',
    'errorRequested(QString,QString)',
    'fileExported(QString)',
    'fileImported(QString)',
    'focusComposerRequested()',
    'infoRequested(QString,QString)',
    'modelsChanged()',
    'permissionsChanged()',
    'presetsChanged()',
    'runtimeSetupFinished(bool)',
    'runtimeSetupStarted()',
    'settingsChanged()',
    'skillsChanged()',
    'stateChanged()',
    'transcriptChanged()',
}
SLOTS = {
    'applyPromptPreset(QString)': 'void',
    'attachWindow(QWindow*)': 'void',
    'checkGateway()': 'void',
    'compactContext()': 'void',
    'deletePermission(QString)': 'void',
    'deletePromptCommand(QString)': 'void',
    'deletePromptPreset(QString)': 'void',
    'editMessage(int,QString)': 'void',
    'exportConversation(QString,QString)': 'void',
    'filteredModels(QString,bool,double,int,int)': 'QVariantList',
    'forkConversation()': 'void',
    'importConversation(QString)': 'void',
    'inspectContext()': 'void',
    'loadPermissionRules()': 'void',
    'navigateBranch(int,int)': 'void',
    'newConversation()': 'void',
    'pauseAgent()': 'void',
    'refreshCatalog()': 'void',
    'refreshSkills()': 'void',
    'regenerateMessage(int)': 'void',
    'renameConversation(QString)': 'void',
    'requestAcceptEdits(bool)': 'void',
    'requestAutoMode(bool)': 'void',
    'requestDeleteConversation()': 'void',
    'requestDiscardStaging()': 'void',
    'resolveApproval(QString,QString)': 'void',
    'resolveConfirmation(QString,bool)': 'void',
    'resumableRuns()': 'QVariantList',
    'resumeRun(QString)': 'void',
    'retryRuntime()': 'void',
    'reviewStaging()': 'void',
    'saveActiveSkills(QString)': 'void',
    'saveApiKey(QString)': 'void',
    'saveGateway(QString,QString)': 'void',
    'saveInvestigationModel(QString)': 'void',
    'saveModelAlias(QString,QString)': 'void',
    'saveModelOptions(QVariantMap)': 'void',
    'savePromptCommand(QString,QString,QString)': 'void',
    'savePromptPreset(QString,QString,QString)': 'void',
    'saveSystemPrompt(QString)': 'void',
    'saveWorkspaceConfiguration(QString,QString)': 'void',
    'selectConversation(QString)': 'void',
    'selectMode(QString)': 'void',
    'selectModel(QString)': 'void',
    'selectWorkspace(QString)': 'void',
    'sendMessage(QString,bool)': 'void',
    'setConversationSearch(QString)': 'void',
    'setPermissionEnabled(QString,bool)': 'void',
    'setReasoningEffort(QString)': 'void',
    'setServerTool(QString,bool)': 'void',
    'setTheme(QString)': 'void',
    'showGatewayStatus()': 'void',
    'showQuickChat()': 'void',
    'showUsage()': 'void',
    'shutdown()': 'void',
    'stopGeneration()': 'void',
    'toggleArchive()': 'void',
    'toggleArchived()': 'void',
    'toggleFavoriteModel(QString)': 'void',
    'togglePin()': 'void',
    'toggleWindowVisibility()': 'void',
    'workspaceConfiguration()': 'QVariantMap',
}


def _surface(cls) -> tuple[dict, set, dict, set]:
    # Everything declared below QObject, including what a subclass inherits from the bridge.
    meta = cls.staticMetaObject
    base = QObject.staticMetaObject
    properties = {}
    for index in range(base.propertyCount(), meta.propertyCount()):
        prop = meta.property(index)
        notify = prop.notifySignal().name().data().decode() if prop.hasNotifySignal() else None
        properties[prop.name()] = (prop.typeName(), notify, prop.isConstant())
    signals, slots, private = set(), {}, set()
    for index in range(base.methodCount(), meta.methodCount()):
        method = meta.method(index)
        signature = method.methodSignature().data().decode()
        if method.methodType() == QMetaMethod.MethodType.Signal:
            signals.add(signature)
        elif method.methodType() == QMetaMethod.MethodType.Slot:
            if signature.startswith("_"):
                private.add(signature)
            else:
                slots[signature] = method.typeName()
    return properties, signals, slots, private


def test_bridge_properties_signals_and_slots_are_unchanged() -> None:
    properties, signals, slots, _private = _surface(DesktopBridge)
    assert properties == PROPERTIES
    assert signals == SIGNALS
    assert slots == SLOTS


def test_bridge_keeps_worker_plumbing_out_of_its_qml_surface() -> None:
    # Worker completion handlers live on the services, not on the QML adapter.
    _properties, _signals, _slots, private = _surface(DesktopBridge)
    assert private == set()


def test_every_backend_member_used_by_qml_exists() -> None:
    used: set[str] = set()
    for path in (ROOT / "qml").glob("*.qml"):
        used.update(re.findall(r"\bbackend\.([A-Za-z_]\w*)", path.read_text(encoding="utf-8")))
    available = set(PROPERTIES) | {signature.split("(", 1)[0] for signature in (*SIGNALS, *SLOTS)}
    assert used and used <= available, sorted(used - available)


def test_application_uses_the_bridge_directly() -> None:
    assert AppDesktopBridge is DesktopBridge


def test_bridge_is_a_thin_adapter() -> None:
    source = (ROOT / "src" / "qml_backend.py").read_text(encoding="utf-8")
    assert len(source.splitlines()) < 900
