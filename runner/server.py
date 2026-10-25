"""The runner's command server: JSON lines over a Unix socket shared only with the executor.

Protocol (one request per connection, newline-delimited JSON):

* ``{"op": "run", "request_id", "command", "cwd", "env", "stdin", "timeout", "max_output"}``
  answers ``{"event": "started", "pid"}`` once the process exists, then
  ``{"event": "result", ...}`` when it ends. Only one run is active at a time; a second
  request while one runs is refused with ``{"event": "error", "code": "runner.busy"}``.
* ``{"op": "cancel", "request_id"}`` answers ``{"cancelled": bool}``.
* ``{"op": "probe", "request_id", "names"}`` answers ``{"executables": [...]}``: which of
  ``names`` the toolchain image provides on the fixed command ``PATH``.

With ``--cache-dir``, commands get package-manager cache locations on that volume unless the
request already sets them, so downloads and builds survive between commands and sessions.

Commands may reach this socket themselves (it lives in their container), which grants nothing
they could not already do: they run as the same user, the server refuses concurrent runs, and
every process is killed when a run ends.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import signal
from pathlib import Path

from .process import (
    MAX_RETAINED_OUTPUT_BYTES,
    ancestors,
    run_process,
    sweep_processes,
)

MAX_REQUEST_BYTES = 4_000_000
MAX_TIMEOUT_SECONDS = 900
MAX_ENV_VARS = 128
MAX_PROBE_NAMES = 64
COMMAND_PATH = "/usr/local/bin:/usr/bin:/bin"
REQUEST_FIELDS = frozenset(
    {"op", "request_id", "command", "cwd", "env", "stdin", "timeout", "max_output", "names"}
)
_EXECUTABLE_NAME = re.compile(r"^[A-Za-z0-9._+-]{1,64}$")


def cache_environment(cache_dir: Path) -> dict[str, str]:
    """Package-manager cache locations beneath the runner's cache volume."""
    root = os.fspath(cache_dir)
    return {
        "XDG_CACHE_HOME": root,
        "PIP_CACHE_DIR": f"{root}/pip",
        "UV_CACHE_DIR": f"{root}/uv",
        "npm_config_cache": f"{root}/npm",
        "YARN_CACHE_FOLDER": f"{root}/yarn",
        "COREPACK_HOME": f"{root}/corepack",
        "GOPATH": f"{root}/go",
        "GOCACHE": f"{root}/go-build",
        "CARGO_HOME": f"{root}/cargo",
        "GRADLE_USER_HOME": f"{root}/gradle",
    }


class RequestError(ValueError):
    pass


