"""Plain helper functions shared by tests (the test folders are not packages)."""
from __future__ import annotations

from pathlib import Path

from executor.config import ExecutorConfig
from src.workspace_broker import workspace_id

TOKEN = "t" * 43


def executor_config(source: Path, **overrides: object) -> ExecutorConfig:
    """An executor for ``source`` (created if missing) with its staging root and socket beside it."""
    source.mkdir(parents=True, exist_ok=True)
    values: dict[str, object] = {
        "source_root": source,
        "work_root": source.parent / "work",
        "socket_path": source.parent / "executor.sock",
        "token": TOKEN,
        "workspace_id": workspace_id(source),
    }
    return ExecutorConfig(**{**values, **overrides})  # type: ignore[arg-type]
