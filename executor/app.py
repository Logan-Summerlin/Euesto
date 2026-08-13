from __future__ import annotations

import hmac
import json
import platform
import shutil
import time
import uuid

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from shared.tools import PublishManifest, PublishOperation, ToolRequest, ToolResult

from .checkpoints import discard_staging
from .config import ExecutorConfig
from .errors import classify_error
from .permissions import enforce_capability
from .staging import Snapshot, load_snapshot, seed_staging, workspace_changes
from .tools import MAX_READ_BYTES, bash, edit, find, grep, ls, read, write
from .tools.bash import cancel as cancel_bash
from .tools.bash import events as bash_events

DEVELOPER_EXECUTABLE_CANDIDATES = ("python", "python3", "pytest", "ruff", "node", "npm", "pnpm", "yarn", "make", "gcc", "g++", "clang", "go", "cargo", "rustc", "java", "javac")


class ExecutorService:
    def __init__(self, config: ExecutorConfig, *, seed: bool = True):
        self.config = config
        self.snapshot = seed_staging(config) if seed else load_snapshot(config.work_root)

    async def execute(self, request: ToolRequest) -> ToolResult:
        started = time.perf_counter()
        try:
            enforce_capability(request)
            root = self.config.source_root if request.mode == "plan" else self.config.work_root
            if request.tool == "read":
                output, data = read(root, request.arguments, max_bytes=self.config.max_file_bytes)
            elif request.tool == "write":
                output, data = write(root, request.arguments, max_bytes=self.config.max_output_bytes, max_checkpoint_files=self.config.max_files, max_checkpoint_bytes=self.config.max_total_bytes)
            elif request.tool == "edit":
                output, data = edit(root, request.arguments, max_bytes=self.config.max_output_bytes, max_checkpoint_files=self.config.max_files, max_checkpoint_bytes=self.config.max_total_bytes)
            elif request.tool == "bash":
                output, data = await bash(request.request_id, root, request.arguments, max_seconds=self.config.max_command_seconds, max_output=self.config.max_output_bytes, max_checkpoint_files=self.config.max_files, max_checkpoint_bytes=self.config.max_total_bytes)
            elif request.tool == "grep":
                output, data = grep(root, request.arguments, max_bytes=self.config.max_file_bytes)
            elif request.tool == "find":
                output, data = find(root, request.arguments)
            elif request.tool == "ls":
                output, data = ls(root, request.arguments)
            else:
                raise ValueError(f"Unknown tool: {request.tool}")
            return _success_result(request.request_id, output, data, time.perf_counter() - started)
        except Exception as exc:
            classified = classify_error(exc)
            return ToolResult(request.request_id, False, output=classified.message, error_code=classified.code, elapsed_seconds=time.perf_counter() - started)

    def manifest(self, run_id: str, approval_id: str) -> PublishManifest:
        operations: list[PublishOperation] = []
        for change in workspace_changes(self.snapshot, self.config.work_root):
            content = None
            if change.operation != "delete":
                try:
                    content = (self.config.work_root / change.path).read_text(encoding="utf-8")
                except UnicodeError as exc:
                    raise ValueError("Changed binary or invalid UTF-8 files cannot be published by the text broker") from exc
            operations.append(PublishOperation(change.path, change.operation, change.base_sha256, change.staged_sha256, content))
        return PublishManifest(str(uuid.uuid4()), run_id, self.config.workspace_id, self.snapshot.snapshot_id, approval_id, tuple(operations))

    def discard(self) -> Snapshot:
        self.snapshot = discard_staging(self.config)
        return self.snapshot


def _success_result(request_id: str, output: str, data: dict, elapsed: float) -> ToolResult:
    returned = data.get("returned")
    if returned is None: returned = data.get("count", data.get("matches_returned"))
    return ToolResult(request_id, True, output, data, truncated=bool(data.get("truncated")), elapsed_seconds=elapsed, returned=_nonnegative_int(returned), total_known=_nonnegative_int(data.get("total_known")), limit=_nonnegative_int(data.get("limit")), next_cursor=(str(data["next_cursor"]) if data.get("next_cursor") else None))


def _nonnegative_int(value: object) -> int | None:
    if value is None: return None
    try: return max(0, int(value))
    except (TypeError, ValueError): return None


