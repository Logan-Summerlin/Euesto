from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from collections import deque
from pathlib import Path

from ..mutations import create_mutation_checkpoint, rollback_mutation
from ..paths import safe_path

MAX_EVENT_COUNT = 128
MAX_EVENT_BYTES = 64_000
MAX_EVENT_REQUESTS = 64
MAX_STDIN_BYTES = 256_000
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


class BashRunner:
    """Track shell processes and their bounded output/event streams."""

    def __init__(self) -> None:
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._events: dict[str, deque[dict[str, object]]] = {}
        self._event_bytes: dict[str, int] = {}
        self._sequence: dict[str, int] = {}
        self._event_order: deque[str] = deque()
        self._cancelled: set[str] = set()

    async def run(
        self,
        request_id: str,
        root: Path,
        arguments: dict,
        *,
        max_seconds: int,
        max_output: int,
        max_checkpoint_files: int = 300_000,
        max_checkpoint_bytes: int = 2_000_000_000,
    ) -> tuple[str, dict]:
        command = arguments.get("command")
        if not isinstance(command, str) or not command.strip() or "\x00" in command:
            raise ValueError("bash requires a non-empty command")
        if len(command.encode("utf-8")) > 512_000:
            raise ValueError("bash command is too large")

        working_directory = arguments.get("working_directory", ".")
        if not isinstance(working_directory, str):
            raise ValueError("working_directory must be a string")
        cwd = safe_path(root, working_directory, must_exist=True)
        if not cwd.is_dir():
            raise ValueError("Working directory is not a directory")

        raw_timeout = arguments.get("timeout_seconds", 60)
        if not isinstance(raw_timeout, int) or isinstance(raw_timeout, bool):
            raise ValueError("timeout_seconds must be an integer")
        timeout = min(max_seconds, max(1, raw_timeout))

        stdin_text = arguments.get("stdin")
        if stdin_text is not None and (
            not isinstance(stdin_text, str)
            or "\x00" in stdin_text
            or len(stdin_text.encode("utf-8")) > MAX_STDIN_BYTES
        ):
            raise ValueError("bash stdin must be bounded UTF-8 text")

        requested_env = arguments.get("env", {})
        environment = self._environment(requested_env)
        checkpoint_id = create_mutation_checkpoint(root, max_files=max_checkpoint_files, max_total_bytes=max_checkpoint_bytes)

        started = time.perf_counter()
        self._start_events(request_id)
        stdout_task = stderr_task = stdin_task = None
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                "/bin/bash",
                "-lc",
                command,
                cwd=cwd,
                env=environment,
                stdin=asyncio.subprocess.PIPE if stdin_text is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            self._processes[request_id] = process
            stdout_task = asyncio.create_task(self._read_stream(request_id, "stdout", process.stdout, max_output))
            stderr_task = asyncio.create_task(self._read_stream(request_id, "stderr", process.stderr, max_output))
            stdin_task = asyncio.create_task(self._write_stdin(process, stdin_text))
            try:
                stdout_result, stderr_result, _ = await asyncio.wait_for(
                    asyncio.gather(stdout_task, stderr_task, stdin_task), timeout=timeout
                )
                await process.wait()
            except TimeoutError as exc:
                await self.cancel(request_id)
                await asyncio.gather(stdout_task, stderr_task, stdin_task, return_exceptions=True)
                rollback_mutation(root, checkpoint_id)
                raise TimeoutError("Bash command exceeded its approved timeout") from exc
            except asyncio.CancelledError:
                await self.cancel(request_id)
                await asyncio.gather(stdout_task, stderr_task, stdin_task, return_exceptions=True)
                rollback_mutation(root, checkpoint_id)
                raise
            finally:
                self._processes.pop(request_id, None)

            stdout, stdout_bytes, stdout_truncated = stdout_result
            stderr, stderr_bytes, stderr_truncated = stderr_result
            rolled_back = process.returncode != 0
            if rolled_back:
                rollback_mutation(root, checkpoint_id)
            combined_bytes = stdout + (b"\n" if stdout and stderr else b"") + stderr
            combined_truncated = len(combined_bytes) > max_output
            combined = combined_bytes[:max_output].decode("utf-8", errors="replace")
            return combined, {
                "exit_code": process.returncode,
                "checkpoint_id": checkpoint_id,
                "elapsed_seconds": time.perf_counter() - started,
                "stdout": stdout.decode("utf-8", errors="replace"),
                "stderr": stderr.decode("utf-8", errors="replace"),
                "stdout_bytes": stdout_bytes,
                "stderr_bytes": stderr_bytes,
                "stdout_truncated": stdout_truncated,
                "stderr_truncated": stderr_truncated,
                "stdin_bytes": len(stdin_text.encode("utf-8")) if stdin_text is not None else 0,
                "truncated": stdout_truncated or stderr_truncated or combined_truncated,
                "cancelled": request_id in self._cancelled,
                "rolled_back": rolled_back,
            }
        except Exception:
            if process is None:
                rollback_mutation(root, checkpoint_id)
            raise

    @staticmethod
    def _environment(requested: object) -> dict[str, str]:
        if not isinstance(requested, dict):
            raise ValueError("env must be an object")
        if len(requested) > MAX_ENV_VARS:
            raise ValueError("env contains too many variables")
        environment = dict(BASE_ENVIRONMENT)
        for key, value in requested.items():
            if not isinstance(key, str) or not key or len(key) > 128 or "\x00" in key:
                raise ValueError("env variable names must be bounded strings")
            if not key.replace("_", "").isalnum() or key[0].isdigit():
                raise ValueError("env variable names must be POSIX identifiers")
            if key in {"PATH", "HOME", "LD_PRELOAD", "LD_LIBRARY_PATH"} or key.startswith("BASH_ENV"):
                raise ValueError(f"env variable is restricted: {key}")
            if not isinstance(value, str) or "\x00" in value or len(value.encode("utf-8")) > MAX_ENV_VALUE_BYTES:
                raise ValueError("env variable values must be bounded UTF-8 strings")
            environment[key] = value
        return environment

    def _start_events(self, request_id: str) -> None:
        if request_id not in self._events:
            self._event_order.append(request_id)
        while len(self._event_order) > MAX_EVENT_REQUESTS:
            expired = self._event_order.popleft()
            self._events.pop(expired, None)
            self._event_bytes.pop(expired, None)
            self._sequence.pop(expired, None)
            self._cancelled.discard(expired)
        self._events[request_id] = deque(maxlen=MAX_EVENT_COUNT)
        self._event_bytes[request_id] = 0
        self._sequence[request_id] = 0
        self._cancelled.discard(request_id)

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

    def events(self, request_id: str, after: int = 0) -> dict[str, object]:
        values = list(self._events.get(request_id, ()))
        first = int(values[0]["sequence"]) if values else self._sequence.get(request_id, 0) + 1
        return {
            "events": [item for item in values if int(item["sequence"]) > max(0, after)],
            "next_cursor": self._sequence.get(request_id, 0),
            "truncated": bool(values and after < first - 1),
            "active": request_id in self._processes,
        }

    async def cancel(self, request_id: str) -> bool:
        process = self._processes.get(request_id)
        self._cancelled.add(request_id)
        if not process or process.returncode is not None:
            return False
        try:
            os.killpg(process.pid, signal.SIGTERM)
            await asyncio.wait_for(process.wait(), timeout=2)
        except TimeoutError:
            os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
        return True

    async def _read_stream(
        self,
        request_id: str,
        name: str,
        stream: asyncio.StreamReader | None,
        max_output: int,
    ) -> tuple[bytes, int, bool]:
        if stream is None:
            return b"", 0, False
        retained = bytearray()
        total = 0
        while chunk := await stream.read(16_384):
            total += len(chunk)
            if len(retained) < max_output:
                retained.extend(chunk[: max_output - len(retained)])
            self._append_event(request_id, name, chunk)
        return bytes(retained), total, total > max_output

    def _append_event(self, request_id: str, stream: str, chunk: bytes) -> None:
        event = {
            "sequence": self._sequence.get(request_id, 0) + 1,
            "stream": stream,
            "text": chunk[:4_096].decode("utf-8", errors="replace"),
        }
        event_bytes = len(json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        self._sequence[request_id] = int(event["sequence"])
        queue = self._events.setdefault(request_id, deque(maxlen=MAX_EVENT_COUNT))
        retained_bytes = self._event_bytes.get(request_id, 0)
        while queue and (retained_bytes + event_bytes > MAX_EVENT_BYTES or len(queue) >= MAX_EVENT_COUNT):
            expired = queue.popleft()
            retained_bytes -= len(json.dumps(expired, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        queue.append(event)
        self._event_bytes[request_id] = max(0, retained_bytes + event_bytes)


_runner = BashRunner()


async def bash(request_id: str, root: Path, arguments: dict, *, max_seconds: int, max_output: int, max_checkpoint_files: int = 300_000, max_checkpoint_bytes: int = 2_000_000_000) -> tuple[str, dict]:
    return await _runner.run(request_id, root, arguments, max_seconds=max_seconds, max_output=max_output, max_checkpoint_files=max_checkpoint_files, max_checkpoint_bytes=max_checkpoint_bytes)


async def cancel(request_id: str) -> bool:
    return await _runner.cancel(request_id)


def events(request_id: str, after: int = 0) -> dict[str, object]:
    return _runner.events(request_id, after)
