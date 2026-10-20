from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    session_token: str
    journal_path: Path
    container_openrouter_key: str | None = None
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "[::1]")
    max_body_bytes: int = 2_500_000
    catalog_ttl_seconds: int = 86_400
    max_events_per_run: int = 20_000
    max_journal_runs: int = 500
    requests_per_minute: int = 240
    chat_requests_per_minute: int = 30
    executor_socket: Path | None = None
    executor_token: str | None = None
    workspace_id: str | None = None

    def __post_init__(self) -> None:
        if len(self.session_token.encode("utf-8")) < 32:
            raise ValueError("Gateway session token must contain at least 256 bits")
        if (
            self.max_body_bytes < 1
            or self.max_events_per_run < 100
            or self.max_journal_runs < 10
            or self.requests_per_minute < 10
            or self.chat_requests_per_minute < 1
        ):
            raise ValueError("Gateway limits must be positive")

    @classmethod
    def from_environment(cls) -> GatewayConfig:
        # The plain token variable is a test/development escape hatch; Compose uses a secret.
        token = _secret_file("LOCAL_CHAT_GATEWAY_TOKEN_FILE", "/run/secrets/gateway_token")
        return cls(
            session_token=token or os.environ.get("LOCAL_CHAT_GATEWAY_TOKEN", "").strip(),
            journal_path=_path("LOCAL_CHAT_JOURNAL_PATH", "/data/gateway.sqlite3"),
            container_openrouter_key=_secret_file(
                "LOCAL_CHAT_OPENROUTER_KEY_FILE", "/run/secrets/openrouter_key"
            ),
            executor_socket=_path("LOCAL_CHAT_EXECUTOR_SOCKET", "/run/ipc/executor.sock"),
            executor_token=_secret_file(
                "LOCAL_CHAT_EXECUTOR_TOKEN_FILE", "/run/secrets/executor_token"
            ),
            workspace_id=os.environ.get("LOCAL_CHAT_WORKSPACE_ID", "").strip() or None,
        )


def _path(variable: str, default: str) -> Path:
    return Path(os.environ.get(variable, default))


def _secret_file(variable: str, default: str) -> str | None:
    return _read_secret(_path(variable, default))


def _read_secret(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    return value or None