def create_app(config: ExecutorConfig | None = None, service: ExecutorService | None = None) -> Starlette:
    resolved = config or ExecutorConfig.from_environment()
    executor = service or ExecutorService(resolved)
    nonces: dict[str, float] = {}

    async def authenticate(request: Request) -> JSONResponse | None:
        authorization = request.headers.get("authorization", "")
        nonce = request.headers.get("x-executor-nonce", "")
        if not authorization.startswith("Bearer ") or not hmac.compare_digest(authorization[7:], resolved.token): return _error("auth.invalid", "Invalid executor credential.", 401)
        if len(nonce) < 20 or nonce in nonces: return _error("auth.replay", "Missing or replayed executor nonce.", 401)
        now = time.monotonic(); nonces[nonce] = now
        for value, created in list(nonces.items()):
            if now - created > 300: nonces.pop(value, None)
        return None

    async def status(request: Request):
        denied = await authenticate(request)
        return denied or JSONResponse({"ready": True, "workspace_id": resolved.workspace_id, "snapshot_id": executor.snapshot.snapshot_id, "tools": sorted(("bash", "edit", "find", "grep", "ls", "read", "write")), "environment": _environment_context(resolved, executor.snapshot)})

    async def tool(request: Request):
        denied = await authenticate(request)
        if denied: return denied
        try:
            data = await _json(request)
            result = await executor.execute(ToolRequest.from_dict(data))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            return _error("request.invalid", str(exc), 422)
        return JSONResponse(result.to_dict())

    async def manifest(request: Request):
        denied = await authenticate(request)
        if denied: return denied
        try:
            data = await _json(request); result = executor.manifest(str(data.get("run_id") or ""), str(data.get("approval_id") or ""))
        except (OSError, UnicodeError, ValueError) as exc: return _error("manifest.invalid", str(exc), 422)
        return JSONResponse(result.to_dict())

    async def cancel(request: Request):
        denied = await authenticate(request)
        if denied: return denied
        return JSONResponse({"cancelled": await cancel_bash(request.path_params["request_id"])})

    async def command_events(request: Request):
        denied = await authenticate(request)
        if denied: return denied
        try: after = max(0, int(request.query_params.get("after", "0")))
        except ValueError: return _error("request.invalid_cursor", "after must be an integer", 422)
        return JSONResponse(bash_events(request.path_params["request_id"], after))

    async def discard(request: Request):
        denied = await authenticate(request)
        if denied: return denied
        try: snapshot = executor.discard()
        except (OSError, ValueError, RuntimeError) as exc: return _error("staging.discard_failed", str(exc), 409)
        return JSONResponse({"snapshot_id": snapshot.snapshot_id, "file_count": snapshot.file_count})

    app = Starlette(routes=[Route("/v1/status", status, methods=["GET"]), Route("/v1/tools", tool, methods=["POST"]), Route("/v1/tools/{request_id:str}/events", command_events, methods=["GET"]), Route("/v1/manifest", manifest, methods=["POST"]), Route("/v1/staging/discard", discard, methods=["POST"]), Route("/v1/tools/{request_id:str}/cancel", cancel, methods=["POST"])])
    app.state.executor = executor
    return app


async def _json(request: Request) -> dict:
    data = await request.json()
    if not isinstance(data, dict): raise ValueError("Request body must be an object")
    return data


def _error(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message, "retryable": False, "details": {}}}, status_code=status)


def _environment_context(config: ExecutorConfig, snapshot: Snapshot) -> dict[str, object]:
    available = [name for name in DEVELOPER_EXECUTABLE_CANDIDATES if shutil.which(name, path="/usr/local/bin:/usr/bin:/bin") is not None]
    try: unpublished_changes = bool(workspace_changes(snapshot, config.work_root))
    except (OSError, ValueError): unpublished_changes = True
    try: headroom = shutil.disk_usage(config.work_root).free
    except OSError: headroom = None
    return {"capability_schema_version": 2, "mode": "plan_reads_source_agent_uses_ephemeral_staging", "workspace_root": ".", "platform": platform.system().casefold(), "python_version": platform.python_version(), "network_access": False, "gpu_access": False, "command_style": "argv_only", "developer_executables": available, "agent_snapshot": {"snapshot_id": snapshot.snapshot_id, "file_count": len(snapshot.hashes), "total_bytes": snapshot.total_bytes}, "workspace_empty": snapshot.empty, "source_snapshot_id": snapshot.snapshot_id, "staging_lifetime": "same_executor_instance", "unpublished_changes": unpublished_changes, "storage_headroom_bytes": headroom, "limits": {"max_read_bytes": min(config.max_file_bytes, MAX_READ_BYTES), "max_output_bytes": config.max_output_bytes, "max_command_seconds": config.max_command_seconds, "max_files": config.max_files, "max_total_bytes": config.max_total_bytes}}
