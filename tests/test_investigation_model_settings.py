from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QCoreApplication

from app import DesktopBridge
from src.storage import Storage
from shared.requests import DEFAULT_INVESTIGATION_MODEL


def make_bridge(tmp_path: Path) -> tuple[Storage, DesktopBridge]:
    QCoreApplication.instance() or QCoreApplication([])
    storage = Storage(tmp_path / "chat.sqlite3")
    return storage, DesktopBridge(storage)


def test_legacy_investigation_model_migrates_to_mimo(tmp_path: Path) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    storage = Storage(tmp_path / "chat.sqlite3")
    storage.set_setting("investigation_model_id", "deepseek/deepseek-chat-v3-0324")

    bridge = DesktopBridge(storage)

    assert bridge.investigationModel == DEFAULT_INVESTIGATION_MODEL
    assert storage.get_setting("investigation_model_id") == DEFAULT_INVESTIGATION_MODEL


def test_saved_investigation_model_survives_backend_reload(tmp_path: Path) -> None:
    storage, bridge = make_bridge(tmp_path)

    bridge.saveInvestigationModel("xiaomi/mimo-v2.5")

    assert storage.get_setting("investigation_model_id") == "xiaomi/mimo-v2.5"
    assert bridge.investigationModel == "xiaomi/mimo-v2.5"
    assert any(item["id"] == "xiaomi/mimo-v2.5" for item in bridge.models)

    reloaded = DesktopBridge(storage)
    assert reloaded.investigationModel == "xiaomi/mimo-v2.5"
    assert any(item["id"] == "xiaomi/mimo-v2.5" for item in reloaded.models)
