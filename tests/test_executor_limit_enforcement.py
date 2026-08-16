from __future__ import annotations

import asyncio

import pytest

from executor.tools.bash import BashRunner


def test_bash_command_limit_is_enforced_before_execution(tmp_path) -> None:
    root = tmp_path / "work"
    root.mkdir()
    runner = BashRunner()
    with pytest.raises(ValueError, match="configured limit"):
        asyncio.run(
            runner.run(
                "command-limit",
                root,
                {"command": "x" * 101, "working_directory": "."},
                max_seconds=10,
                max_output=1_000,
                max_command_bytes=100,
            )
        )


def test_bash_stdin_limit_is_enforced_before_execution(tmp_path) -> None:
    root = tmp_path / "work"
    root.mkdir()
    runner = BashRunner()
    with pytest.raises(ValueError, match="configured limit"):
        asyncio.run(
            runner.run(
                "stdin-limit",
                root,
                {"command": "cat", "working_directory": ".", "stdin": "x" * 101},
                max_seconds=10,
                max_output=1_000,
                max_stdin_bytes=100,
            )
        )