class RunnerServer:
    def __init__(
        self, work_root: Path, *, sweep: bool = False, cache_dir: Path | None = None
    ) -> None:
        self.work_root = work_root.resolve(strict=True)
        self.sweep = sweep
        self.defaults = cache_environment(cache_dir) if cache_dir is not None else {}
        self._active: str | None = None
        self._pid: int | None = None
        self._task: asyncio.Task[dict[str, object]] | None = None
        self._cancelled: set[str] = set()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await reader.readline()
            try:
                request = parse_request(line)
            except RequestError as exc:
                await _send(
                    writer, {"event": "error", "code": "runner.invalid", "message": str(exc)}
                )
                return
            if request["op"] == "cancel":
                await _send(writer, {"cancelled": await self.cancel(request["request_id"])})
                return
            if request["op"] == "probe":
                await _send(writer, {"executables": probe(request["names"])})
                return
            await self._run(request, writer)
        except (ConnectionError, asyncio.IncompleteReadError):
            return
        finally:
            writer.close()

    async def _run(self, request: dict, writer: asyncio.StreamWriter) -> None:
        if self._active is not None:
            await _send(
                writer,
                {"event": "error", "code": "runner.busy", "message": "A command is running."},
            )
            return
        request_id = request["request_id"]
        self._active = request_id
        self._cancelled.discard(request_id)
        started: asyncio.Queue[int] = asyncio.Queue()
        try:
            cwd = self._working_directory(request["cwd"])
            self._task = asyncio.create_task(
                run_process(
                    request_id,
                    request["command"],
                    cwd,
                    {**self.defaults, **request["env"]},
                    request["stdin"],
                    request["max_output"],
                    request["timeout"],
                    work_root=self.work_root,
                    on_started=started.put_nowait,
                )
            )
            waiter = asyncio.create_task(started.get())
            await asyncio.wait({self._task, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if waiter.done():
                self._pid = waiter.result()
                await _send(writer, {"event": "started", "pid": self._pid})
            else:
                waiter.cancel()
            outcome = await self._task
            stdout, stderr = outcome["stdout"], outcome["stderr"]
            await _send(
                writer,
                {
                    "event": "result",
                    "exit_code": outcome["exit_code"],
                    "timed_out": outcome["timed_out"],
                    "cancelled": request_id in self._cancelled,
                    "stdout": stdout.bytes().decode("utf-8", errors="replace"),
                    "stderr": stderr.bytes().decode("utf-8", errors="replace"),
                    "stdout_bytes": stdout.total,
                    "stderr_bytes": stderr.total,
                    "stdout_truncated": stdout.truncated,
                    "stderr_truncated": stderr.truncated,
                    "log_path": outcome["log_path"],
                    "log_bytes": outcome["log_bytes"],
                    "log_truncated": outcome["log_truncated"],
                },
            )
        except RequestError as exc:
            await _send(
                writer, {"event": "error", "code": "runner.invalid", "message": str(exc)}
            )
        except OSError as exc:
            await _send(
                writer, {"event": "error", "code": "runner.failed", "message": str(exc)}
            )
        finally:
            if self._task is not None and not self._task.done():
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
            if self.sweep:
                sweep_processes(ancestors(os.getpid()))
            self._active = None
            self._pid = None
            self._task = None
            self._cancelled.discard(request_id)

    async def cancel(self, request_id: str) -> bool:
        if request_id != self._active or self._pid is None or self._task is None:
            return False
        self._cancelled.add(request_id)
        try:
            os.killpg(self._pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return True
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout=2)
        except TimeoutError:
            try:
                os.killpg(self._pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        except Exception:
            pass
        return True

    def _working_directory(self, relative: str) -> Path:
        candidate = (self.work_root / relative).resolve(strict=True)
        if not candidate.is_relative_to(self.work_root) or not candidate.is_dir():
            raise RequestError("working directory is outside the work root")
        return candidate


def parse_request(line: bytes) -> dict:
    """Validate the shape of one request; the executor has already validated its content."""
    if not line or len(line) > MAX_REQUEST_BYTES:
        raise RequestError("request is empty or too large")
    try:
        request = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RequestError("request is not JSON") from exc
    if not isinstance(request, dict) or set(request) - REQUEST_FIELDS:
        raise RequestError("request has unknown fields")
    request_id = request.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        raise RequestError("request_id is required")
    if request.get("op") == "cancel":
        return {"op": "cancel", "request_id": request_id}
    if request.get("op") == "probe":
        names = request.get("names")
        if (
            not isinstance(names, list)
            or len(names) > MAX_PROBE_NAMES
            or not all(
                isinstance(name, str) and _EXECUTABLE_NAME.fullmatch(name) for name in names
            )
        ):
            raise RequestError("names must be a bounded list of executable names")
        return {"op": "probe", "request_id": request_id, "names": names}
    if request.get("op") != "run":
        raise RequestError("op must be run, cancel, or probe")
    command = request.get("command")
    if not isinstance(command, str) or not command.strip() or "\x00" in command:
        raise RequestError("command must be non-empty text")
    cwd = request.get("cwd", ".")
    if not isinstance(cwd, str) or cwd.startswith("/") or "\x00" in cwd:
        raise RequestError("cwd must be a relative path")
    env = request.get("env")
    if (
        not isinstance(env, dict)
        or len(env) > MAX_ENV_VARS
        or not all(
            isinstance(key, str) and isinstance(value, str) and "\x00" not in key + value
            for key, value in env.items()
        )
    ):
        raise RequestError("env must be a bounded string map")
    stdin = request.get("stdin")
    if stdin is not None and (not isinstance(stdin, str) or "\x00" in stdin):
        raise RequestError("stdin must be text")
    timeout = request.get("timeout")
    max_output = request.get("max_output")
    for name, value, ceiling in (
        ("timeout", timeout, MAX_TIMEOUT_SECONDS),
        ("max_output", max_output, MAX_RETAINED_OUTPUT_BYTES * 16),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= ceiling:
            raise RequestError(f"{name} is out of range")
    return {
        "op": "run",
        "request_id": request_id,
        "command": command,
        "cwd": cwd,
        "env": env,
        "stdin": stdin,
        "timeout": timeout,
        "max_output": max_output,
    }


def probe(names: list[str]) -> list[str]:
    """The requested executables present on the fixed command ``PATH``, in request order."""
    return [name for name in names if shutil.which(name, path=COMMAND_PATH) is not None]


async def _send(writer: asyncio.StreamWriter, message: dict) -> None:
    writer.write(json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n")
    await writer.drain()


async def serve(
    socket_path: Path, work_root: Path, *, sweep: bool, cache_dir: Path | None = None
) -> None:
    server_state = RunnerServer(work_root, sweep=sweep, cache_dir=cache_dir)
    socket_path.unlink(missing_ok=True)
    server = await asyncio.start_unix_server(
        server_state.handle, path=os.fspath(socket_path), limit=MAX_REQUEST_BYTES + 1
    )
    os.chmod(socket_path, 0o660)
    async with server:
        await server.serve_forever()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Euesto runner command server")
    parser.add_argument("--socket", type=Path, default=Path("/run/runner/runner.sock"))
    parser.add_argument("--work-root", type=Path, default=Path("/work"))
    parser.add_argument(
        "--sweep",
        action="store_true",
        help="kill every other process in the container after each command",
    )
    parser.add_argument(
        "--cache-dir", type=Path, default=None, help="package-manager cache volume"
    )
    arguments = parser.parse_args(argv)
    os.umask(0o007)
    asyncio.run(
        serve(
            arguments.socket,
            arguments.work_root,
            sweep=arguments.sweep,
            cache_dir=arguments.cache_dir,
        )
    )


if __name__ == "__main__":
    main()
