from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import platform
import re
import shutil
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from shared.coercion import optional_nonnegative_int
from shared.tools import (
    EXECUTOR_TOOLS,
    MUTATION_TOOLS,
    PublicationReceipt,
    PublishManifest,
    PublishOperation,
    ToolRequest,
    ToolResult,
)

from . import sandbox_git
from .checkpoints import checkpoint_files, configure_store, restore_checkpoint
from .config import ExecutorConfig
from .drift import rebase
from .egress import egress_status
from .errors import INVALID_ARGUMENTS, STAGING_CONFLICT, ExecutorToolError, classify_error
from .paths import configure_secret_patterns, matches_publish_glob, normalize_relative
from .permissions import enforce_capability
from .snapshot import agents_md, workspace_snapshot
from .staging import (
    Snapshot,
    WorkspaceChange,
    advance_published_staging,
    clear_staging,
    publication_batches,
    refresh_visible_files,
    seed_staging,
    staging_links,
    visible_files,
    workspace_changes,
)
from .state import Baseline, Conflict, StateStore
from .tools import apply_patch, bash, edit, find, grep, ls, read, status, write
from .tools.apply_patch import apply_patch_paths
from .tools.bash import cancel as cancel_bash
from .tools.bash import configure_runner, probe_runner

DEVELOPER_EXECUTABLE_CANDIDATES = (
    "python",
    "python3",
    "python3.12",
    "python3.13",
    "pip",
    "uv",
    "pytest",
    "ruff",
    "node",
    "npm",
    "npx",
    "corepack",
    "pnpm",
    "yarn",
    "make",
    "gcc",
    "g++",
    "clang",
    "go",
    "cargo",
    "rustc",
    "java",
    "javac",
    "git",
    "rg",
    "fd",
    "jq",
    "ctags",
    "sqlite3",
)


ToolRunner = Callable[[ToolRequest, Path], Awaitable[tuple[str, dict]]]

# Declared checks (harness plan 3.4) run like Bash in the runner and always restore staging.
CHECK_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
DEFAULT_CHECK_SECONDS = 300
MAX_CHECK_OUTPUT_CHARS = 16_000
MAX_MANIFEST_PATHS = 10_000


def staged_fingerprint(files: dict[str, tuple[str, int, int]]) -> str:
    """The staged snapshot's identity: a digest of every visible file's path, content hash,
    and mode. Any later mutation changes it, so results tied to it go stale."""
    digest = hashlib.sha256()
    for path in sorted(files):
        content, _size, mode = files[path]
        digest.update(f"{path}\0{content}\0{mode:o}\n".encode())
    return digest.hexdigest()


