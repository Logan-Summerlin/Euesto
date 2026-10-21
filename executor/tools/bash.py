from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from collections.abc import Callable
from pathlib import Path

from runner.process import (
    MAX_LOG_BYTES,
    OUTPUT_DIRECTORY,
    OutputBuffer,
    run_process,
)
from runner.process import (  # re-exported: the retained-output bound is part of the contract
    MAX_RETAINED_OUTPUT_BYTES as MAX_RETAINED_OUTPUT_BYTES,
)

from ..checkpoints import DEFAULT_MAX_CHECKPOINT_FILES, checkpointed, restore_checkpoint
from ..config import ExecutorConfig
from ..egress import proxy_environment
from ..errors import (
    COMMAND_INVALID_ARGUMENTS,
    LIMIT_EXCEEDED,
    WORKING_DIRECTORY_INVALID,
    ExecutorToolError,
)
from ..paths import safe_path
from ..reduction import reduce_output

MAX_STDIN_BYTES = ExecutorConfig.HARD_CEILINGS["max_bash_stdin_bytes"]
MAX_COMMAND_BYTES = ExecutorConfig.HARD_CEILINGS["max_command_bytes"]
MAX_COMMAND_SECONDS = ExecutorConfig.HARD_CEILINGS["max_command_seconds"]
DEFAULT_TIMEOUT_SECONDS = 60
MAX_ENV_VARS = 64
MAX_ENV_VALUE_BYTES = 16_384
RUNNER_CONNECT_SECONDS = 10
BASE_ENVIRONMENT = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "HOME": "/tmp/executor-home",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PYTHONUTF8": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
}
_OutputBuffer = OutputBuffer


class RunnerUnavailable(OSError):
    """The runner container did not accept or finish a command."""


