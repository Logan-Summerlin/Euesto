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
    assert app_source.index('QQuickStyle.setStyle("Fusion")') < app_source.index("QApplication(sys.argv)")
    assert all((ROOT / "qml" / name).is_file() for name in QML_COMPONENTS)
    assert 'project_root / "qml"' in spec_source


def test_settings_load_optional_values_without_assigning_undefined() -> None:
    source = _qml("Main.qml")
    assert "function optionalText(value)" in source
    assert "temperature.text = optionalText(options.temperature)" in source
    assert "topP.text = optionalText(options.top_p)" in source
    assert 'Shortcut { sequence: "Ctrl+Comma"; onActivated: settingsDialog.openAndLoad() }' in source
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