class ExecutorService:
    """The control plane: file tools, status, checkpoints, baselines, and manifests.

    Staging persists on the work volume and the baseline on the state volume, so a restart
    resumes where it left off; host changes made meanwhile are absorbed by a rebase, and
    files changed on both sides are held back as conflicts.
    """

    def __init__(self, config: ExecutorConfig):
        self.config = config
        self.state = StateStore(config.state_directory, config.workspace_id)
        configure_store(config.work_root, self.state.checkpoints)
        configure_runner(config.runner_socket)
        configure_secret_patterns(config.secret_patterns)
        self.conflicts: dict[str, Conflict] = {}
        self.last_rebase: dict[str, object] = {}
        # What the runner's toolchain image provides; probed once the runner answers.
        self.runner_executables: list[str] | None = None
        claimed = self.state.claim()
        baseline = self.state.load() if claimed else None
        if baseline is not None and not config.work_root.is_dir():
            # The work volume was removed while the state volume survived: reseed.
            self.state.forget_baseline()
            baseline = None
        self.resumed = baseline is not None
        if baseline is not None:
            self.snapshot, self.conflicts = baseline.snapshot, baseline.conflicts
            self.git = sandbox_git.repository_status(self.state.root, config.git_history_depth)
            self.rebase()
        else:
            # No baseline means whatever the work volume holds is unaccounted for (an
            # interrupted seed, or a state volume that was removed): start over from the host.
            clear_staging(config.work_root)
            self._seed()
        self._tools = self._dispatch_table()

    def _seed(self) -> None:
        self.snapshot = seed_staging(self.config)
        self.conflicts = {}
        self._save()
        self.git = sandbox_git.seed_repository(
            self.config.source_root,
            self.config.work_root,
            self.state.root,
            self.snapshot,
            self.config.git_history_depth,
        )

    def _save(self) -> None:
        self.state.save(Baseline(self.snapshot, self.conflicts))

    def _baseline_changed(self) -> None:
        self._save()
        if self.git.get("available"):
            self.git = sandbox_git.update_base(
                self.config.work_root, self.state.root, self.snapshot
            )

    def rebase(
        self,
        keep_staged: list[str] | tuple[str, ...] = (),
        take_host: list[str] | tuple[str, ...] = (),
    ) -> dict[str, object]:
        """Absorb host changes made since the baseline; optionally resolve conflicts."""
        baseline, report = rebase(
            self.config,
            Baseline(self.snapshot, self.conflicts),
            keep_staged=keep_staged,
            take_host=take_host,
        )
        changed = baseline.snapshot.snapshot_id != self.snapshot.snapshot_id
        conflicts_changed = baseline.conflicts != self.conflicts
        self.snapshot, self.conflicts = baseline.snapshot, baseline.conflicts
        if changed:
            self._baseline_changed()
        elif conflicts_changed:
            self._save()
        self.last_rebase = report.to_dict()
        return {
            **self.last_rebase,
            "snapshot_id": self.snapshot.snapshot_id,
            "open_conflicts": [item.to_dict() for item in self.conflicts.values()],
        }

    async def execute(self, request: ToolRequest) -> ToolResult:
        started = time.perf_counter()
        try:
            enforce_capability(request)
            root = self.config.source_root if request.mode == "plan" else self.config.work_root
            run = self._tools.get(request.tool)
            if run is None:
                raise ExecutorToolError(INVALID_ARGUMENTS, f"Unknown tool: {request.tool}")
            output, data = await run(request, root)
            if request.mode == "agent" and request.tool in MUTATION_TOOLS:
                data["workspace_status"] = self.workspace_status(
                    self._post_mutation_files(request, data)
                )
                output = f"{output} {data['workspace_status']['summary']}"
            return _success_result(
                request.request_id, output, data, time.perf_counter() - started
            )
        except Exception as exc:
            classified = classify_error(exc)
            return ToolResult(
                request.request_id,
                False,
                output=classified.message,
                data=dict(classified.details or {}),
                error_code=classified.code,
                elapsed_seconds=time.perf_counter() - started,
            )

    def _dispatch_table(self) -> dict[str, ToolRunner]:
        """One entry per executor tool, passing only that operation's effective limits.

        Read-only tools run off the event loop so independent calls proceed concurrently;
        mutations run inline and stay serialized in call order.
        """
        config = self.config
        limit = config.effective_limit
        checkpoint = {
            "max_checkpoint_files": config.max_staged_files,
            "max_checkpoint_bytes": config.max_checkpoint_bytes,
        }

        def search(name: str, request: ToolRequest) -> dict[str, int]:
            return {
                "max_results": limit(name, request.arguments.get("max_results")),
                "max_seconds": limit("max_search_seconds"),
            }

        def run_read(request: ToolRequest, root: Path) -> Awaitable[tuple[str, dict]]:
            max_bytes = limit("max_read_bytes", request.arguments.get("max_bytes"))
            return asyncio.to_thread(read, root, request.arguments, max_bytes=max_bytes)

        def run_write(request: ToolRequest, root: Path) -> Awaitable[tuple[str, dict]]:
            return _inline(
                write,
                root,
                request.arguments,
                max_bytes=limit("max_write_bytes"),
                max_staging_bytes=config.max_staging_bytes,
                **checkpoint,
            )

        def run_edit(request: ToolRequest, root: Path) -> Awaitable[tuple[str, dict]]:
            return _inline(
                edit,
                root,
                request.arguments,
                max_target_bytes=limit("max_edit_target_bytes"),
                max_result_bytes=limit("max_edit_result_bytes"),
                **checkpoint,
            )

        def run_apply_patch(request: ToolRequest, root: Path) -> Awaitable[tuple[str, dict]]:
            return _inline(
                apply_patch,
                root,
                request.arguments,
                max_operations=limit("max_patch_operations"),
                max_patch_bytes=limit("max_patch_bytes"),
                max_write_bytes=limit("max_write_bytes"),
                max_edit_target_bytes=limit("max_edit_target_bytes"),
                max_edit_result_bytes=limit("max_edit_result_bytes"),
                max_staging_bytes=config.max_staging_bytes,
                **checkpoint,
            )

        def run_bash(request: ToolRequest, root: Path) -> Awaitable[tuple[str, dict]]:
            return bash(
                request.request_id,
                root,
                request.arguments,
                max_seconds=limit("max_command_seconds"),
                max_output=limit("max_bash_output_bytes"),
                max_command_bytes=limit("max_command_bytes"),
                max_stdin_bytes=limit("max_bash_stdin_bytes"),
                **checkpoint,
            )

        def run_status(request: ToolRequest, root: Path) -> Awaitable[tuple[str, dict]]:
            return asyncio.to_thread(
                status,
                config.work_root,
                config.source_root,
                self.snapshot,
                request.arguments,
                conflicts=self.conflicts,
            )

        def run_grep(request: ToolRequest, root: Path) -> Awaitable[tuple[str, dict]]:
            return asyncio.to_thread(
                grep,
                root,
                request.arguments,
                max_scan_bytes=limit("max_grep_scan_bytes"),
                max_output_bytes=limit("max_grep_output_bytes"),
                **search("max_search_results", request),
            )

        def run_find(request: ToolRequest, root: Path) -> Awaitable[tuple[str, dict]]:
            return asyncio.to_thread(
                find, root, request.arguments, **search("max_find_results", request)
            )

        def run_ls(request: ToolRequest, root: Path) -> Awaitable[tuple[str, dict]]:
            return asyncio.to_thread(
                ls, root, request.arguments, **search("max_ls_results", request)
            )

        return {
            "read": run_read,
            "write": run_write,
            "edit": run_edit,
            "apply_patch": run_apply_patch,
            "bash": run_bash,
            "status": run_status,
            "grep": run_grep,
            "find": run_find,
            "ls": run_ls,
        }

    def _post_mutation_files(
        self, request: ToolRequest, data: dict
    ) -> dict[str, tuple[str, int, int]] | None:
        """Reuse the listing the mutation's own checkpoint just walked when the touched paths
        are known exactly (write/edit/apply_patch), instead of walking the tree a second time.
        Bash can touch anything, so it always gets a fresh walk."""
        if request.tool == "bash":
            return None
        base = checkpoint_files(str(data.get("checkpoint_id") or ""))
        if base is None:
            return None
        if request.tool == "apply_patch":
            paths = apply_patch_paths(request.arguments)
        else:
            paths = [str(data.get("path") or "")]
        return refresh_visible_files(
            self.config.work_root, base, [path for path in paths if path]
        )

    def workspace_status(
        self, current: dict[str, tuple[str, int, int]] | None = None
    ) -> dict[str, object]:
        if current is None:
            current = visible_files(self.config.work_root)
        changes = workspace_changes(self.snapshot, self.config.work_root, current)
        created = [change.path for change in changes if change.operation == "create"]
        modified = [change.path for change in changes if change.operation == "update"]
        deleted = [change.path for change in changes if change.operation == "delete"]
        return {
            "created": created,
            "modified": modified,
            "deleted": deleted,
            "permission_changes": [
                change.path for change in changes if change.permission_changed
            ],
            "unpublishable_links": staging_links(self.config.work_root),
            "conflicts": sorted(self.conflicts),
            "never_publish": self._withheld(changes),
            "fingerprint": staged_fingerprint(current),
            "staged": bool(changes),
            "publication": "pending_review" if changes else "no_changes",
            "summary": (
                f"Created {len(created)}, modified {len(modified)}, deleted {len(deleted)}; "
                "host publication pending review."
            ),
        }

    def manifest(
        self,
        run_id: str,
        approval_id: str,
        *,
        publication_id: str | None = None,
        batch_index: int = 1,
        paths: list[str] | None = None,
    ) -> PublishManifest:
        """Build the next publication batch from the changes still pending.

        ``paths`` selects a subset (partial publication, plan 4.4): only those changes are
        offered and the rest stay staged. Paths matching the workspace policy's
        ``never_publish`` globs are never offered.

        Publishing a batch advances the baseline for exactly its operations, so requesting the
        next batch recomputes what remains: a retry after a failure only re-attempts the
        unpublished remainder. ``batch_count`` is ``batch_index - 1`` plus the batches the
        current remainder needs.
        """
        if isinstance(batch_index, bool) or not isinstance(batch_index, int) or batch_index < 1:
            raise ExecutorToolError(INVALID_ARGUMENTS, "batch_index must be a positive integer")
        # A conflicting file is withheld until the user resolves it; the rest still publish.
        pending = [
            change
            for change in workspace_changes(self.snapshot, self.config.work_root)
            if change.path not in self.conflicts
            and not matches_publish_glob(change.path, self.config.never_publish)
        ]
        if paths is not None:
            selected_paths = _selected_paths(paths)
            unknown = selected_paths - {change.path for change in pending}
            # A later batch of the same selection: earlier batches already published.
            if batch_index > 1:
                unknown = set()
            if unknown:
                raise ExecutorToolError(
                    INVALID_ARGUMENTS,
                    "Not a publishable staged change: " + ", ".join(sorted(unknown)[:5]),
                )
            pending = [change for change in pending if change.path in selected_paths]
        batches = publication_batches(pending)
        selected = batches[0] if batches else []
        operations = tuple(self._publish_operation(change) for change in selected)
        manifest_id = str(uuid.uuid4())
        return PublishManifest(
            manifest_id,
            run_id,
            self.config.workspace_id,
            self.snapshot.snapshot_id,
            approval_id,
            operations,
            publication_id or manifest_id,
            batch_index,
            batch_index - 1 + max(1, len(batches)),
        )

    def _publish_operation(self, change: WorkspaceChange) -> PublishOperation:
        base = {
            "path": change.path,
            "operation": change.operation,
            "base_sha256": change.base_sha256,
            "base_mode": change.base_mode,
        }
        if change.operation == "delete":
            return PublishOperation(**base, staged_sha256=None)
        raw = (self.config.work_root / change.path).read_bytes()
        staged = {
            **base,
            "staged_sha256": change.staged_sha256,
            "staged_mode": change.staged_mode,
        }
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            # Binary or non-UTF-8 files are carried byte-for-byte; the broker copies them.
            return PublishOperation(
                **staged, content_base64=base64.b64encode(raw).decode("ascii")
            )
        return PublishOperation(**staged, content=text)

    def _withheld(self, changes: list[WorkspaceChange]) -> list[str]:
        return [
            change.path
            for change in changes
            if matches_publish_glob(change.path, self.config.never_publish)
        ]

    def review(self, arguments: dict) -> dict[str, object]:
        """Staged changes with per-file unified diffs for the desktop review pane (4.4)."""
        current = visible_files(self.config.work_root)
        arguments = {"include_diffs": True, "max_results": 100, **arguments}
        output, data = status(
            self.config.work_root,
            self.config.source_root,
            self.snapshot,
            arguments,
            current=current,
            conflicts=self.conflicts,
        )
        changes = workspace_changes(self.snapshot, self.config.work_root, current)
        return {
            **data,
            "summary": output.splitlines()[0] if output else "",
            "fingerprint": staged_fingerprint(current),
            "never_publish": self._withheld(changes),
        }

    async def run_check(self, name: str, command: str, timeout_seconds: int) -> dict:
        """Run one declared check against staging in the runner, then restore staging.

        A check verifies; it never changes what would be published. Its result is tied to the
        staged fingerprint taken before it ran.
        """
        if not isinstance(name, str) or not CHECK_NAME_PATTERN.match(name):
            raise ExecutorToolError(INVALID_ARGUMENTS, "check name must be a short identifier")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int):
            raise ExecutorToolError(INVALID_ARGUMENTS, "timeout_seconds must be an integer")
        root = self.config.work_root
        before = staged_fingerprint(await asyncio.to_thread(visible_files, root))
        limit = self.config.effective_limit
        started = time.perf_counter()
        request_id = f"check-{uuid.uuid4()}"
        try:
            output, data = await bash(
                request_id,
                root,
                {
                    "command": command,
                    "timeout_seconds": timeout_seconds,
                    "rollback_on_failure": True,
                },
                max_seconds=limit("max_command_seconds"),
                max_output=limit("max_bash_output_bytes"),
                max_command_bytes=limit("max_command_bytes"),
                max_checkpoint_files=self.config.max_staged_files,
                max_checkpoint_bytes=self.config.max_checkpoint_bytes,
            )
        except TimeoutError:
            exit_code, outcome, output, data = None, "timed_out", "", {}
        else:
            exit_code = int(data.get("exit_code", -1))
            outcome = "passed" if exit_code == 0 else "failed"
            if not data.get("rolled_back") and data.get("checkpoint_id"):
                await asyncio.to_thread(restore_checkpoint, root, str(data["checkpoint_id"]))
        after = staged_fingerprint(await asyncio.to_thread(visible_files, root))
        reduction = data.get("reduction") if isinstance(data.get("reduction"), dict) else None
        return {
            "name": name,
            "command": command,
            "outcome": outcome,
            "exit_code": exit_code,
            "fingerprint": before,
            "restored": after == before,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "output": output[-MAX_CHECK_OUTPUT_CHARS:],
            "output_truncated": len(output) > MAX_CHECK_OUTPUT_CHARS,
            "reduction": reduction,
        }

    def discard(self) -> Snapshot:
        """Drop every staged change, log, checkpoint, and the sandbox repository; reseed."""
        clear_staging(self.config.work_root)
        shutil.rmtree(self.state.checkpoints, ignore_errors=True)
        configure_store(self.config.work_root, self.state.checkpoints)
        self.state.forget_baseline()
        self._seed()
        return self.snapshot

    def mark_published(self, manifest: PublishManifest | PublicationReceipt) -> Snapshot:
        if manifest.workspace_id != self.config.workspace_id:
            raise ExecutorToolError(
                INVALID_ARGUMENTS, "Publication manifest belongs to another workspace"
            )
        if manifest.source_snapshot_id != self.snapshot.snapshot_id:
            raise ExecutorToolError(
                STAGING_CONFLICT,
                "Publication manifest is stale for the current staging baseline",
            )
        self.snapshot = advance_published_staging(
            self.config.work_root, self.snapshot, manifest.operations
        )
        self._baseline_changed()
        return self.snapshot


