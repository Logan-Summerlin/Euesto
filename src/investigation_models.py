"""Desktop-facing model catalog additions for repository investigation.

Kept outside the Qt adapter so bootstrap code only wires the application and the
model policy has one testable home.
"""

from typing import Any

from shared.requests import DEFAULT_INVESTIGATION_MODEL


LEGACY_INVESTIGATION_DEFAULT = "deepseek/deepseek-chat-v3-0324"
INVESTIGATION_MODEL = {
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


def ensure_investigation_model(storage: Any) -> str:
    value = (storage.get_setting("investigation_model_id", "") or "").strip()
    if value in {"", LEGACY_INVESTIGATION_DEFAULT}:
        value = DEFAULT_INVESTIGATION_MODEL
        storage.set_setting("investigation_model_id", value)
    return value


def saved_investigation_model_entry(model_id: str) -> dict[str, object]:
    return {
        "id": model_id,
        "label": model_id,
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
