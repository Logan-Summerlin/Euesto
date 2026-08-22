import asyncio
import importlib
from pathlib import Path

import pytest

from executor.tools.bash import MAX_COMMAND_SECONDS, MAX_EVENT_BYTES, MAX_EVENT_COUNT, bash, cancel, events

# executor.tools/__init__.py does `from .bash import bash`, which shadows the
# `bash` submodule attribute on the `executor.tools` package with the `bash`
# function. Use importlib to reach the actual submodule object needed below to
# monkeypatch its `asyncio` reference.
bash_tool = importlib.import_module("executor.tools.bash")


async def run_bash(root: Path, arguments: dict, *, request_id: str = "test-request", max_seconds: int = 10, max_output: int = 64_000):
    return await bash(request_id, root, arguments, max_seconds=max_seconds, max_output=max_output, max_checkpoint_files=10_000, max_checkpoint_bytes=20_000_000)


def test_bash_supports_shell_syntax(tmp_path: Path) -> None:
    output, data = asyncio.run(run_bash(tmp_path, {"command": "printf 'a\\nb\\n' | tail -1"}))
    assert output == "b\n"
    assert data["exit_code"] == 0
    assert data["checkpoint_id"]


def test_bash_supports_redirects_substitution_loops_and_multiline(tmp_path: Path) -> None:
    command = """set -e
printf '%s\\n' one two > values.txt
for f in $(cat values.txt); do echo "$f"; done
"""
    output, data = asyncio.run(run_bash(tmp_path, {"command": command}))
    assert output == "one\ntwo\n"
    assert (tmp_path / "values.txt").read_text(encoding="utf-8") == "one\ntwo\n"
    assert data["exit_code"] == 0


def test_bash_supports_environment_and_stdin(tmp_path: Path) -> None:
    output, data = asyncio.run(run_bash(tmp_path, {"command": "read value; printf '%s:%s\\n' \"$DEBUG\" \"$value\"", "env": {"DEBUG": "1"}, "stdin": "input\n"}))