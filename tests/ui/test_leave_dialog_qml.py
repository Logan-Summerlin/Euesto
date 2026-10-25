"""Drive the close/switch-workspace dialog in ``qml/Main.qml`` through the real bridge (0.6)."""

from __future__ import annotations

import gc
from pathlib import Path

import pytest

try:
    from PySide6.QtCore import Q_ARG, QMetaObject, QObject
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuickControls2 import QQuickStyle
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from app import DesktopBridge
except ImportError as exc:
    pytest.skip(f"Qt Quick runtime unavailable: {exc}", allow_module_level=True)

from src.storage import Storage

pytestmark = pytest.mark.slow

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def scene(tmp_path: Path):
    QQuickStyle.setStyle("Fusion")
    app = QApplication.instance() or QApplication([])
    storage = Storage(tmp_path / "leave.sqlite")
    bridge = DesktopBridge(storage)
    choices: list[str] = []
    bridge.staging.resolve_leave = choices.append  # type: ignore[method-assign]
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("backend", bridge)
    engine.rootContext().setContextProperty("transcriptModel", bridge.transcriptModel)
    engine.load(str(ROOT / "qml" / "Main.qml"))
    assert engine.rootObjects()
    window = engine.rootObjects()[0]
    yield app, bridge, window, choices
    gc.collect()
    window.close()
    del window
    engine.deleteLater()
    app.processEvents()
    QTest.qWait(0)
    app.processEvents()
    bridge.shutdown()
    storage.close()


def test_leave_dialog_opens_and_reports_the_chosen_action(scene) -> None:
    app, bridge, window, choices = scene
    dialog = window.findChild(QObject, "leaveDialog")
    assert dialog is not None
    bridge.leaveRequested.emit("Close with unpublished staged changes?", "Staging has changes.")
    app.processEvents()
    assert dialog.property("opened") or dialog.property("visible")
    assert dialog.property("title") == "Close with unpublished staged changes?"

    QMetaObject.invokeMethod(dialog, "choose", Q_ARG("QVariant", "keep"))
    QTest.qWait(50)
    app.processEvents()
    assert choices == ["keep"]
    assert not dialog.property("visible")


def test_dismissing_the_leave_dialog_cancels(scene) -> None:
    app, bridge, window, choices = scene
    dialog = window.findChild(QObject, "leaveDialog")
    bridge.leaveRequested.emit("Switch workspace with unpublished staged changes?", "…")
    app.processEvents()
    QMetaObject.invokeMethod(dialog, "close")
    QTest.qWait(50)
    app.processEvents()
    assert choices == ["cancel"]
