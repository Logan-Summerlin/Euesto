from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

import pytest

try:
    from app import DesktopBridge
except ImportError as exc:
    pytest.skip(f"Desktop Qt bridge unavailable: {exc}", allow_module_level=True)

from shared.requests import DEFAULT_INVESTIGATION_MODEL
from src.storage import Storage


def test_investigation_model_setting_round_trips(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "settings.sqlite")
    try:
        assert storage.get_setting("investigation_model_id", "") == ""
        storage.set_setting("investigation_model_id", "openai/gpt-5-mini")
        assert storage.get_setting("investigation_model_id", "") == "openai/gpt-5-mini"
    finally:
        storage.close()


def test_default_investigation_model_is_mimo_v25() -> None:
    assert DEFAULT_INVESTIGATION_MODEL == "xiaomi/mimo-v2.5"


def test_desktop_bridge_does_not_redeclare_investigation_model_property() -> None:
    meta = DesktopBridge.staticMetaObject
    matches = [
        index
        for index in range(meta.propertyOffset(), meta.propertyCount())
        if meta.property(index).name() == "investigationModel"
    ]
    assert matches == []


def test_legacy_investigation_model_migrates_to_mimo(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "settings.sqlite")
    try:
        storage.set_setting("investigation_model_id", "deepseek/deepseek-chat-v3-0324")
        bridge = DesktopBridge(storage)
        assert bridge.investigationModel == DEFAULT_INVESTIGATION_MODEL
        assert storage.get_setting("investigation_model_id") == DEFAULT_INVESTIGATION_MODEL
    finally:
        storage.close()


def test_saved_investigation_model_survives_backend_reload(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "settings.sqlite")
    try:
        bridge = DesktopBridge(storage)
        bridge.saveInvestigationModel("openai/gpt-5-mini")
        assert storage.get_setting("investigation_model_id") == "openai/gpt-5-mini"
        assert bridge.investigationModel == "openai/gpt-5-mini"
        assert any(item["id"] == "openai/gpt-5-mini" for item in bridge.models)

        reloaded = DesktopBridge(storage)
        assert reloaded.investigationModel == "openai/gpt-5-mini"
        assert any(item["id"] == "openai/gpt-5-mini" for item in reloaded.models)
    finally:
        storage.close()