def _selected_paths(paths: object) -> set[str]:
    if (
        not isinstance(paths, list)
        or not paths
        or len(paths) > MAX_MANIFEST_PATHS
        or not all(isinstance(item, str) for item in paths)
    ):
        raise ExecutorToolError(INVALID_ARGUMENTS, "paths must be a non-empty list of paths")
    return {normalize_relative(item) for item in paths}


async def _inline(
    function: Callable[..., tuple[str, dict]], *args: object, **kwargs: object
) -> tuple[str, dict]:
    return function(*args, **kwargs)


def _success_result(request_id: str, output: str, data: dict, elapsed: float) -> ToolResult:
    returned = data.get("returned") or data.get("count", data.get("matches_returned"))
    return ToolResult(
        request_id,
        True,
        output,
        data,
        truncated=bool(data.get("truncated")),
        elapsed_seconds=elapsed,
        returned=optional_nonnegative_int(returned),
        total_known=optional_nonnegative_int(data.get("total_known")),
        limit=optional_nonnegative_int(data.get("limit")),
        next_cursor=str(data["next_cursor"]) if data.get("next_cursor") else None,
    )


Handler = Callable[[Request], Awaitable[JSONResponse]]
NONCE_TTL_SECONDS = 300


def create_app(
    config: ExecutorConfig | None = None, service: ExecutorService | None = None
) -> Starlette:
    resolved = config or ExecutorConfig.from_environment()
    executor = service or ExecutorService(resolved)
    nonces: dict[str, float] = {}

    def authenticated(handler: Handler) -> Handler:
        """Require the bearer credential and a fresh, never-replayed nonce on every request."""

        async def endpoint(request: Request) -> JSONResponse:
            authorization = request.headers.get("authorization", "")
            nonce = request.headers.get("x-executor-nonce", "")
            if not authorization.startswith("Bearer ") or not hmac.compare_digest(
                authorization[7:], resolved.token
            ):
                return _error("auth.invalid", "Invalid executor credential.", 401)
            if len(nonce) < 20 or nonce in nonces:
                return _error("auth.replay", "Missing or replayed executor nonce.", 401)
            now = time.monotonic()
            nonces[nonce] = now
            for value, created in list(nonces.items()):
                if now - created > NONCE_TTL_SECONDS:
                    nonces.pop(value, None)
            return await handler(request)

        return endpoint

    async def status(request: Request) -> JSONResponse:
        if resolved.runner_socket is not None and executor.runner_executables is None:
            executor.runner_executables = await probe_runner(
                list(DEVELOPER_EXECUTABLE_CANDIDATES)
            )
        return JSONResponse(
            {
                "ready": True,
                "workspace_id": resolved.workspace_id,
                "snapshot_id": executor.snapshot.snapshot_id,
                "tools": sorted(EXECUTOR_TOOLS),
                "environment": _environment_context(resolved, executor),
                "workspace_status": executor.workspace_status(),
            }
        )

    async def tool(request: Request) -> JSONResponse:
        try:
            result = await executor.execute(ToolRequest.from_dict(await _json(request)))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            return _error("request.invalid", str(exc), 422)
        return JSONResponse(result.to_dict())

    async def manifest(request: Request) -> JSONResponse:
        try:
            data = await _json(request)
            result = executor.manifest(
                str(data.get("run_id") or ""),
                str(data.get("approval_id") or ""),
                publication_id=str(data.get("publication_id") or "") or None,
                batch_index=data.get("batch_index", 1),
                paths=data.get("paths"),
            )
        except (OSError, UnicodeError, ValueError) as exc:
            return _error("manifest.invalid", str(exc), 422)
        return JSONResponse(result.to_dict())

    async def mark_published(request: Request) -> JSONResponse:
        try:
            snapshot = executor.mark_published(
                PublicationReceipt.from_dict(await _json(request))
            )
        except (OSError, UnicodeError, TypeError, ValueError, RuntimeError) as exc:
            return _error("staging.mark_published_failed", str(exc), 409)
        return _snapshot_response(snapshot)

    async def snapshot(request: Request) -> JSONResponse:
        text, truncated = await asyncio.to_thread(agents_md, resolved.source_root)
        try:
            data = await asyncio.to_thread(workspace_snapshot, resolved.source_root)
        except OSError:
            data = {}
        return JSONResponse(
            {"snapshot": data, "agents_md": text, "agents_md_truncated": truncated}
        )

    async def rebase_staging(request: Request) -> JSONResponse:
        try:
            body = await _json(request) if await request.body() else {}
            fields = {"keep_staged", "take_host"}
            choices = {name: body.get(name) or [] for name in fields}
            if set(body) - fields or not all(
                isinstance(value, list) and all(isinstance(item, str) for item in value)
                for value in choices.values()
            ):
                raise ExecutorToolError(
                    INVALID_ARGUMENTS, "keep_staged and take_host must be lists of paths"
                )
            result = await asyncio.to_thread(
                executor.rebase, choices["keep_staged"], choices["take_host"]
            )
        except ExecutorToolError as exc:
            return _error(exc.code, exc.message, 422)
        except (OSError, ValueError, RuntimeError) as exc:
            return _error("staging.rebase_failed", str(exc), 409)
        return JSONResponse(result)

    async def discard(request: Request) -> JSONResponse:
        try:
            snapshot = executor.discard()
        except (OSError, ValueError, RuntimeError) as exc:
            return _error("staging.discard_failed", str(exc), 409)
        return _snapshot_response(snapshot)

    async def review(request: Request) -> JSONResponse:
        try:
            data = await _json(request) if await request.body() else {}
            if set(data) - {"paths", "cursor", "max_results"}:
                raise ExecutorToolError(INVALID_ARGUMENTS, "Unknown review fields")
            result = await asyncio.to_thread(executor.review, data)
        except (OSError, UnicodeError, ValueError) as exc:
            return _error("staging.review_failed", str(exc), 422)
        return JSONResponse(result)

    async def check(request: Request) -> JSONResponse:
        try:
            data = await _json(request)
            if set(data) - {"name", "command", "timeout_seconds"}:
                raise ExecutorToolError(INVALID_ARGUMENTS, "Unknown check fields")
            result = await executor.run_check(
                data.get("name"),
                data.get("command"),
                data.get("timeout_seconds", DEFAULT_CHECK_SECONDS),
            )
        except (OSError, UnicodeError, ValueError) as exc:
            return _error("check.failed", str(exc), 422)
        return JSONResponse(result)

    async def cancel(request: Request) -> JSONResponse:
        return JSONResponse({"cancelled": await cancel_bash(request.path_params["request_id"])})

    routes = (
        ("/v1/status", status, "GET"),
        ("/v1/tools", tool, "POST"),
        ("/v1/manifest", manifest, "POST"),
        ("/v1/staging/mark-published", mark_published, "POST"),
        ("/v1/staging/discard", discard, "POST"),
        ("/v1/staging/rebase", rebase_staging, "POST"),
        ("/v1/staging/review", review, "POST"),
        ("/v1/staging/checks", check, "POST"),
        ("/v1/workspace-snapshot", snapshot, "GET"),
        ("/v1/tools/{request_id:str}/cancel", cancel, "POST"),
    )
    app = Starlette(
        routes=[
            Route(path, authenticated(handler), methods=[method])
            for path, handler, method in routes
        ]
    )
    app.state.executor = executor
    return app


