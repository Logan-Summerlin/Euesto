from __future__ import annotations

from pathlib import Path

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
