from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar


@dataclass(frozen=True, slots=True)
class ExecutorConfig:
    """Authoritative executor resource configuration.

    Each limit has four distinct meanings: a caller may request a smaller value,
    this object stores the configured value, a hard implementation ceiling bounds
    the configured value, and ``effective_limit`` computes the value actually
    usable for a particular operation.
    """

    source_root: Path
    work_root: Path
    socket_path: Path
    token: str
    workspace_id: str

    max_read_bytes: int = 1_000_000
    max_write_bytes: int = 1_000_000
    max_edit_target_bytes: int = 2_000_000
    max_edit_result_bytes: int = 2_000_000
    max_bash_output_bytes: int = 1_000_000
    max_bash_stdin_bytes: int = 1_000_000
    max_command_bytes: int = 1_000_000
    max_checkpoint_bytes: int = 256_000_000
    max_staging_bytes: int = 1_500_000_000
    max_staged_files: int = 300_000
    max_command_seconds: int = 300
    max_search_results: int = 500
    max_find_results: int = 500
    max_ls_results: int = 500
    max_grep_scan_bytes: int = 64_000_000

    HARD_CEILINGS: ClassVar[dict[str, int]] = {
        "max_read_bytes": 8_000_000,
        "max_write_bytes": 8_000_000,
        "max_edit_target_bytes": 16_000_000,
        "max_edit_result_bytes": 16_000_000,
        "max_bash_output_bytes": 8_000_000,
        "max_bash_stdin_bytes": 8_000_000,
        "max_command_bytes": 1_000_000,
        "max_checkpoint_bytes": 1_000_000_000,
        "max_staging_bytes": 8_000_000_000,
        "max_staged_files": 1_000_000,
        "max_command_seconds": 900,
        "max_search_results": 5_000,
        "max_find_results": 2_000,
        "max_ls_results": 2_000,
        "max_grep_scan_bytes": 256_000_000,
    }
    _LIMIT_FIELDS: ClassVar[tuple[str, ...]] = tuple(HARD_CEILINGS)
    _ENV_PREFIX: ClassVar[str] = "LOCAL_CHAT_"
    sources: dict[str, str] = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        if len(self.token.encode("utf-8")) < 32:
            raise ValueError("Executor token must contain at least 256 bits")
        if not self.workspace_id:
            raise ValueError("Executor workspace identity is required")
        values = {name: getattr(self, name) for name in self._LIMIT_FIELDS}
        invalid = [name for name, value in values.items() if not isinstance(value, int) or isinstance(value, bool) or value < 1]
        if invalid:
            raise ValueError(f"Executor limits must be positive integers: {', '.join(invalid)}")
        over = [name for name, value in values.items() if value > self.HARD_CEILINGS[name]]
        if over:
            raise ValueError("Configured limits exceed hard ceilings: " + ", ".join(over))
        if self.max_checkpoint_bytes >= self.max_staging_bytes:
            raise ValueError("Checkpoint capacity must be smaller than staging capacity")
        if self.max_checkpoint_bytes + self.max_staging_bytes > self.HARD_CEILINGS["max_staging_bytes"]:
            raise ValueError("Staging content and checkpoint capacity exceed the hard staging ceiling")

    @classmethod
    def from_environment(cls) -> ExecutorConfig:
        token_path = Path(os.environ.get("LOCAL_CHAT_EXECUTOR_TOKEN_FILE", "/run/ipc/executor_token"))
        profile = os.environ.get("LOCAL_CHAT_EXECUTOR_PROFILE", "coding").strip().casefold() or "coding"
        profiles = cls._profiles()
        if profile not in profiles:
            raise ValueError(f"Unknown executor profile: {profile}")
        values = dict(profiles[profile])
        sources = {name: f"profile:{profile}" for name in values}
        for name in cls._LIMIT_FIELDS:
            env_name = cls._ENV_PREFIX + name.upper()
            raw = os.environ.get(env_name)
            if raw is None:
                continue
            try:
                value = int(raw, 10)
            except ValueError as exc:
                raise ValueError(f"{env_name} must be a positive integer") from exc
            values[name] = value
            sources[name] = f"environment:{env_name}"
        return cls(
            source_root=Path(os.environ.get("LOCAL_CHAT_SOURCE_ROOT", "/source")),
            work_root=Path(os.environ.get("LOCAL_CHAT_WORK_ROOT", "/work")),
            socket_path=Path(os.environ.get("LOCAL_CHAT_EXECUTOR_SOCKET", "/run/ipc/executor.sock")),
            token=token_path.read_text(encoding="utf-8").strip(),
            workspace_id=os.environ.get("LOCAL_CHAT_WORKSPACE_ID", "").strip(),
            sources=sources,
            **values,
        )

    @staticmethod
    def _profiles() -> dict[str, dict[str, int]]:
        base = {
            "max_read_bytes": 1_000_000,
            "max_write_bytes": 1_000_000,
            "max_edit_target_bytes": 2_000_000,
            "max_edit_result_bytes": 2_000_000,
            "max_bash_output_bytes": 1_000_000,
            "max_bash_stdin_bytes": 1_000_000,
            "max_command_bytes": 1_000_000,
            "max_checkpoint_bytes": 256_000_000,
            "max_staging_bytes": 1_500_000_000,
            "max_staged_files": 300_000,
            "max_command_seconds": 300,
            "max_search_results": 500,
            "max_find_results": 500,
            "max_ls_results": 500,
            "max_grep_scan_bytes": 64_000_000,
        }
        return {
            "small": {**base, "max_read_bytes": 256_000, "max_write_bytes": 256_000, "max_edit_target_bytes": 512_000, "max_edit_result_bytes": 512_000, "max_bash_output_bytes": 256_000, "max_bash_stdin_bytes": 256_000, "max_command_bytes": 256_000, "max_staging_bytes": 512_000_000, "max_checkpoint_bytes": 64_000_000, "max_search_results": 250, "max_find_results": 250, "max_ls_results": 250, "max_grep_scan_bytes": 16_000_000},
            "coding": base,
            "large-workspace": {**base, "max_read_bytes": 2_000_000, "max_write_bytes": 2_000_000, "max_edit_target_bytes": 4_000_000, "max_edit_result_bytes": 4_000_000, "max_bash_output_bytes": 2_000_000, "max_bash_stdin_bytes": 2_000_000, "max_staging_bytes": 6_000_000_000, "max_checkpoint_bytes": 1_000_000_000, "max_staged_files": 750_000, "max_search_results": 1_000, "max_find_results": 1_000, "max_ls_results": 1_000, "max_grep_scan_bytes": 128_000_000},
        }

    def effective_limit(self, name: str, requested: int | None = None) -> int:
        if name not in self._LIMIT_FIELDS:
            raise KeyError(f"Unknown executor limit: {name}")
        configured = getattr(self, name)
        if requested is None:
            return configured
        if not isinstance(requested, int) or isinstance(requested, bool) or requested < 1:
            raise ValueError(f"{name} requested limit must be a positive integer")
        return min(requested, configured, self.HARD_CEILINGS[name])

    def limit_status(self, name: str, requested: int | None = None) -> dict[str, object]:
        if name not in self._LIMIT_FIELDS:
            raise KeyError(f"Unknown executor limit: {name}")
        configured = getattr(self, name)
        return {"requested": requested, "configured": configured, "hard_ceiling": self.HARD_CEILINGS[name], "effective": self.effective_limit(name, requested), "source": self.sources.get(name, "constructor")}

    def limits_status(self) -> dict[str, dict[str, object]]:
        return {name: self.limit_status(name) for name in self._LIMIT_FIELDS}
