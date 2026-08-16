import asyncio
from pathlib import Path

import pytest

from executor.tools.bash import MAX_COMMAND_SECONDS, MAX_EVENT_BYTES, MAX_EVENT_COUNT, bash, cancel, events


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
    assert output == "1:input\n"
    assert data["stdin_bytes"] == len("input\n".encode())


def test_bash_rejects_workspace_traversal_before_starting_shell(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Traversal"):
        asyncio.run(run_bash(tmp_path, {"command": "pwd", "working_directory": "../../"}))


def test_bash_enforces_separate_command_and_stdin_limits(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="command exceeds"):
        asyncio.run(run_bash(tmp_path, {"command": "x" * 1_000_001}))
    with pytest.raises(ValueError, match="stdin exceeds"):
        asyncio.run(run_bash(tmp_path, {"command": "true", "stdin": "x" * 1_000_001}))


def test_bash_enforces_timeout_and_rolls_back(tmp_path: Path) -> None:
    with pytest.raises(TimeoutError, match="approved timeout"):
        asyncio.run(run_bash(tmp_path, {"command": "echo changed > timeout.txt; sleep 10", "timeout_seconds": 1}, max_seconds=2))
    assert not (tmp_path / "timeout.txt").exists()


def test_bash_cancellation_terminates_process_group_and_rolls_back(tmp_path: Path) -> None:
    async def scenario() -> dict:
        task = asyncio.create_task(run_bash(tmp_path, {"command": "echo changed > cancel.txt; sleep 30"}))
        await asyncio.sleep(0.1)
        assert await cancel("test-request") is True
        return (await asyncio.wait_for(task, timeout=3))[1]

    data = asyncio.run(scenario())
    assert data["cancelled"] is True
    assert data["rolled_back"] is True
    assert data["exit_code"] != 0
    assert not (tmp_path / "cancel.txt").exists()


def test_bash_rolls_back_nonzero_exit(tmp_path: Path) -> None:
    output, data = asyncio.run(run_bash(tmp_path, {"command": "echo changed > failed.txt; echo error >&2; exit 7"}))
    assert data["exit_code"] == 7
    assert data["rolled_back"] is True
    assert data["stderr_bytes"] > 0
    assert not (tmp_path / "failed.txt").exists()
    assert "error" in output


def test_bash_large_stdout_retains_bounded_head_and_tail(tmp_path: Path) -> None:
    output, data = asyncio.run(run_bash(tmp_path, {"command": "python -c 'print(\"A\" * 1200000); print(\"TAIL-MARKER\")'"}, max_output=2_000_000))
    assert data["stdout_bytes"] > 1_000_000
    assert data["stdout_truncated"] is True
    assert data["retained_output_bytes"] <= 512_100
    assert "TAIL-MARKER" in output
    assert data["truncated"] is True


def test_bash_large_stderr_and_mixed_streams_are_accounted_separately(tmp_path: Path) -> None:
    command = "python -c 'import sys; print(\"OUT\" * 300000); print(\"ERR\" * 300000, file=sys.stderr)'"
    output, data = asyncio.run(run_bash(tmp_path, {"command": command}, max_output=100_000))
    assert data["stdout_bytes"] > 1_000_000
    assert data["stderr_bytes"] > 1_000_000
    assert data["stdout_truncated"] is True
    assert data["stderr_truncated"] is True
    assert data["truncated"] is True
    assert data["retained_output_bytes"] <= 1_000_200
    assert data["model_output_bytes"] <= 100_000
    assert "stdout:" in output and "stderr:" in output


def test_bash_event_retention_and_cursors_are_bounded(tmp_path: Path) -> None:
    _, data = asyncio.run(run_bash(tmp_path, {"command": "python -c 'import sys; [sys.stdout.write(\"x\" * 16384) for _ in range(700)]'"}, max_output=1000))
    assert data["exit_code"] == 0
    event_data = events("test-request")
    assert len(event_data["events"]) <= MAX_EVENT_COUNT
    assert sum(len(item["text"].encode()) for item in event_data["events"]) <= MAX_EVENT_BYTES
    assert event_data["next_cursor"] >= event_data["first_cursor"]
    first_page = events("test-request", event_data["first_cursor"] - 1)
    assert first_page["events"]
    assert first_page["truncated"] is False
    old_page = events("test-request", 0)
    assert old_page["truncated"] is True
    assert old_page["next_cursor"] == event_data["next_cursor"]


def test_bash_preserves_restricted_environment(tmp_path: Path) -> None:
    output, data = asyncio.run(run_bash(tmp_path, {"command": "printf '%s\\n' \"$PATH\"; printf '%s\\n' \"${SECRET:-unset}\""}))
    assert data["exit_code"] == 0
    assert output.splitlines()[0] == "/usr/local/bin:/usr/bin:/bin"
    assert output.splitlines()[1] == "unset"
    with pytest.raises(ValueError, match="restricted"):
        asyncio.run(run_bash(tmp_path, {"command": "true", "env": {"LD_PRELOAD": "x"}}))


def test_bash_rejects_interactive_tty_and_preserves_network_isolation_contract(tmp_path: Path) -> None:
    output, data = asyncio.run(run_bash(tmp_path, {"command": "test -t 0 && echo tty || echo no-tty"}))
    assert data["exit_code"] == 0
    assert output == "no-tty\n"
    assert MAX_COMMAND_SECONDS == 900


def test_bash_allows_subprocess_spawning_inside_sandbox(tmp_path: Path) -> None:
    output, data = asyncio.run(run_bash(tmp_path, {"command": "python -c 'import subprocess; subprocess.run([\"echo\", \"child\"], check=True)'"}))
    assert "child" in output
    assert data["exit_code"] == 0
