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


def test_legacy_investigation_model_migrates_to_mimo(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "settings.sqlite")
    try:
        storage.set_setting("investigation_model_id", "deepseek/deepseek-chat-v3-0324")
        bridge = DesktopBridge(storage)
        assert bridge.investigationModel == DEFAULT_INVESTIGATION_MODEL
        assert storage.get_setting("investigation_model_id") == DEFAULT_INVESTIGATION_MODEL
    finally:
        storage.close()


def test_legacy_default_migrates_only_once(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "settings.sqlite")
    try:
        storage.set_setting("investigation_model_id", "deepseek/deepseek-chat-v3-0324")
        bridge = DesktopBridge(storage)
        assert bridge.investigationModel == DEFAULT_INVESTIGATION_MODEL
        # Choosing the old default explicitly afterwards must stick across reloads and restarts.
        bridge.saveInvestigationModel("deepseek/deepseek-chat-v3-0324")
        bridge.reload_models()
        assert bridge.investigationModel == "deepseek/deepseek-chat-v3-0324"
        assert DesktopBridge(storage).investigationModel == "deepseek/deepseek-chat-v3-0324"
    finally:
        storage.close()


def test_blank_investigation_model_is_rejected(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "settings.sqlite")
    try:
        bridge = DesktopBridge(storage)
        errors = []
        bridge.errorRequested.connect(lambda title, _body: errors.append(title))
        bridge.saveInvestigationModel("   ")
        assert errors == ["Invalid investigation model"]
        assert bridge.investigationModel == DEFAULT_INVESTIGATION_MODEL
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


def test_investigation_models_are_listed_and_filterable_without_catalog_metadata(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "settings.sqlite")
    try:
        storage.set_setting("investigation_model_id", "custom/investigator")
        bridge = DesktopBridge(storage)
        ids = [item["id"] for item in bridge.models]
        assert DEFAULT_INVESTIGATION_MODEL in ids and "custom/investigator" in ids
        filtered = bridge.filteredModels("investigator", True, -1.0, 0, 0)
        assert [item["id"] for item in filtered] == ["custom/investigator"]
    finally:
        storage.close()


def test_empty_investigation_model_is_rejected(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "settings.sqlite")
    try:
        bridge = DesktopBridge(storage)
        errors: list[tuple[str, str]] = []
        bridge.errorRequested.connect(lambda title, body: errors.append((title, body)))
        bridge.saveInvestigationModel("  ")
        assert errors and errors[0][0] == "Invalid investigation model"
        assert storage.get_setting("investigation_model_id") == DEFAULT_INVESTIGATION_MODEL
    finally:
        storage.close()
