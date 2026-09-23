"""Drive the Settings investigation-model control in ``qml/Main.qml`` through the real bridge.

Regression: the Save button referenced an out-of-scope ``currentText``, so every click raised a
QML ReferenceError and the saved model could never change from the default.
"""
from __future__ import annotations

import gc
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")
os.environ.setdefault("PYTHON_KEYRING_BACKEND", "keyring.backends.null.Keyring")

import pytest

try:
    from PySide6.QtCore import QMetaObject, QObject
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuickControls2 import QQuickStyle
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from app import DesktopBridge
except ImportError as exc:
    pytest.skip(f"Qt Quick runtime unavailable: {exc}", allow_module_level=True)

from shared.requests import DEFAULT_INVESTIGATION_MODEL
from src.storage import Storage

pytestmark = pytest.mark.slow

ROOT = Path(__file__).resolve().parents[2]


class SettingsUi:
    """Only the handles a test needs; wrapping every QML child leaves stale PySide wrappers."""

    def __init__(self, bridge: DesktopBridge, storage: Storage, root: QObject) -> None:
        self.bridge = bridge
        self.storage = storage
        self.dialog = root.findChild(QObject, "settingsDialog")
        self.combo = root.findChild(QObject, "investigationModelCombo")
        self.save = root.findChild(QObject, "saveInvestigationModelButton")
        assert self.dialog is not None and self.combo is not None and self.save is not None

    def open(self) -> None:
        QMetaObject.invokeMethod(self.dialog, "openAndLoad")


@pytest.fixture
def settings_ui(tmp_path: Path):
    QQuickStyle.setStyle("Fusion")
    app = QApplication.instance() or QApplication([])
    storage = Storage(tmp_path / "settings.sqlite")
    bridge = DesktopBridge(storage)
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("backend", bridge)
    engine.rootContext().setContextProperty("transcriptModel", bridge.transcriptModel)
    engine.load(str(ROOT / "qml" / "Main.qml"))
    assert engine.rootObjects()
    window = engine.rootObjects()[0]
    ui = SettingsUi(bridge, storage, window)
    ui.open()
    app.processEvents()
    yield ui
    # Release wrappers before their C++ objects go away, then tear down the scene before the
    # bridge its bindings read from.
    ui.dialog = ui.combo = ui.save = None
    gc.collect()
    window.close()
    del window
    engine.deleteLater()
    app.processEvents()
    QTest.qWait(0)
    app.processEvents()
    bridge.shutdown()
    storage.close()


def _pick(combo: QObject, model_id: str) -> None:
    index = [item for item in combo.property("model")].index(model_id)
    combo.setProperty("currentIndex", index)
    combo.activated.emit(index)


def test_save_button_persists_the_picked_model(settings_ui: SettingsUi) -> None:
    bridge, storage, combo, save = settings_ui.bridge, settings_ui.storage, settings_ui.combo, settings_ui.save
    assert combo.property("editText") == DEFAULT_INVESTIGATION_MODEL
    target = next(item["id"] for item in bridge.models if item["id"] != DEFAULT_INVESTIGATION_MODEL)
    _pick(combo, target)
    save.clicked.emit()
    assert storage.get_setting("investigation_model_id") == target
    assert bridge.investigationModel == target


def test_model_reload_keeps_the_pending_and_saved_selection(settings_ui: SettingsUi) -> None:
    bridge, combo, save = settings_ui.bridge, settings_ui.combo, settings_ui.save
    target = next(item["id"] for item in bridge.models if item["id"] != DEFAULT_INVESTIGATION_MODEL)
    _pick(combo, target)
    bridge.reload_models()  # e.g. a catalog refresh while the dialog is open
    assert combo.property("editText") == target
    save.clicked.emit()
    bridge.reload_models()
    settings_ui.open()
    assert combo.property("editText") == target
    assert combo.property("currentText") == target


def test_any_typed_model_id_can_be_saved(settings_ui: SettingsUi) -> None:
    bridge, storage, combo, save = settings_ui.bridge, settings_ui.storage, settings_ui.combo, settings_ui.save
    combo.setProperty("editText", "  vendor/custom-model  ")
    save.clicked.emit()
    assert storage.get_setting("investigation_model_id") == "vendor/custom-model"
    assert any(item["id"] == "vendor/custom-model" for item in bridge.models)
