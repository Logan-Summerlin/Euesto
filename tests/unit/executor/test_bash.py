import asyncio
import importlib
import os
from pathlib import Path

import pytest

from executor.tools.bash import (
    BASE_ENVIRONMENT,
    MAX_COMMAND_SECONDS,
    MAX_RETAINED_OUTPUT_BYTES,
    bash,
    cancel,
)

# executor.tools/__init__.py does `from .bash import bash`, which shadows the
# `bash` submodule attribute on the `executor.tools` package with the `bash`
# function. Use importlib to reach the actual submodule object needed below to
# monkeypatch its `asyncio` reference.
bash_tool = importlib.import_module("executor.tools.bash")

CHECKPOINTS = ".local-chat-checkpoints"


async def run_bash(root: Path, arguments: dict, *, request_id: str = "test-request", max_seconds: int = 10, max_output: int = 64_000):
    return await bash(request_id, root, arguments, max_seconds=max_seconds, max_output=max_output, max_checkpoint_files=10_000, max_checkpoint_bytes=20_000_000)


def run_and_cancel_after_start(root: Path, arguments: dict, monkeypatch: pytest.MonkeyPatch, *, request_id: str = "test-request") -> dict:
    """Start a command, cancel it once its process exists, and return the result data.

    Synchronizes on the process-creation hook rather than sleeping (docs/TESTING.md).
    """

    async def scenario() -> dict:
        started = asyncio.Event()
        create_process = asyncio.create_subprocess_exec

        async def create_and_signal(*args, **kwargs):
            process = await create_process(*args, **kwargs)
            started.set()
            return process

        monkeypatch.setattr(bash_tool.asyncio, "create_subprocess_exec", create_and_signal)
        task = asyncio.create_task(run_bash(root, arguments, request_id=request_id))
        await started.wait()
        assert await cancel(request_id) is True
        return (await asyncio.wait_for(task, timeout=5))[1]

    return asyncio.run(scenario())


# Shell semantics


@pytest.mark.posix
def test_bash_supports_shell_syntax(tmp_path: Path) -> None:
    output, data = asyncio.run(run_bash(tmp_path, {"command": "printf 'a\\nb\\n' | tail -1"}))
    assert output == "b\n"
    assert data["exit_code"] == 0
    assert data["checkpoint_id"]


@pytest.mark.posix
def test_bash_supports_redirects_substitution_loops_and_multiline(tmp_path: Path) -> None:
    command = """set -e
printf '%s\\n' one two > values.txt
for f in $(cat values.txt); do echo "$f"; done
"""
    output, data = asyncio.run(run_bash(tmp_path, {"command": command}))
    assert output == "one\ntwo\n"
    assert (tmp_path / "values.txt").read_text(encoding="utf-8") == "one\ntwo\n"
    assert data["exit_code"] == 0


@pytest.mark.posix
def test_bash_supports_environment_and_stdin(tmp_path: Path) -> None:
    output, data = asyncio.run(run_bash(tmp_path, {"command": "read value; printf '%s:%s\\n' \"$DEBUG\" \"$value\"", "env": {"DEBUG": "1"}, "stdin": "input\n"}))
    assert output == "1:input\n"
    assert data["stdin_bytes"] == len(b"input\n")


@pytest.mark.posix
def test_bash_allows_subprocess_spawning_inside_sandbox(tmp_path: Path) -> None:
    output, data = asyncio.run(run_bash(tmp_path, {"command": "python3 -c 'import subprocess; subprocess.run([\"echo\", \"child\"], check=True)'"}))
    assert "child" in output
    assert data["exit_code"] == 0


# Argument validation happens before a checkpoint is taken or a shell is started