async def _json(request: Request) -> dict:
    data = await request.json()
    if not isinstance(data, dict):
        raise ExecutorToolError(INVALID_ARGUMENTS, "Request body must be an object")
    return data


def _error(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message, "retryable": False, "details": {}}},
        status_code=status,
    )


def _snapshot_response(snapshot: Snapshot) -> JSONResponse:
    return JSONResponse(
        {"snapshot_id": snapshot.snapshot_id, "file_count": snapshot.file_count}
    )


def _environment_context(
    config: ExecutorConfig, executor: ExecutorService
) -> dict[str, object]:
    snapshot = executor.snapshot
    if config.runner_socket is not None:
        # Commands run in the runner container, so its image decides what is installed.
        available = executor.runner_executables or []
    else:
        available = [
            name
            for name in DEVELOPER_EXECUTABLE_CANDIDATES
            if shutil.which(name, path="/usr/local/bin:/usr/bin:/bin") is not None
        ]
    try:
        unpublished_changes = bool(workspace_changes(snapshot, config.work_root))
    except (OSError, ValueError):
        unpublished_changes = True
    try:
        headroom = shutil.disk_usage(config.work_root).free
    except OSError:
        headroom = None
    return {
        "capability_schema_version": 2,
        "mode": "plan_reads_source_agent_uses_persistent_staging",
        "workspace_root": ".",
        "platform": platform.system().casefold(),
        "python_version": platform.python_version(),
        "network_access": False,
        "egress": egress_status(),
        "gpu_access": False,
        "command_style": "bash -lc",
        "developer_executables": available,
        "agent_snapshot": {
            "snapshot_id": snapshot.snapshot_id,
            "file_count": len(snapshot.hashes),
            "total_bytes": snapshot.total_bytes,
        },
        "workspace_empty": snapshot.empty,
        "source_snapshot_id": snapshot.snapshot_id,
        "staging_lifetime": "persistent_until_discarded",
        "staging_resumed": executor.resumed,
        "conflicts": [item.to_dict() for item in executor.conflicts.values()],
        "last_rebase": executor.last_rebase,
        "git_history": executor.git,
        "runner_image": config.runner_image or None,
        "command_runner": "runner_container" if config.runner_socket else "in_process",
        "command_runner_reachable": config.runner_socket is None
        or executor.runner_executables is not None,
        "unpublished_changes": unpublished_changes,
        "publication_status": "host_publication_pending_review"
        if unpublished_changes
        else "no_unpublished_changes",
        "storage_headroom_bytes": headroom,
        "limits": config.limits_status(),
    }
