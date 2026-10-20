from __future__ import annotations

import asyncio
import os
import signal
import time
from collections import deque
from pathlib import Path

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

MAX_RETAINED_OUTPUT_BYTES = 512_000
MAX_STREAM_PREVIEW_BYTES = MAX_RETAINED_OUTPUT_BYTES // 2
MAX_STDIN_BYTES = ExecutorConfig.HARD_CEILINGS["max_bash_stdin_bytes"]
MAX_COMMAND_BYTES = ExecutorConfig.HARD_CEILINGS["max_command_bytes"]
MAX_COMMAND_SECONDS = ExecutorConfig.HARD_CEILINGS["max_command_seconds"]
DEFAULT_TIMEOUT_SECONDS = 60
MAX_ENV_VARS = 64
MAX_ENV_VALUE_BYTES = 16_384
BASE_ENVIRONMENT = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "HOME": "/tmp/executor-home",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PYTHONUTF8": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
}


class _OutputBuffer:
    def __init__(self, limit: int) -> None:
        self.limit = max(1, min(limit, MAX_RETAINED_OUTPUT_BYTES))
        self._full = bytearray()
        self.head = bytearray()
        self.tail: deque[bytes] = deque()
        self.tail_bytes = 0
        self.total = 0
        self._truncated = False

    def append(self, chunk: bytes) -> None:
        self.total += len(chunk)
        if not self._truncated:
            self._full.extend(chunk)
            if len(self._full) <= self.limit:
                return
            self._truncated = True
            self.head = self._full[:MAX_STREAM_PREVIEW_BYTES]
            self.tail.clear()
            self.tail.append(self._full[-MAX_STREAM_PREVIEW_BYTES:])
            self.tail_bytes = len(self.tail[0])
            self._full.clear()
            return
        self.tail.append(chunk)
        self.tail_bytes += len(chunk)
        while self.tail and self.tail_bytes > MAX_STREAM_PREVIEW_BYTES:
            excess = self.tail_bytes - MAX_STREAM_PREVIEW_BYTES
            first = self.tail[0]
            if len(first) <= excess:
                self.tail.popleft()
                self.tail_bytes -= len(first)
            else:
                self.tail[0] = first[excess:]
                self.tail_bytes -= excess

    def bytes(self) -> bytes:
        if not self._truncated:
            return bytes(self._full)
        return bytes(self.head) + b"\n... [output truncated] ...\n" + b"".join(self.tail)

    @property
    def truncated(self) -> bool:
        return self._truncated


