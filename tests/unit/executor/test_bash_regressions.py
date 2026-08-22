import asyncio
from pathlib import Path

import pytest

from executor.tools.bash import bash, cancel, events


async def run_bash(root: Path, arguments: dict, **limits: int):
    return await bash(
        "regression-request",
        root,
        arguments,
        max_seconds=limits.get("max_seconds", 10),
        max_output=limits.get("max_output", 64_000),
        max_checkpoint_files=10_000,
        max_checkpoint_bytes=20_000_000,
    )


def test_bash_rejects_workspace_traversal(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        asyncio.run(run_bash(tmp_path, {"command": "pwd", "working_directory": ".."}))


def test_bash_rolls_back_changes_on_timeout(tmp_path: Path) -> None:
    with pytest.raises(TimeoutError):
        asyncio.run(run_bash(tmp_path, {"command": "touch transient.txt; sleep 2"}, max_seconds=1))
    assert not (tmp_path / "transient.txt").exists()


def test_bash_rejects_restricted_environment(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        asyncio.run(run_bash(tmp_path, {"command": "true", "env": {"PATH": "/tmp"}}))


def test_bash_truncates_large_output(tmp_path: Path) -> None:
    output, data = asyncio.run(run_bash(tmp_path, {"command": "yes x | head -c 10000"}, max_output=100))
    assert data["truncated"]
    assert "output truncated" in output


def test_bash_event_cursor_is_bounded(tmp_path: Path) -> None:
    asyncio.run(run_bash(tmp_path, {"command": "printf 'event\\n'"}))
    result = events("regression-request", 0)
    assert result["next_cursor"] >= 1
    assert result["events"]


def test_cancel_unknown_request_is_safe() -> None:
    assert asyncio.run(cancel("missing-request")) is False
