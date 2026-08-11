from __future__ import annotations

import os
import sys
from pathlib import Path

import keyring
from keyring.errors import KeyringError

APP_NAME = "LocalOpenRouterChat"
KEYRING_SERVICE = "Local OpenRouter Chat"
KEYRING_USERNAME = "openrouter-api-key"
GATEWAY_KEYRING_USERNAME = "gateway-session-token"


def app_data_dir() -> Path:
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    path = root / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def database_path() -> Path:
    return app_data_dir() / "chat.sqlite3"


def get_api_key() -> str | None:
    try:
        value = keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
    except KeyringError:
        return None
    return value.strip() if value else None


def save_api_key(api_key: str) -> None:
    api_key = api_key.strip()
    if not api_key:
        raise ValueError("API key cannot be empty")
    keyring.set_password(KEYRING_SERVICE, KEYRING_USERNAME, api_key)


def delete_api_key() -> None:
    try:
        keyring.delete_password(KEYRING_SERVICE, KEYRING_USERNAME)
    except (KeyringError, keyring.errors.PasswordDeleteError):
        pass


def get_gateway_token() -> str | None:
    try:
        value = keyring.get_password(KEYRING_SERVICE, GATEWAY_KEYRING_USERNAME)
    except KeyringError:
        return None
    return value.strip() if value else None


def get_gateway_session_token() -> str | None:
    path = app_data_dir() / "gateway-session" / "gateway_token.txt"
    try:
        if path.is_symlink() or path.stat().st_size > 4_096:
            return None
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    return value if len(value.encode("utf-8")) >= 32 else None


def save_gateway_token(token: str) -> None:
    value = token.strip()
    if len(value.encode("utf-8")) < 32:
        raise ValueError("Gateway token must contain at least 256 bits")
    keyring.set_password(KEYRING_SERVICE, GATEWAY_KEYRING_USERNAME, value)


def delete_gateway_token() -> None:
    try:
        keyring.delete_password(KEYRING_SERVICE, GATEWAY_KEYRING_USERNAME)
    except (KeyringError, keyring.errors.PasswordDeleteError):
        pass
