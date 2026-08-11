from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import time
from collections import deque
from pathlib import Path

from ..checkpoints import create_checkpoint
from ..paths import safe_path

DENIED_EXECUTABLES = frozenset(
    {
        "sh",
        "bash",
        "zsh",
        "cmd",
        "cmd.exe",
        "powershell",
        "pwsh",
        "sudo",
        "ssh",
        "docker",
        "podman",
    }
)
ALLOWED_ENV = frozenset({"LANG", "LC_ALL", "TZ", "PYTHONUTF8", "PYTHONDONTWRITEBYTECODE"})
MAX_EVENT_COUNT = 128
MAX_EVENT_BYTES = 64_000
MAX_EVENT_REQUESTS = 64
MAX_STDIN_BYTES = 256_000


class CommandRunner:
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
        executable = str(arguments.get("executable") or "")
        if (
            not executable
            or Path(executable).name.casefold() in DENIED_EXECUTABLES
            or "/" in executable
            or "\\" in executable
        ):
            raise ValueError("Executable must be a non-shell program available in the image")
        resolved = shutil.which(executable)
        if not resolved:
            raise ValueError("Executable is not installed in the executor")
        argv = arguments.get("arguments") or []
        if (
            not isinstance(argv, list)
            or len(argv) > 200
            or any(not isinstance(item, str) or "\x00" in item for item in argv)
        ):
            raise ValueError("Command arguments must be a bounded string array")
        cwd = safe_path(root, str(arguments.get("working_directory") or "."), must_exist=True)
        if not cwd.is_dir():
            raise ValueError("Working directory is not a directory")
        timeout = min(max_seconds, max(1, int(arguments.get("timeout_seconds") or 60)))
        requested_env = arguments.get("environment") or {}
        if not isinstance(requested_env, dict) or set(requested_env) - ALLOWED_ENV:
            raise ValueError("Command requested a forbidden environment variable")
        stdin_text = arguments.get("stdin")
        if stdin_text is not None and (
            not isinstance(stdin_text, str)
            or "\x00" in stdin_text
            or len(stdin_text.encode("utf-8")) > MAX_STDIN_BYTES
        ):
            raise ValueError("Command stdin must be bounded UTF-8 text")
        environment = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": "/tmp/executor-home",
            **{str(k): str(v) for k, v in requested_env.items()},
        }
        checkpoint_id = create_checkpoint(
            root,
            max_files=max_checkpoint_files,
            max_total_bytes=max_checkpoint_bytes,
            max_storage_bytes=max_checkpoint_bytes,
        )
        started = time.perf_counter()
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
        process = await asyncio.create_subprocess_exec(
            resolved,
            *argv,
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
            raise TimeoutError("Command exceeded its approved timeout") from exc
        except asyncio.CancelledError:
            await self.cancel(request_id)
            await asyncio.gather(stdout_task, stderr_task, stdin_task, return_exceptions=True)
            raise
        finally:
            self._processes.pop(request_id, None)
        stdout, stdout_bytes, stdout_truncated = stdout_result
        stderr, stderr_bytes, stderr_truncated = stderr_result
        combined_bytes = stdout + (b"\n" if stdout and stderr else b"") + stderr
        combined_truncated = len(combined_bytes) > max_output
        combined = combined_bytes[:max_output].decode("utf-8", errors="replace")
        return combined, {
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
            "stdout_bytes": stdout_bytes,
            "stderr_bytes": stderr_bytes,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "stdin_bytes": len(stdin_text.encode("utf-8")) if stdin_text is not None else 0,
            "exit_code": process.returncode,
            "checkpoint_id": checkpoint_id,
            "truncated": stdout_truncated or stderr_truncated or combined_truncated,
            "cancelled": request_id in self._cancelled,
            "elapsed_seconds": time.perf_counter() - started,
        }

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
        event_bytes = len(
            json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        self._sequence[request_id] = int(event["sequence"])
        queue = self._events.setdefault(request_id, deque(maxlen=MAX_EVENT_COUNT))
        retained_bytes = self._event_bytes.get(request_id, 0)
        while queue and (
            retained_bytes + event_bytes > MAX_EVENT_BYTES or len(queue) >= MAX_EVENT_COUNT
        ):
            expired = queue.popleft()
            retained_bytes -= len(
                json.dumps(expired, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            )
        queue.append(event)
        self._event_bytes[request_id] = max(0, retained_bytes + event_bytes)
