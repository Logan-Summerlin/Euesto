"""The runner command server's protocol and bounds (plan item 2.1)."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from runner.server import MAX_REQUEST_BYTES, RequestError, RunnerServer, parse_request

pytestmark = pytest.mark.posix


def request(**overrides: object) -> bytes:
    value: dict[str, object] = {
        "op": "run",
        "request_id": "r1",
        "command": "echo hi",
        "cwd": ".",
        "env": {"PATH": "/usr/bin:/bin"},
        "stdin": None,
        "timeout": 10,
        "max_output": 1_000,
    }
    value.update(overrides)
    return json.dumps(value).encode() + b"\n"


@pytest.mark.parametrize(
    "line",
    [
        b"",
        b"not json\n",
        b"[]\n",
        request(extra=1),
        request(op="exec"),
        request(request_id=""),
        request(command="  "),
        request(command="a\x00b"),
        request(cwd="/etc"),
        request(env={"A": 1}),
        request(env={f"V{index}": "x" for index in range(200)}),
        request(stdin=5),
        request(timeout=0),
        request(timeout=10_000),
        request(timeout=True),
        request(max_output=0),
        b"x" * (MAX_REQUEST_BYTES + 1),
    ],
)
def test_malformed_or_unbounded_requests_are_refused(line: bytes) -> None:
    with pytest.raises(RequestError):
        parse_request(line)


def test_cancel_requests_need_only_an_id() -> None:
    assert parse_request(b'{"op": "cancel", "request_id": "r1"}') == {
        "op": "cancel",
        "request_id": "r1",
    }


async def _exchange(socket_path: Path, payload: bytes) -> list[dict]:
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    writer.write(payload)
    await writer.drain()
    messages = []
    while line := await reader.readline():
        messages.append(json.loads(line))
    writer.close()
    return messages


def _serve(tmp_path: Path, scenario) -> object:
    async def main():
        socket_path = tmp_path / "runner.sock"
        work = tmp_path / "work"
        work.mkdir(exist_ok=True)
        state = RunnerServer(work)
        server = await asyncio.start_unix_server(state.handle, path=str(socket_path))
        try:
            return await scenario(socket_path, work)
        finally:
            server.close()
            await server.wait_closed()

    return asyncio.run(main())


def test_a_run_reports_start_then_a_bounded_result_and_writes_the_log(tmp_path: Path) -> None:
    async def scenario(socket_path: Path, work: Path):
        return await _exchange(
            socket_path, request(command="head -c 5000 /dev/zero | tr '\\0' a; echo e >&2")
        )

    started, result = _serve(tmp_path, scenario)
    assert started["event"] == "started" and started["pid"] > 0
    assert result["event"] == "result" and result["exit_code"] == 0
    assert result["stdout_bytes"] == 5_000 and result["stdout_truncated"] is True
    assert len(result["stdout"]) < 5_000
    assert result["log_path"] == ".euesto-output/r1.log"
    assert (tmp_path / "work" / ".euesto-output" / "r1.log").stat().st_size == 5_002


def test_a_second_run_while_one_is_active_is_refused(tmp_path: Path) -> None:
    async def scenario(socket_path: Path, work: Path):
        first = asyncio.create_task(_exchange(socket_path, request(command="sleep 1")))
        await asyncio.sleep(0.3)
        second = await _exchange(socket_path, request(request_id="r2"))
        await first
        return second

    [refused] = _serve(tmp_path, scenario)
    assert refused == {
        "event": "error",
        "code": "runner.busy",
        "message": "A command is running.",
    }


def test_cancel_stops_the_active_run_and_ignores_others(tmp_path: Path) -> None:
    async def scenario(socket_path: Path, work: Path):
        run = asyncio.create_task(_exchange(socket_path, request(command="sleep 30")))
        await asyncio.sleep(0.3)
        unknown = await _exchange(socket_path, b'{"op":"cancel","request_id":"other"}\n')
        cancelled = await _exchange(socket_path, b'{"op":"cancel","request_id":"r1"}\n')
        return unknown, cancelled, await asyncio.wait_for(run, timeout=10)

    unknown, cancelled, (_started, result) = _serve(tmp_path, scenario)
    assert unknown == [{"cancelled": False}] and cancelled == [{"cancelled": True}]
    assert result["cancelled"] is True and result["exit_code"] != 0


def test_a_timeout_kills_the_process_group(tmp_path: Path) -> None:
    async def scenario(socket_path: Path, work: Path):
        return await _exchange(
            socket_path, request(command="(sleep 30 &); sleep 30", timeout=1)
        )

    _started, result = _serve(tmp_path, scenario)
    assert result["timed_out"] is True


def test_working_directories_outside_the_work_root_are_refused(tmp_path: Path) -> None:
    (tmp_path / "outside").mkdir()

    async def scenario(socket_path: Path, work: Path):
        os.symlink(tmp_path / "outside", work / "escape")
        return await _exchange(socket_path, request(cwd="escape"))

    [refused] = _serve(tmp_path, scenario)
    assert refused["event"] == "error" and refused["code"] == "runner.invalid"
