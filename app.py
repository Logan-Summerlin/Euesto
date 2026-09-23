from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtWidgets import QApplication

from src.qml_backend import DesktopBridge


def resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative


def main() -> int:
    # Opt-in software-rendering diagnostic; must be set before Qt initializes.
    if os.environ.get("EUESTO_RENDER_DIAGNOSTIC", "").casefold() == "software":
        os.environ.setdefault("QSG_RHI_BACKEND", "software")
    # Set the style exactly once, before the application exists; switching styles
    # after startup made input handling inconsistent.
    QQuickStyle.setStyle("Fusion")
    app = QApplication(sys.argv)
    app.setApplicationName("Euesto")
    app.setApplicationDisplayName("Euesto")
    app.setOrganizationName("Euesto")
    icon_path = resource_path("assets/app.ico")
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    backend = DesktopBridge()
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("backend", backend)
    engine.rootContext().setContextProperty("transcriptModel", backend.transcriptModel)
    qml_root = resource_path("qml")
    engine.addImportPath(str(qml_root))
    engine.load(str(qml_root / "Main.qml"))
    if not engine.rootObjects():
        backend.shutdown()
        return 1
    app.aboutToQuit.connect(backend.shutdown)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