def test_bash_rejects_workspace_traversal_before_starting_shell(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Traversal"):
        asyncio.run(run_bash(tmp_path, {"command": "touch escaped.txt", "working_directory": "../../"}))
    assert not (tmp_path / CHECKPOINTS).exists()
    assert not (tmp_path / "escaped.txt").exists()


def test_bash_enforces_separate_command_and_stdin_limits(tmp_path: Path) -> None:
    # Hard caps apply even when the caller passes no smaller limit.
    with pytest.raises(ValueError, match="command exceeds"):
        asyncio.run(run_bash(tmp_path, {"command": "x" * 1_000_001}))
    with pytest.raises(ValueError, match="stdin exceeds"):
        asyncio.run(run_bash(tmp_path, {"command": "true", "stdin": "x" * 8_000_001}))
    # Configured limits below the caps are enforced the same way.
    limits = {"max_seconds": 10, "max_output": 1_000, "max_checkpoint_bytes": 20_000_000}
    with pytest.raises(ValueError, match="configured limit of 100 bytes"):
        asyncio.run(bash("command-limit", tmp_path, {"command": "x" * 101}, max_command_bytes=100, **limits))
    with pytest.raises(ValueError, match="configured limit of 100 bytes"):
        asyncio.run(bash("stdin-limit", tmp_path, {"command": "cat", "stdin": "x" * 101}, max_stdin_bytes=100, **limits))
    assert not (tmp_path / CHECKPOINTS).exists()


def test_bash_rejects_non_boolean_rollback_on_failure(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="rollback_on_failure must be a boolean"):
        asyncio.run(run_bash(tmp_path, {"command": "touch created.txt", "rollback_on_failure": "false"}))
    assert not (tmp_path / CHECKPOINTS).exists()
    assert not (tmp_path / "created.txt").exists()


# Restricted environment and non-interactivity


@pytest.mark.posix
def test_bash_preserves_restricted_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET", "host-value")
    # The shell starts from the fixed base environment only; a login profile may prepend or
    # append PATH entries (Ubuntu runners add /snap/bin), so only require the base entries.
    assert bash_tool.BashRunner._environment({"DEBUG": "1"}) == {**BASE_ENVIRONMENT, "DEBUG": "1"}
    output, data = asyncio.run(run_bash(tmp_path, {"command": "printf '%s\\n' \"$PATH\"; printf '%s\\n' \"${SECRET:-unset}\"; printf '%s\\n' \"$HOME\""}))
    assert data["exit_code"] == 0
    path, secret, home = output.splitlines()
    assert BASE_ENVIRONMENT["PATH"] in path
    assert secret == "unset"
    assert home == BASE_ENVIRONMENT["HOME"]


@pytest.mark.parametrize("name", ["PATH", "HOME", "LD_PRELOAD", "LD_LIBRARY_PATH", "BASH_ENV", "BASH_ENV_EXTRA"])
def test_bash_refuses_restricted_environment_overrides(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError, match="restricted"):
        asyncio.run(run_bash(tmp_path, {"command": "true", "env": {name: "x"}}))


def test_bash_refuses_malformed_or_oversized_environment(tmp_path: Path) -> None:
    for env in ({"1BAD": "x"}, {"BAD-NAME": "x"}, {"OK": 1}, {"OK": "v" * 16_385}, {f"V{index}": "x" for index in range(65)}, ["NOT", "AN", "OBJECT"]):
        with pytest.raises(ValueError):
            asyncio.run(run_bash(tmp_path, {"command": "true", "env": env}))


@pytest.mark.posix
def test_bash_rejects_interactive_tty_and_preserves_network_isolation_contract(tmp_path: Path) -> None:
    command = "for fd in 0 1 2; do test -t $fd && echo \"tty:$fd\"; done; (: < /dev/tty) 2>/dev/null && echo controlling-tty; echo done"
    output, data = asyncio.run(run_bash(tmp_path, {"command": command}))
    assert data["exit_code"] == 0
    assert output == "done\n"
    assert MAX_COMMAND_SECONDS == 900


@pytest.mark.posix
@pytest.mark.skipif(not hasattr(os, "openpty"), reason="requires POSIX pseudo-terminals")
def test_bash_does_not_inherit_a_terminal_from_the_executor(tmp_path: Path) -> None:
    # Even when the executor process itself has a terminal on stdin, commands see none.
    primary, secondary = os.openpty()
    saved_stdin = os.dup(0)
    try:
        os.dup2(secondary, 0)
        output, data = asyncio.run(run_bash(tmp_path, {"command": "test -t 0 && echo tty || echo no-tty; cat"}))
    finally:
        os.dup2(saved_stdin, 0)
        for descriptor in (saved_stdin, primary, secondary):
            os.close(descriptor)
    assert data["exit_code"] == 0
    assert output == "no-tty\n"
    assert data["stdin_bytes"] == 0


# Output bounds


@pytest.mark.posix
def test_bash_large_stdout_retains_bounded_head_and_tail(tmp_path: Path) -> None:
    output, data = asyncio.run(run_bash(tmp_path, {"command": "python3 -c 'print(\"A\" * 1200000); print(\"TAIL-MARKER\")'"}, max_output=2_000_000))
    assert data["stdout_bytes"] > 1_000_000
    assert data["stdout_truncated"] is True
    assert data["retained_output_bytes"] <= MAX_RETAINED_OUTPUT_BYTES + 100
    assert "output truncated" in output
    assert "TAIL-MARKER" in output
    assert data["truncated"] is True


@pytest.mark.posix
def test_bash_large_stderr_and_mixed_streams_are_accounted_separately(tmp_path: Path) -> None:
    command = "python3 -c 'import sys; print(\"OUT\" * 400000); print(\"ERR\" * 400000, file=sys.stderr)'"
    output, data = asyncio.run(run_bash(tmp_path, {"command": command}, max_output=100_000))
    assert data["stdout_bytes"] > 1_000_000
    assert data["stderr_bytes"] > 1_000_000
    assert data["stdout_truncated"] is True
    assert data["stderr_truncated"] is True
    assert data["truncated"] is True
    assert data["retained_output_bytes"] <= 2 * (MAX_RETAINED_OUTPUT_BYTES + 100)
    assert data["model_output_bytes"] <= 100_000
    assert len(output.encode("utf-8")) <= 100_000
    assert "OUT" in data["stdout"]
    assert "ERR" in data["stderr"]


def test_output_buffer_keeps_head_and_tail_beyond_its_limit() -> None:
    buffer = bash_tool._OutputBuffer(8)
    buffer.append(b"abcdefghijk")
    assert buffer.truncated and buffer.total == 11
    assert b"output truncated" in buffer.bytes()


@pytest.mark.posix
def test_bash_small_output_is_not_truncated(tmp_path: Path) -> None:
    output, data = asyncio.run(run_bash(tmp_path, {"command": "echo out; echo err >&2"}))
    assert output == "out\n\nerr\n"
    assert data["truncated"] is False
    assert data["stdout_truncated"] is False and data["stderr_truncated"] is False


# Timeout, cancellation, and rollback


def test_cancelling_an_unknown_request_records_nothing() -> None:
    assert asyncio.run(cancel("not-running")) is False
    assert bash_tool._runner._cancelled == set()



@pytest.mark.posix
def test_bash_enforces_timeout_and_rolls_back(tmp_path: Path) -> None:
    with pytest.raises(TimeoutError, match="approved timeout"):
        asyncio.run(run_bash(tmp_path, {"command": "echo changed > timeout.txt; sleep 10", "timeout_seconds": 1}, max_seconds=2))
    assert not (tmp_path / "timeout.txt").exists()


@pytest.mark.posix
def test_bash_cancellation_terminates_process_group_and_rolls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = run_and_cancel_after_start(tmp_path, {"command": "echo changed > cancel.txt; sleep 30"}, monkeypatch)
    assert data["cancelled"] is True
    assert data["rolled_back"] is True
    assert data["rollback_reason"] == "cancelled"
    assert data["exit_code"] != 0
    assert not (tmp_path / "cancel.txt").exists()


def test_cancel_unknown_request_is_safe() -> None:
    assert asyncio.run(cancel("missing-request")) is False


@pytest.mark.posix
def test_bash_success_keeps_changes_and_reports_no_rollback(tmp_path: Path) -> None:
    _, data = asyncio.run(run_bash(tmp_path, {"command": "echo kept > kept.txt"}))
    assert data["exit_code"] == 0
    assert data["rolled_back"] is False
    assert data["rollback_reason"] == "none"
    assert data["rollback_on_failure"] is True
    assert (tmp_path / "kept.txt").read_text(encoding="utf-8") == "kept\n"


@pytest.mark.posix
def test_bash_rolls_back_nonzero_exit(tmp_path: Path) -> None:
    (tmp_path / "earlier.txt").write_text("earlier staged work", encoding="utf-8")
    (tmp_path / "state").write_text("before", encoding="utf-8")
    output, data = asyncio.run(run_bash(tmp_path, {"command": "printf after > state; echo changed > failed.txt; echo error >&2; exit 7"}))
    assert data["exit_code"] == 7
    assert data["rolled_back"] is True
    assert data["rollback_reason"] == "nonzero_exit"
    assert data["rollback_on_failure"] is True
    assert data["stderr_bytes"] > 0
    assert not (tmp_path / "failed.txt").exists()
    assert (tmp_path / "state").read_text(encoding="utf-8") == "before"
    # Only this command's transaction is discarded; earlier staged work remains.
    assert (tmp_path / "earlier.txt").read_text(encoding="utf-8") == "earlier staged work"
    assert "error" in output


# rollback_on_failure opt-out


@pytest.mark.posix
def test_bash_rollback_opt_out_retains_partial_progress_on_nonzero_exit(tmp_path: Path) -> None:
    (tmp_path / "existing.txt").write_text("before", encoding="utf-8")
    command = "printf generated > one.txt; printf updated > existing.txt; exit 3"
    output, data = asyncio.run(run_bash(tmp_path, {"command": command, "rollback_on_failure": False}))
    assert data["exit_code"] == 3
    assert data["rolled_back"] is False
    assert data["rollback_reason"] == "none"
    assert data["rollback_on_failure"] is False
    assert data["checkpoint_id"]
    assert (tmp_path / "one.txt").read_text(encoding="utf-8") == "generated"
    assert (tmp_path / "existing.txt").read_text(encoding="utf-8") == "updated"


@pytest.mark.posix
def test_bash_rollback_opt_out_still_rolls_back_on_timeout(tmp_path: Path) -> None:
    (tmp_path / "existing.txt").write_text("before", encoding="utf-8")
    command = "printf partial > existing.txt; printf new > created.txt; sleep 10"
    with pytest.raises(TimeoutError, match="approved timeout"):
        asyncio.run(run_bash(tmp_path, {"command": command, "timeout_seconds": 1, "rollback_on_failure": False}, max_seconds=2))
    assert (tmp_path / "existing.txt").read_text(encoding="utf-8") == "before"
    assert not (tmp_path / "created.txt").exists()


@pytest.mark.posix
def test_bash_rollback_opt_out_still_rolls_back_on_cancellation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "existing.txt").write_text("before", encoding="utf-8")
    command = "printf partial > existing.txt; printf new > created.txt; sleep 30"
    data = run_and_cancel_after_start(tmp_path, {"command": command, "rollback_on_failure": False}, monkeypatch)
    assert data["cancelled"] is True
    assert data["rolled_back"] is True
    assert data["rollback_reason"] == "cancelled"
    assert data["rollback_on_failure"] is False
    assert (tmp_path / "existing.txt").read_text(encoding="utf-8") == "before"
    assert not (tmp_path / "created.txt").exists()
