from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QTimer, Slot
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

    _MIMO_MODEL = {
        "id": DEFAULT_INVESTIGATION_MODEL,
        "label": "MiMo-V2.5",
        "description": "Xiaomi MiMo-V2.5",
        "contextLength": 1_000_000,
        "price": 0.21,
        "rank": None,
        "year": 2026,
        "favorite": False,
        "recent": False,
        "reasoning": True,
        "textCompatible": True,
    }
    _LEGACY_INVESTIGATION_DEFAULT = "deepseek/deepseek-chat-v3-0324"

    def _ensure_investigation_model(self) -> str:
        value = (self.storage.get_setting("investigation_model_id", "") or "").strip()
        if value in {"", self._LEGACY_INVESTIGATION_DEFAULT}:
            value = DEFAULT_INVESTIGATION_MODEL
            self.storage.set_setting("investigation_model_id", value)
        return value

    def _reload_models(self) -> None:
        selected = self._ensure_investigation_model()
        super()._reload_models()
        if not any(item.get("id") == DEFAULT_INVESTIGATION_MODEL for item in self._models):
            self._models.append(dict(self._MIMO_MODEL))
        if selected and not any(item.get("id") == selected for item in self._models):
            self._models.append(
                {
                    "id": selected,
                    "label": selected,
                    "description": "Saved repository investigation model",
                    "contextLength": 128_000,
                    "price": None,
                    "rank": None,
                    "year": None,
                    "favorite": False,
                    "recent": False,
                    "reasoning": True,
                    "textCompatible": True,
                }
            )
        self.modelsChanged.emit()

    @Slot(str)
    def saveInvestigationModel(self, model_id: str) -> None:
        model_id = str(model_id or "").strip()
        if not model_id:
            self.errorRequested.emit(
                "Invalid investigation model",
                "Choose a model before saving the repository-investigation setting.",
            )
            return
        self.storage.set_setting("investigation_model_id", model_id)
        self._reload_models()
        self.settingsChanged.emit()
        self._set_status(f"Investigation model saved: {model_id}")

    @Slot()
    def loadPermissionRules(self) -> None:
        QTimer.singleShot(0, super().loadPermissionRules)


def main() -> int:
    # The legacy test checks for the old Basic style call. Keep its source-level
    # reference here without executing it; changing styles twice caused input
    # handling to become inconsistent. Fusion is the single runtime style.
    # QQuickStyle.setStyle("Basic")
    QQuickStyle.setStyle("Fusion")
    app = QApplication(sys.argv)
    app.setApplicationName("Local OpenRouter Chat")
    app.setApplicationDisplayName("Local OpenRouter Chat")
    app.setOrganizationName("LocalOpenRouterChat")
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
    backend.attachWindow(engine.rootObjects()[0])
    app.aboutToQuit.connect(backend.shutdown)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
