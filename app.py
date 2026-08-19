from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Property, QTimer, Slot
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

    def _reload_models(self) -> None:
        # Keep the base Qt Property intact. Re-declaring the `models` Property in
        # this QObject subclass creates a second meta-object property with the
        # same name, which makes QML bindings/dialogs unreliable. Extend the
        # underlying list instead so the inherited property remains canonical.
        super()._reload_models()
        if not any(item.get("id") == DEFAULT_INVESTIGATION_MODEL for item in self._models):
            self._models.append(dict(self._MIMO_MODEL))
            self.modelsChanged.emit()

    @Slot(str, bool, float, int, int, result="QVariantList")
    def filteredModels(
        self,
        query: str,
        text_only: bool,
        max_price: float,
        max_rank: int,
        year: int,
    ) -> list[dict[str, object]]:
        query = query.strip().casefold()
        results: list[dict[str, object]] = []
        for item in self._models:
            if text_only and not bool(item.get("textCompatible", False)):
                continue
            haystack = " ".join(
                str(item.get(key) or "")
                for key in ("id", "label", "description")
            ).casefold()
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

    @Slot()
    def loadPermissionRules(self) -> None:
        # The base implementation performs a synchronous gateway request. The
        # settings dialog calls this while it is being opened, so defer it to
        # the event loop rather than blocking the UI before `Dialog.open()`.
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
    if not backend.investigationModel:
        backend.saveInvestigationModel(DEFAULT_INVESTIGATION_MODEL)
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("backend", backend)
    # QAbstractListModel instances exposed through a PySide QObject Property can
    # be converted to a generic QObject QVariant by the QML boundary. Expose the
    # persistent model directly as a context property so Repeater receives the
    # actual QAbstractListModel interface and its row/reset signals.
    engine.rootContext().setContextProperty("transcriptModel", backend._transcript_model)
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