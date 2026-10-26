"""Structural checks: QML visual invariants and desktop packaging that no runtime API exposes.

Transcript layout behavior (exact-height rows, scroll anchoring, streaming rows, no delegate
virtualization) is exercised by ``tests/ui/test_transcript_qml.py``; stream buffering and
event persistence by ``tests/unit/desktop/test_desktop_services.py``. Only what those cannot
observe is pinned here.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
QML_COMPONENTS = ("Main.qml", "Sidebar.qml", "Transcript.qml", "Composer.qml")


def _qml(name: str) -> str:
    return (ROOT / "qml" / name).read_text(encoding="utf-8")


def test_desktop_starts_qml_with_the_fusion_style_and_packages_it() -> None:
    app_source = (ROOT / "app.py").read_text(encoding="utf-8")
    spec_source = (ROOT / "build" / "chatbot.spec").read_text(encoding="utf-8")
    assert "QQmlApplicationEngine" in app_source
    # The style must be chosen once, before the application object exists.
    assert app_source.count("QQuickStyle.setStyle(") == 1
    assert app_source.index('QQuickStyle.setStyle("Fusion")') < app_source.index(
        "QApplication(sys.argv)"
    )
    assert all((ROOT / "qml" / name).is_file() for name in QML_COMPONENTS)
    assert 'project_root / "qml"' in spec_source


def test_settings_load_optional_values_without_assigning_undefined() -> None:
    source = _qml("Main.qml")
    assert "function optionalText(value)" in source
    assert "temperature.text = optionalText(options.temperature)" in source
    assert "topP.text = optionalText(options.top_p)" in source
    assert (
        'Shortcut { sequence: "Ctrl+Comma"; onActivated: settingsDialog.openAndLoad() }'
        in source
    )
    assert 'text: "Auto"' in source
    assert "backend.requestAutoMode(checked)" in source
    assert "enabled: presetBox.currentIndex >= 0" in source


def test_transcript_binds_its_model_directly_and_guards_optional_values() -> None:
    source = _qml("Transcript.qml")
    # The model is a context property, not a per-binding bridge lookup.
    assert "model: transcriptModel" in source and "model: backend.transcriptModel" not in source
    assert "card.value.streaming === true" in source
    assert 'String(card.value.metadata || "").length' in source
    assert "policy: ScrollBar.AlwaysOn" in source
    assert "minimumSize: 0.08" in source


def test_transcript_activity_button_is_native_style_safe() -> None:
    source = _qml("Transcript.qml")
    start = source.index("id: activityButton")
    activity_button = source[start : source.index("id: activityLoader", start)]
    assert "contentItem:" not in activity_button
    assert "palette.buttonText: root.mutedColor" in activity_button


def test_tool_checkboxes_use_compact_indicators() -> None:
    source = _qml("Composer.qml")
    assert "component ToolCheckBox: CheckBox" in source
    assert "width: 13" in source and "height: 13" in source
    assert "leftPadding: 0" in source
    assert "leftPadding: control.indicator.width + control.spacing" in source
    assert "contentItem: Label" in source
    assert source.count("ToolCheckBox {") == 3


def test_closing_and_switching_workspace_never_drop_staged_work_silently() -> None:
    """Plan 0.6: the window close goes through the staging guard and its four choices."""
    source = _qml("Main.qml")
    closing = source[source.index("onClosing:") : source.index("Shortcut {")]
    assert "backend.requestClose()" in closing and "backend.shutdown()" not in closing
    dialog = source[source.index('objectName: "leaveDialog"') :]
    for choice in ("publish", "discard", "keep", "cancel"):
        assert f'leaveDialog.choose("{choice}")' in dialog
    for label in ("Review and publish", "Discard", "Keep for later", "Cancel"):
        assert f'text: "{label}"' in dialog
    # Dismissing the dialog any other way is a cancel, never a silent proceed.
    assert 'onClosed: if (!resolved) backend.resolveLeave("cancel")' in dialog
    assert "function onLeaveRequested(title, message)" in source


def test_settings_describe_workspace_instructions_and_runner_controls() -> None:
    """Plan 0.3 help text, and the runner (2.2, 2.6, 2.7) and retention (2.4) settings."""
    source = _qml("Main.qml")
    help_text = source[source.index('objectName: "workspaceInstructionsHelp"') :][:700]
    assert "user-authored" in help_text and "32,000 characters" in help_text
    assert "AGENTS.md" in help_text and "cannot grant" in help_text
    runner = source[source.index('objectName: "runnerSettings"') :][:2500]
    for field in ("runnerTarget", "runnerCpus", "runnerMemory", "runnerPids", "gitDepth"):
        assert f"id: {field}" in runner
    assert "backend.saveRuntimeSettings(" in source
    assert 'objectName: "stagedSessions"' in source and "backend.removeStagedSession(" in source
    assert 'objectName: "stagingConflicts"' in source
    assert 'backend.resolveConflict(modelData.path, "keep_staged")' in source
    assert 'backend.resolveConflict(modelData.path, "take_host")' in source


def test_composer_shows_the_run_meter_and_steers_active_runs() -> None:
    """Plan 0.8: the meter while a run is active, and Steer (also Ctrl+Enter)."""
    source = _qml("Composer.qml")
    meter = source[source.index('objectName: "runMeter"') :][:900]
    assert "backend.runMeter.text" in meter and "backend.runMeter.contextFraction" in meter
    assert "root.sendRequested(prompt.text, true)" in source
    assert "Boolean(event.modifiers & Qt.ControlModifier)" in source
    picker = _qml("Main.qml")
    assert "modelData.priceText" in picker and "modelData.warnings" in picker
    assert '"/M est."' in picker and "95% input, 5% output, 95% cache hits" in picker


def test_the_thinking_control_offers_the_five_levels_from_the_backend() -> None:
    """Model controls 3.3: the combo reads backend.reasoningLevels and never offers minimal."""
    source = _qml("Main.qml")
    combo = source[source.index("id: reasoningCombo") :][:1200]
    assert "model: backend.reasoningLevels" in combo
    assert 'valueRole: "value"' in combo
    assert "backend.setReasoningEffort(currentValue)" in combo
    assert "backend.reasoningSupported" in combo
    assert "minimal" not in combo
    assert '"minimal"' not in source


def test_settings_offer_the_context_compaction_threshold() -> None:
    """MODEL_CONTROLS_PLAN 1.3: the Conversation tab sets the compaction share with an
    Automatic option and saves it through the backend, which names the resolved mark."""
    source = _qml("Main.qml")
    form = source[source.index('objectName: "compactionShare"') :][:900]
    assert "from: 10" in form and "to: 60" in form
    assert 'objectName: "compactionAutomatic"' in source
    assert "backend.saveContextCompactionShare(" in source
    assert "backend.contextThreshold" in source
    assert 'objectName: "compactionHelp"' in source
