import asyncio
from pathlib import Path

import pytest

from executor.tools.bash import bash, cancel, events


async def run_bash(root: Path, arguments: dict, *, max_seconds: int = 10, max_output: int = 64_000):
    return await bash(
        "test-request",
        root,
        arguments,
        max_seconds=max_seconds,
        max_output=max_output,
        max_checkpoint_files=10_000,
        max_checkpoint_bytes=20_000_000,
    )


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
    output, data = asyncio.run(
        run_bash(tmp_path, {"command": "read value; printf '%s:%s\\n' \"$DEBUG\" \"$value\"", "env": {"DEBUG": "1"}, "stdin": "input\n"})
    )
    assert output == "1:input\n"
    assert data["stdin_bytes"] == len("input\n".encode())


def test_bash_rejects_workspace_traversal_before_starting_shell(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Traversal"):
        asyncio.run(run_bash(tmp_path, {"command": "pwd", "working_directory": "../../"}))


def test_bash_enforces_timeout(tmp_path: Path) -> None:
    with pytest.raises(TimeoutError, match="approved timeout"):
        asyncio.run(run_bash(tmp_path, {"command": "sleep 10", "timeout_seconds": 1}, max_seconds=2))


def test_bash_cancellation_terminates_process_group(tmp_path: Path) -> None:
    async def scenario() -> None:
        task = asyncio.create_task(run_bash(tmp_path, {"command": "sleep 30"}))
        await asyncio.sleep(0.1)
        assert await cancel("test-request") is True
        result = await asyncio.wait_for(task, timeout=3)
        assert result[1]["cancelled"] is True
        assert result[1]["exit_code"] != 0

    asyncio.run(scenario())


def test_bash_bounds_output_and_events(tmp_path: Path) -> None:
    output, data = asyncio.run(run_bash(tmp_path, {"command": "python -c 'print(\"x\" * 100000)'"}, max_output=1000))
    assert len(output.encode()) <= 1000
    assert data["truncated"] is True
    event_data = events("test-request")
    assert event_data["events"]
    assert event_data["next_cursor"] >= 1


def test_bash_allows_subprocess_spawning_inside_sandbox(tmp_path: Path) -> None:
    output, data = asyncio.run(run_bash(tmp_path, {"command": "python -c 'import subprocess; subprocess.run([\"echo\", \"child\"], check=True)'"}))
    assert "child" in output
    assert data["exit_code"] == 0