class BashRunner:
    """Track running shell processes so they can be cancelled."""

    def __init__(self) -> None:
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._cancelled: set[str] = set()

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
            raise ExecutorToolError(COMMAND_INVALID_ARGUMENTS, "bash requires a non-empty command")
        command_limit = min(max_command_bytes, MAX_COMMAND_BYTES)
        if len(command.encode("utf-8")) > command_limit:
            raise ExecutorToolError(LIMIT_EXCEEDED, f"bash command exceeds the configured limit of {command_limit} bytes")

        working_directory = arguments.get("working_directory", ".")
        if not isinstance(working_directory, str):
            raise ExecutorToolError(WORKING_DIRECTORY_INVALID, "working_directory must be a string")
        cwd = safe_path(root, working_directory, must_exist=True)
        if not cwd.is_dir():
            raise ExecutorToolError(WORKING_DIRECTORY_INVALID, "Working directory is not a directory")

        raw_timeout = arguments.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
        if not isinstance(raw_timeout, int) or isinstance(raw_timeout, bool):
            raise ExecutorToolError(COMMAND_INVALID_ARGUMENTS, "timeout_seconds must be an integer")
        timeout = min(MAX_COMMAND_SECONDS, max_seconds, max(1, raw_timeout))

        rollback_on_failure = arguments.get("rollback_on_failure", True)
        if not isinstance(rollback_on_failure, bool):
            raise ExecutorToolError(COMMAND_INVALID_ARGUMENTS, "rollback_on_failure must be a boolean")

        stdin_text = arguments.get("stdin")
        stdin_limit = min(max_stdin_bytes, MAX_STDIN_BYTES)
        if stdin_text is not None and (
            not isinstance(stdin_text, str) or "\x00" in stdin_text or len(stdin_text.encode("utf-8")) > stdin_limit
        ):
            raise ExecutorToolError(LIMIT_EXCEEDED, f"bash stdin exceeds the configured limit of {stdin_limit} bytes")

        environment = self._environment(arguments.get("env", {}))
        started = time.perf_counter()
        self._cancelled.discard(request_id)
        try:
            # Any failure, timeout, or cancellation restores the checkpoint before propagating.
            with checkpointed(root, max_files=max_checkpoint_files, max_total_bytes=max_checkpoint_bytes) as checkpoint_id:
                stdout, stderr, exit_code = await self._execute(
                    request_id, command, cwd, environment, stdin_text, max_output, timeout
                )
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
        return combined, {
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
        cwd: Path,
        environment: dict[str, str],
        stdin_text: str | None,
        max_output: int,
        timeout: int,
    ) -> tuple[_OutputBuffer, _OutputBuffer, int | None]:
        """Run one command to completion; on any failure kill its process group first."""
        process = await asyncio.create_subprocess_exec(
            "/bin/bash",
            "-lc",
            command,
            cwd=cwd,
            env=environment,
            stdin=asyncio.subprocess.PIPE if stdin_text is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        self._processes[request_id] = process
        tasks = (
            asyncio.create_task(self._read_stream(process.stdout, max_output)),
            asyncio.create_task(self._read_stream(process.stderr, max_output)),
            asyncio.create_task(self._write_stdin(process, stdin_text)),
        )
        try:
            stdout, stderr, _ = await asyncio.wait_for(asyncio.gather(*tasks), timeout=timeout)
            await process.wait()
        except BaseException as exc:
            await self.cancel(request_id)
            await asyncio.gather(*tasks, return_exceptions=True)
            if isinstance(exc, TimeoutError):
                raise TimeoutError("Bash command exceeded its approved timeout") from exc
            raise
        finally:
            self._processes.pop(request_id, None)
        return stdout, stderr, process.returncode

    @staticmethod
    def _model_output(stdout: _OutputBuffer, stderr: _OutputBuffer, max_output: int) -> str:
        if not stdout.truncated and not stderr.truncated:
            combined = stdout.bytes() + (b"\n" if stdout.bytes() and stderr.bytes() else b"") + stderr.bytes()
            return combined[:max_output].decode("utf-8", errors="replace")
        sections = []
        if stdout.total:
            sections.append(f"[stdout: {stdout.total} bytes]\n{stdout.bytes().decode('utf-8', errors='replace')}")
        if stderr.total:
            sections.append(f"[stderr: {stderr.total} bytes]\n{stderr.bytes().decode('utf-8', errors='replace')}")
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
                raise ExecutorToolError(COMMAND_INVALID_ARGUMENTS, "env variable names must be POSIX identifiers")
            if key in {"PATH", "HOME", "LD_PRELOAD", "LD_LIBRARY_PATH"} or key.startswith("BASH_ENV"):
                raise ExecutorToolError(COMMAND_INVALID_ARGUMENTS, f"env variable is restricted: {key}")
            if not isinstance(value, str) or "\x00" in value or len(value.encode("utf-8")) > MAX_ENV_VALUE_BYTES:
                raise ExecutorToolError(COMMAND_INVALID_ARGUMENTS, "env variable values must be bounded UTF-8 strings")
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
        process = self._processes.get(request_id)
        if not process:
            return False
        self._cancelled.add(request_id)
        if process.returncode is not None:
            return False
        try:
            os.killpg(process.pid, signal.SIGTERM)
            await asyncio.wait_for(process.wait(), timeout=2)
        except TimeoutError:
            os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
        return True

    @staticmethod
    async def _read_stream(stream: asyncio.StreamReader | None, max_output: int) -> _OutputBuffer:
        retained = _OutputBuffer(max_output)
        while stream is not None and (chunk := await stream.read(16_384)):
            retained.append(chunk)
        return retained


_runner = BashRunner()


bash = _runner.run
cancel = _runner.cancel

