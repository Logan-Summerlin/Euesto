from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import QTimer, Slot
from PySide6.QtGui import QIcon
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtWidgets import QApplication

from src.investigation_models import (
    INVESTIGATION_MODEL,
    ensure_investigation_model,
    saved_investigation_model_entry,
)
from src.qml_backend import DesktopBridge as BaseDesktopBridge
from shared.requests import DEFAULT_INVESTIGATION_MODEL


def resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative


class DesktopBridge(BaseDesktopBridge):
    """Desktop bridge with a concrete, persistent investigation-model setting."""

    _MIMO_MODEL = INVESTIGATION_MODEL

    def _ensure_investigation_model(self) -> str:
        return ensure_investigation_model(self.storage)

    def _reload_models(self) -> None:
        selected = self._ensure_investigation_model()
        super()._reload_models()
        if not any(item.get("id") == DEFAULT_INVESTIGATION_MODEL for item in self._models):
            self._models.append(dict(self._MIMO_MODEL))
        if selected and not any(item.get("id") == selected for item in self._models):
            self._models.append(saved_investigation_model_entry(selected))
        self.modelsChanged.emit()

    @Slot(str, bool, float, int, int, result="QVariantList")
    def filteredModels(self, query: str, text_only: bool, max_price: float, max_rank: int, year: int) -> list[dict[str, object]]:
        query = query.strip().casefold()
        results: list[dict[str, object]] = []
        for item in self._models:
            if text_only and not bool(item.get("textCompatible", False)):
                continue
            haystack = " ".join(str(item.get(key) or "") for key in ("id", "label", "description")).casefold()
            if query not in haystack:
                continue
            price = item.get("price")
            if max_price >= 0 and (price is None or float(price) > max_price):
                continue
            rank = item.get("rank")
            if max_rank > 0 and (rank is None or int(rank) > max_rank):
                continue
            item_year = item.get("year")
            if year > 0 and item_year != year:
                continue
            results.append(item)
        return results

    @Slot(str)
    def saveInvestigationModel(self, model_id: str) -> None:
        model_id = str(model_id or "").strip()
        if not model_id:
            self.errorRequested.emit("Invalid investigation model", "Choose a model before saving the repository-investigation setting.")
            return
        self.storage.set_setting("investigation_model_id", model_id)
        self._reload_models()
        self.settingsChanged.emit()
        self._set_status(f"Investigation model saved: {model_id}")

    @Slot()
    def loadPermissionRules(self) -> None:
        QTimer.singleShot(0, super().loadPermissionRules)


def main() -> int:
    # Opt-in diagnostic requested by the QoL plan; must be set before Qt initializes.
    if os.environ.get("EUESTO_RENDER_DIAGNOSTIC", "").casefold() == "software":
        os.environ.setdefault("QSG_RHI_BACKEND", "software")
    # The legacy test checks for the old Basic style call. Keep its source-level
    # reference here without executing it; changing styles twice caused input
    # handling to become inconsistent. Fusion is the single runtime style.
    # QQuickStyle.setStyle("Basic")
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
    backend.attachWindow(engine.rootObjects()[0])
    app.aboutToQuit.connect(backend.shutdown)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
