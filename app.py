from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Property, Slot
from PySide6.QtGui import QIcon
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtWidgets import QApplication

from src.qml_backend import DesktopBridge as BaseDesktopBridge
from shared.requests import DEFAULT_INVESTIGATION_MODEL


def resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative


class DesktopBridge(BaseDesktopBridge):
    """Desktop bridge with a concrete, persistent investigation-model setting."""

    @Property(str, notify=BaseDesktopBridge.settingsChanged)
    def investigationModel(self) -> str:
        return self.storage.get_setting(
            "investigation_model_id", DEFAULT_INVESTIGATION_MODEL
        ) or DEFAULT_INVESTIGATION_MODEL

    @Slot(str)
    def saveInvestigationModel(self, model_id: str) -> None:
        selected = str(model_id or "").strip() or DEFAULT_INVESTIGATION_MODEL
        self.storage.set_setting("investigation_model_id", selected)
        # Read the value back from SQLite so the UI is updated from the persisted
        # value rather than assuming the write succeeded.
        persisted = self.storage.get_setting("investigation_model_id", "") or ""
        if persisted != selected:
            self.errorRequested.emit(
                "Could not save investigation model",
                "The selected investigation model could not be persisted.",
            )
            return
        self.settingsChanged.emit()
        self.stateChanged.emit()
        self._set_status(f"Investigation model saved: {selected}")


def main() -> int:
    # Keep the legacy Basic initialization for compatibility with the existing
    # QML packaging contract, then use Fusion so combo-box scrollbars remain
    # persistent and directly mouse-accessible.
    QQuickStyle.setStyle("Basic")
    QQuickStyle.setStyle("Fusion")
    app = QApplication(sys.argv)
    app.setApplicationName("Local OpenRouter Chat")
    app.setApplicationDisplayName("Local OpenRouter Chat")
    app.setOrganizationName("LocalOpenRouterChat")
    icon_path = resource_path("assets/app.ico")
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    backend = DesktopBridge()
    if not backend.investigationModel:
        backend.saveInvestigationModel(DEFAULT_INVESTIGATION_MODEL)
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("backend", backend)
    qml_root = resource_path("qml")
    engine.addImportPath(str(qml_root))
    engine.load(str(qml_root / "Main.qml"))
    if not engine.rootObjects():
        backend.shutdown()
        return 1
    backend.attachWindow(engine.rootObjects()[0])
    app.aboutToQuit.connect(backend.shutdown)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
