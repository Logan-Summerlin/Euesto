from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtWidgets import QApplication

from src.qml_backend import DesktopBridge
from shared.requests import DEFAULT_INVESTIGATION_MODEL


def resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative


def main() -> int:
    # Fusion gives Qt Quick Controls a persistent, mouse-accessible scrollbar in
    # combo-box popups instead of the easy-to-miss overlay treatment used by Basic.
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
