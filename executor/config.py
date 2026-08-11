from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ExecutorConfig:
    source_root: Path
    work_root: Path
    socket_path: Path
    token: str
    workspace_id: str
    max_file_bytes: int = 1_000_000
    max_output_bytes: int = 256_000
    max_files: int = 300_000
    max_total_bytes: int = 2_000_000_000
    max_command_seconds: int = 300

    def __post_init__(self) -> None:
        if len(self.token.encode()) < 32:
            raise ValueError("Executor token must contain at least 256 bits")
        if not self.workspace_id or any(value < 1 for value in (self.max_file_bytes, self.max_output_bytes, self.max_files, self.max_total_bytes)):
            raise ValueError("Executor identity and positive limits are required")

    @classmethod
    def from_environment(cls) -> ExecutorConfig:
        token_path = Path(os.environ.get("LOCAL_CHAT_EXECUTOR_TOKEN_FILE", "/run/ipc/executor_token"))
        return cls(
            source_root=Path(os.environ.get("LOCAL_CHAT_SOURCE_ROOT", "/source")),
            work_root=Path(os.environ.get("LOCAL_CHAT_WORK_ROOT", "/work")),
            socket_path=Path(os.environ.get("LOCAL_CHAT_EXECUTOR_SOCKET", "/run/ipc/executor.sock")),
            token=token_path.read_text(encoding="utf-8").strip(),
            workspace_id=os.environ.get("LOCAL_CHAT_WORKSPACE_ID", "").strip(),
        )