class BashRunner:
    """Track running shell processes so they can be cancelled."""

    def __init__(self) -> None:
        self._pids: dict[str, int] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._cancelled: set[str] = set()
        # None runs commands in this process (unit tests); the executor container points this
        # at the runner's socket so commands run in the separate workbench container.
        self.runner_socket: Path | None = None
        self.on_started: Callable[[str], None] | None = None

    def configure(self, runner_socket: Path | None) -> None:
        self.runner_socket = runner_socket

    async def run(
        self,
        request_id: str,
        root: Path,
        arguments: dict,
        *,
        max_seconds: int,
        max_output: int,
        max_command_bytes: int = MAX_COMMAND_BYTES,
        max_stdin_bytes: int = MAX_STDIN_BYTES,
        max_checkpoint_files: int = DEFAULT_MAX_CHECKPOINT_FILES,
        max_checkpoint_bytes: int,
    ) -> tuple[str, dict]:
        command = arguments.get("command")
        if not isinstance(command, str) or not command.strip() or "\x00" in command:
            raise ExecutorToolError(
                COMMAND_INVALID_ARGUMENTS, "bash requires a non-empty command"
            )
        command_limit = min(max_command_bytes, MAX_COMMAND_BYTES)
        if len(command.encode("utf-8")) > command_limit:
            raise ExecutorToolError(
                LIMIT_EXCEEDED,
                f"bash command exceeds the configured limit of {command_limit} bytes",
            )

        working_directory = arguments.get("working_directory", ".")
        if not isinstance(working_directory, str):
            raise ExecutorToolError(
                WORKING_DIRECTORY_INVALID, "working_directory must be a string"
            )
        cwd = safe_path(root, working_directory, must_exist=True)
        if not cwd.is_dir():
            raise ExecutorToolError(
                WORKING_DIRECTORY_INVALID, "Working directory is not a directory"
            )

        raw_timeout = arguments.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
        if not isinstance(raw_timeout, int) or isinstance(raw_timeout, bool):
            raise ExecutorToolError(
                COMMAND_INVALID_ARGUMENTS, "timeout_seconds must be an integer"
            )
        timeout = min(MAX_COMMAND_SECONDS, max_seconds, max(1, raw_timeout))

        rollback_on_failure = arguments.get("rollback_on_failure", True)
        if not isinstance(rollback_on_failure, bool):
            raise ExecutorToolError(
                COMMAND_INVALID_ARGUMENTS, "rollback_on_failure must be a boolean"
            )

        stdin_text = arguments.get("stdin")
        stdin_limit = min(max_stdin_bytes, MAX_STDIN_BYTES)
        if stdin_text is not None and (
            not isinstance(stdin_text, str)
            or "\x00" in stdin_text
            or len(stdin_text.encode("utf-8")) > stdin_limit
        ):
            raise ExecutorToolError(
                LIMIT_EXCEEDED,
                f"bash stdin exceeds the configured limit of {stdin_limit} bytes",
            )

        environment = self._environment(arguments.get("env", {}))
        started = time.perf_counter()
        self._cancelled.discard(request_id)
        try:
            # Any failure, timeout, or cancellation restores the checkpoint before propagating.
            with checkpointed(
                root, max_files=max_checkpoint_files, max_total_bytes=max_checkpoint_bytes
            ) as checkpoint_id:
                outcome = await self._execute(
                    request_id,
                    command,
                    root,
                    cwd.relative_to(root).as_posix() if cwd != root else ".",
                    environment,
                    stdin_text,
                    max_output,
                    timeout,
                )
                if outcome["timed_out"]:
                    log = (
                        f"; partial output in {outcome['log_path']}"
                        if outcome["log_path"]
                        else ""
                    )
                    raise TimeoutError(f"Bash command exceeded its approved timeout{log}")
            stdout, stderr = outcome["stdout"], outcome["stderr"]
            exit_code = outcome["exit_code"]
            # Cancellation always rolls back; rollback_on_failure only governs non-zero exits.
            cancelled = request_id in self._cancelled
        finally:
            self._cancelled.discard(request_id)
        if cancelled:
            rollback_reason = "cancelled"
        elif exit_code != 0 and rollback_on_failure:
            rollback_reason = "nonzero_exit"
        else:
            rollback_reason = "none"
        rolled_back = rollback_reason != "none"
        if rolled_back:
            restore_checkpoint(root, checkpoint_id)
        combined = self._model_output(stdout, stderr, max_output)
        log_path = outcome["log_path"]
        reduction = reduce_output(command, _read_log(root, log_path) or _joined(stdout, stderr))
        return combined, {
            "log_path": log_path,
            "log_bytes": outcome["log_bytes"],
            "log_truncated": outcome["log_truncated"],
            "reduction": reduction,
            "exit_code": exit_code,
            "checkpoint_id": checkpoint_id,
            "elapsed_seconds": time.perf_counter() - started,
            "stdout": stdout.bytes().decode("utf-8", errors="replace"),
            "stderr": stderr.bytes().decode("utf-8", errors="replace"),
            "stdout_bytes": stdout.total,
            "stderr_bytes": stderr.total,
            "retained_output_bytes": len(stdout.bytes()) + len(stderr.bytes()),
            "model_output_bytes": len(combined.encode("utf-8")),
            "stdout_truncated": stdout.truncated,
            "stderr_truncated": stderr.truncated,
            "stdin_bytes": len(stdin_text.encode("utf-8")) if stdin_text is not None else 0,
            "truncated": stdout.truncated or stderr.truncated,
            "cancelled": cancelled,
            "rolled_back": rolled_back,
            "rollback_on_failure": rollback_on_failure,
            "rollback_reason": rollback_reason,
        }

    async def _execute(
        self,
        request_id: str,
        command: str,
        root: Path,
        relative_cwd: str,
        environment: dict[str, str],
        stdin_text: str | None,
        max_output: int,
        timeout: int,
    ) -> dict[str, object]:
        """Run one command to completion in the runner (or in-process for tests)."""
        task = asyncio.create_task(
            self._run_remote(
                request_id, command, relative_cwd, environment, stdin_text, max_output, timeout
            )
            if self.runner_socket is not None
            else self._run_local(
                request_id,
                command,
                root,
                relative_cwd,
                environment,
                stdin_text,
                max_output,
                timeout,
            )
        )
        self._tasks[request_id] = task
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await self.cancel(request_id)
            await asyncio.gather(task, return_exceptions=True)
            raise
        finally:
            self._tasks.pop(request_id, None)
            self._pids.pop(request_id, None)

    def _started(self, request_id: str, pid: int) -> None:
        self._pids[request_id] = pid
        if self.on_started is not None:
            self.on_started(request_id)

    async def _run_local(
        self,
        request_id: str,
        command: str,
        root: Path,
        relative_cwd: str,
        environment: dict[str, str],
        stdin_text: str | None,
        max_output: int,
        timeout: int,
    ) -> dict[str, object]:
        return await run_process(
            request_id,
            command,
            root if relative_cwd == "." else root / relative_cwd,
            environment,
            stdin_text,
            max_output,
            timeout,
            work_root=root,
            on_started=lambda pid: self._started(request_id, pid),
        )

    async def _run_remote(
        self,
        request_id: str,
        command: str,
        relative_cwd: str,
        environment: dict[str, str],
        stdin_text: str | None,
        max_output: int,
        timeout: int,
    ) -> dict[str, object]:
        request = {
            "op": "run",
            "request_id": request_id,
            "command": command,
            "cwd": relative_cwd,
            "env": environment,
            "stdin": stdin_text,
            "timeout": timeout,
            "max_output": max_output,
        }
        reader, writer = await self._connect()
        try:
            writer.write(json.dumps(request).encode("utf-8") + b"\n")
            await writer.drain()
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=timeout + 30)
                if not line:
                    raise RunnerUnavailable("The runner closed the connection mid-command.")
                message = json.loads(line)
                event = message.get("event")
                if event == "started":
                    self._started(request_id, int(message.get("pid") or 0))
                elif event == "result":
                    return _remote_outcome(message, max_output)
                else:
                    raise RunnerUnavailable(
                        f"The runner refused the command: {message.get('code', 'unknown')}"
                    )
        finally:
            writer.close()

    async def probe(self, names: list[str]) -> list[str] | None:
        """Which of ``names`` the runner's image provides; None when the runner is unreachable."""
        if self.runner_socket is None:
            return None
        try:
            reader, writer = await self._connect()
        except RunnerUnavailable:
            return None
        try:
            request = {"op": "probe", "request_id": "probe", "names": names}
            writer.write(json.dumps(request).encode("utf-8") + b"\n")
            await writer.drain()
            reply = json.loads(await asyncio.wait_for(reader.readline(), timeout=5))
        except (OSError, TimeoutError, ValueError):
            return None
        finally:
            writer.close()
        found = reply.get("executables") if isinstance(reply, dict) else None
        if not isinstance(found, list):
            return None
        return [name for name in names if name in found]

    async def _connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        assert self.runner_socket is not None
        try:
            return await asyncio.wait_for(
                asyncio.open_unix_connection(os.fspath(self.runner_socket), limit=4_000_000),
                timeout=RUNNER_CONNECT_SECONDS,
            )
        except (OSError, TimeoutError) as exc:
            raise RunnerUnavailable("The runner is not reachable.") from exc

    @staticmethod
    def _model_output(stdout: _OutputBuffer, stderr: _OutputBuffer, max_output: int) -> str:
        if not stdout.truncated and not stderr.truncated:
            combined = (
                stdout.bytes()
                + (b"\n" if stdout.bytes() and stderr.bytes() else b"")
                + stderr.bytes()
            )
            return combined[:max_output].decode("utf-8", errors="replace")
        sections = []
        if stdout.total:
            sections.append(
                f"[stdout: {stdout.total} bytes]\n{stdout.bytes().decode('utf-8', errors='replace')}"
            )
        if stderr.total:
            sections.append(
                f"[stderr: {stderr.total} bytes]\n{stderr.bytes().decode('utf-8', errors='replace')}"
            )
        combined = "\n".join(sections).encode("utf-8")
        if len(combined) <= max_output:
            return combined.decode("utf-8", errors="replace")
        marker = b"\n... [output truncated] ...\n"
        budget = max(0, max_output - len(marker))
        return (combined[:budget] + marker).decode("utf-8", errors="replace")

    @staticmethod
    def _environment(requested: object) -> dict[str, str]:
        if not isinstance(requested, dict):
            raise ExecutorToolError(COMMAND_INVALID_ARGUMENTS, "env must be an object")
        if len(requested) > MAX_ENV_VARS:
            raise ExecutorToolError(LIMIT_EXCEEDED, "env contains too many variables")
        # Empty unless the opt-in allowlisted-egress profile configured a proxy for this executor.
        environment = {**BASE_ENVIRONMENT, **proxy_environment()}
        for key, value in requested.items():
            identifier = (
                isinstance(key, str)
                and 0 < len(key) <= 128
                and "\x00" not in key
                and key.replace("_", "").isalnum()
                and not key[0].isdigit()
            )
            if not identifier:
                raise ExecutorToolError(
                    COMMAND_INVALID_ARGUMENTS, "env variable names must be POSIX identifiers"
                )
            if key in {"PATH", "HOME", "LD_PRELOAD", "LD_LIBRARY_PATH"} or key.startswith(
                "BASH_ENV"
            ):
                raise ExecutorToolError(
                    COMMAND_INVALID_ARGUMENTS, f"env variable is restricted: {key}"
                )
            if (
                not isinstance(value, str)
                or "\x00" in value
                or len(value.encode("utf-8")) > MAX_ENV_VALUE_BYTES
            ):
                raise ExecutorToolError(
                    COMMAND_INVALID_ARGUMENTS,
                    "env variable values must be bounded UTF-8 strings",
                )
            environment[key] = value
        return environment

    @staticmethod
    async def _write_stdin(process: asyncio.subprocess.Process, value: str | None) -> None:
        if value is None or process.stdin is None:
            return
        try:
            process.stdin.write(value.encode("utf-8"))
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            process.stdin.close()

    async def cancel(self, request_id: str) -> bool:
        if request_id not in self._tasks:
            return False
        self._cancelled.add(request_id)
        if self.runner_socket is not None:
            try:
                reader, writer = await self._connect()
                writer.write(
                    json.dumps({"op": "cancel", "request_id": request_id}).encode() + b"\n"
                )
                await writer.drain()
                reply = json.loads(
                    await asyncio.wait_for(reader.readline(), timeout=5) or b"{}"
                )
                writer.close()
                return bool(reply.get("cancelled"))
            except (OSError, ValueError, TimeoutError):
                return False
        pid = self._pids.get(request_id)
        if pid is None:
            return False
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return True
        task = self._tasks.get(request_id)
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=2)
            except TimeoutError:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            except Exception:
                pass
        return True


