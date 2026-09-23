"""The gateway's executor client speaks HTTP over the executor's Unix socket."""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from server.executor.client import ExecutorClient, ExecutorUnavailable
from shared.tools import ToolRequest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the executor socket is a Linux container interface")


def test_client_round_trips_over_a_unix_socket() -> None:
    seen: list[tuple[str, str, str | None]] = []

    async def handler(request: Request) -> JSONResponse:
        seen.append((request.method, request.url.path, request.headers.get("authorization")))
        if request.url.path == "/v1/tools":
            body = await request.json()
            return JSONResponse({"request_id": body["request_id"], "ok": True, "output": "hi"})
        if request.url.path.endswith("/cancel"):
            return JSONResponse({}, status_code=500)
        return JSONResponse({"workspace_id": "workspace"})

    app = Starlette(routes=[Route("/{path:path}", handler, methods=["GET", "POST"])])

    async def scenario() -> tuple[dict, str, dict]:
        # Unix socket paths are length-limited, so keep the directory short.
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            socket = Path(directory) / "executor.sock"
            server = uvicorn.Server(uvicorn.Config(app, uds=str(socket), log_level="error"))
            task = asyncio.create_task(server.serve())
            while not server.started:
                await asyncio.sleep(0.01)
            client = ExecutorClient(socket, "token")
            try:
                status = await client.status()
                result = await client.execute(ToolRequest("r1", "run", "read", "plan", {"path": "a"}))
                discarded = await client.discard_staging()
                await client.cancel("r1")  # cancellation is best-effort: errors are not raised
                with pytest.raises(ExecutorUnavailable):
                    await ExecutorClient(Path(directory) / "missing.sock", "token").status()
            finally:
                server.should_exit = True
                await task
            return status, result.output, discarded

    assert asyncio.run(scenario()) == ({"workspace_id": "workspace"}, "hi", {"workspace_id": "workspace"})
    assert [(method, path) for method, path, _ in seen] == [
        ("GET", "/v1/status"), ("POST", "/v1/tools"), ("POST", "/v1/staging/discard"), ("POST", "/v1/tools/r1/cancel"),
    ]
    assert {auth for _, _, auth in seen} == {"Bearer token"}
