"""Run one command with bounded captured output and a full output log (plan items 1.5, 2.1)."""

from __future__ import annotations

import asyncio
import os
import re
import signal
import stat
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path

OUTPUT_DIRECTORY = ".euesto-output"
MAX_RETAINED_OUTPUT_BYTES = 512_000
MAX_STREAM_PREVIEW_BYTES = MAX_RETAINED_OUTPUT_BYTES // 2
MAX_LOG_BYTES = 16_000_000
MAX_LOG_DIRECTORY_BYTES = 64_000_000
MAX_LOG_FILES = 200
MAX_LOG_AGE_SECONDS = 24 * 3600
LOG_TRUNCATED_MARKER = b"\n... [log truncated at the per-command limit] ...\n"
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class OutputBuffer:
    """Keep a stream whole up to ``limit`` bytes, then only a bounded head and tail."""

    def __init__(self, limit: int) -> None:
        self.limit = max(1, min(limit, MAX_RETAINED_OUTPUT_BYTES))
        self.preview = max(1, min(MAX_STREAM_PREVIEW_BYTES, self.limit // 2))
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
            self.head = self._full[: self.preview]
            self.tail.clear()
            self.tail.append(self._full[-self.preview :])
            self.tail_bytes = len(self.tail[0])
            self._full.clear()
            return
        self.tail.append(chunk)
        self.tail_bytes += len(chunk)
        while self.tail and self.tail_bytes > self.preview:
            excess = self.tail_bytes - self.preview
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


class OutputLog:
    """The complete interleaved output of one command, written beneath the work root.

    The directory and file are opened without following links, so a command that replaces
    ``.euesto-output`` with a link cannot redirect the log anywhere else.
    """

    def __init__(self, work_root: Path, request_id: str) -> None:
        if not _REQUEST_ID.fullmatch(request_id):
            raise ValueError("request id is not a safe log name")
        self.relative = f"{OUTPUT_DIRECTORY}/{request_id}.log"
        self.bytes_written = 0
        self.truncated = False
        self.opened = False
        self._fd: int | None = None
        try:
            directory = _open_output_directory(work_root)
        except OSError:
            return
        try:
            self._fd = os.open(
                f"{request_id}.log",
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o640,
                dir_fd=directory,
            )
        except OSError:
            self._fd = None
        finally:
            os.close(directory)
        self.opened = self._fd is not None

    @property
    def available(self) -> bool:
        return self._fd is not None

    def write(self, chunk: bytes) -> None:
        if self._fd is None or self.truncated:
            return
        room = MAX_LOG_BYTES - self.bytes_written
        if len(chunk) > room:
            chunk = chunk[: max(0, room)] + LOG_TRUNCATED_MARKER
            self.truncated = True
        try:
            os.write(self._fd, chunk)
        except OSError:
            self.close()
            return
        self.bytes_written += len(chunk)

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


def _open_output_directory(work_root: Path) -> int:
    root = os.open(work_root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        try:
            os.mkdir(OUTPUT_DIRECTORY, 0o770, dir_fd=root)
        except FileExistsError:
            pass
        return os.open(
            OUTPUT_DIRECTORY,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=root,
        )
    finally:
        os.close(root)


def prune_logs(
    work_root: Path,
    *,
    keep: str | None = None,
    max_age_seconds: int = MAX_LOG_AGE_SECONDS,
    max_bytes: int = MAX_LOG_DIRECTORY_BYTES,
    max_files: int = MAX_LOG_FILES,
    now: float | None = None,
) -> list[str]:
    """Delete logs older than ``max_age_seconds``, then the oldest until the directory fits."""
    try:
        directory = _open_output_directory(work_root)
    except OSError:
        return []
    removed: list[str] = []
    try:
        entries: list[tuple[float, int, str]] = []
        for name in os.listdir(directory):
            try:
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                entries.append((info.st_mtime, info.st_size, name))
        entries.sort()
        current = time.time() if now is None else now
        total = sum(size for _mtime, size, _name in entries)
        count = len(entries)
        for mtime, size, name in entries:
            expired = current - mtime > max_age_seconds
            if name != keep and (expired or total > max_bytes or count > max_files):
                try:
                    os.unlink(name, dir_fd=directory)
                except OSError:
                    continue
                removed.append(name)
                total -= size
                count -= 1
    finally:
        os.close(directory)
    return removed


async def run_process(
    request_id: str,
    command: str,
    cwd: Path,
    environment: dict[str, str],
    stdin_text: str | None,
    max_output: int,
    timeout: int,
    *,
    work_root: Path,
    on_started: Callable[[int], None] | None = None,
) -> dict[str, object]:
    """Run ``bash -lc command`` in its own process group and wait for it.

    Returns captured output and the exit code. ``timed_out`` is set when the timeout expired;
    the process group is killed on timeout and on cancellation of the awaiting task.
    """
    log = OutputLog(work_root, request_id)
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
    if on_started is not None:
        on_started(process.pid)
    stdout, stderr = OutputBuffer(max_output), OutputBuffer(max_output)
    tasks = (
        asyncio.create_task(_read_stream(process.stdout, stdout, log)),
        asyncio.create_task(_read_stream(process.stderr, stderr, log)),
        asyncio.create_task(_write_stdin(process, stdin_text)),
    )
    timed_out = False
    try:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=timeout)
        await process.wait()
    except TimeoutError:
        timed_out = True
        await kill_group(process)
        await asyncio.gather(*tasks, return_exceptions=True)
    except BaseException:
        await kill_group(process)
        await asyncio.gather(*tasks, return_exceptions=True)
        log.close()
        raise
    finally:
        log.close()
    prune_logs(work_root, keep=f"{request_id}.log")
    return {
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "stdout": stdout,
        "stderr": stderr,
        "log_path": log.relative if log.opened else None,
        "log_bytes": log.bytes_written,
        "log_truncated": log.truncated,
    }


async def kill_group(process: asyncio.subprocess.Process) -> None:
    """Terminate a command's process group, escalating to SIGKILL after two seconds."""
    if process.returncode is not None:
        _kill_quietly(process.pid, signal.SIGKILL)
        return
    _kill_quietly(process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), timeout=2)
    except TimeoutError:
        _kill_quietly(process.pid, signal.SIGKILL)
        await process.wait()
    _kill_quietly(process.pid, signal.SIGKILL)


def _kill_quietly(pgid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


async def _read_stream(
    stream: asyncio.StreamReader | None, buffer: OutputBuffer, log: OutputLog
) -> None:
    while stream is not None and (chunk := await stream.read(16_384)):
        buffer.append(chunk)
        log.write(chunk)


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


def sweep_processes(keep: set[int]) -> int:
    """Kill every process in this container except ``keep`` (the server and its ancestors).

    Only the dedicated runner container calls this: it stops daemons a command left behind
    (``setsid``, double forks) from outliving the command and its checkpoint.
    """
    killed = 0
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) in keep:
            continue
        try:
            os.kill(int(entry), signal.SIGKILL)
            killed += 1
        except (ProcessLookupError, PermissionError):
            continue
    return killed


def ancestors(pid: int) -> set[int]:
    """``pid`` and every ancestor process, read from ``/proc``."""
    chain = {pid}
    current = pid
    while current > 1:
        try:
            with open(f"/proc/{current}/stat", encoding="ascii", errors="replace") as handle:
                fields = handle.read().rsplit(")", 1)[1].split()
            current = int(fields[1])
        except (OSError, IndexError, ValueError):
            break
        chain.add(current)
    return chain