def _remote_outcome(message: dict, max_output: int) -> dict[str, object]:
    """Rebuild bounded buffers from the runner's reply; the runner itself is not trusted, so
    every field is re-bounded here."""
    buffers = []
    for name in ("stdout", "stderr"):
        buffer = OutputBuffer(max_output)
        buffer.append(str(message.get(name) or "").encode("utf-8"))
        buffer.total = max(buffer.total, int(message.get(f"{name}_bytes") or 0))
        if message.get(f"{name}_truncated") and not buffer.truncated:
            buffer._truncated = True
            buffer.head = bytearray(buffer.bytes())
            buffer.tail.clear()
            buffer.tail_bytes = 0
        buffers.append(buffer)
    exit_code = message.get("exit_code")
    log_path = message.get("log_path")
    return {
        "exit_code": exit_code if isinstance(exit_code, int) else None,
        "timed_out": bool(message.get("timed_out")),
        "stdout": buffers[0],
        "stderr": buffers[1],
        "log_path": log_path if isinstance(log_path, str) else None,
        "log_bytes": int(message.get("log_bytes") or 0),
        "log_truncated": bool(message.get("log_truncated")),
    }


def _read_log(root: Path, log_path: object) -> str | None:
    """The full log, read without following links, for reduction only."""
    if not isinstance(log_path, str) or not log_path.startswith(OUTPUT_DIRECTORY + "/"):
        return None
    name = log_path.split("/", 1)[1]
    if "/" in name:
        return None
    try:
        directory = os.open(
            root / OUTPUT_DIRECTORY, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
    except OSError:
        return None
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
    except OSError:
        return None
    finally:
        os.close(directory)
    with os.fdopen(descriptor, "rb") as handle:
        return handle.read(MAX_LOG_BYTES + 1024).decode("utf-8", errors="replace")


def _joined(stdout: OutputBuffer, stderr: OutputBuffer) -> str:
    return (stdout.bytes() + b"\n" + stderr.bytes()).decode("utf-8", errors="replace")


_runner = BashRunner()


bash = _runner.run
cancel = _runner.cancel
configure_runner = _runner.configure
probe_runner = _runner.probe
