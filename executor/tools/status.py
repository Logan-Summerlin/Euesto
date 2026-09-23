"""Harness-native staged status and bounded diff inspection (no Git involved)."""

from __future__ import annotations

import difflib
import hashlib
from pathlib import Path

from shared.tools import PUBLISH_BATCH_MAX_BYTES, PUBLISH_BATCH_MAX_OPERATIONS

from ..errors import INVALID_ARGUMENTS, PATH_INVALID, PATH_MISSING, ExecutorToolError
from ..paths import UnsafePath, is_tool_excluded, normalize_relative
from ..staging import Snapshot, WorkspaceChange, publication_batches, workspace_changes
from .listing import decode_cursor, encode_cursor

STATUS_ARGUMENTS = frozenset({"paths", "include_diffs", "max_results", "cursor"})
DEFAULT_STATUS_RESULTS = 100
MAX_STATUS_RESULTS = 500
MAX_STATUS_PATHS = 50
MAX_STATUS_DIFF_BYTES = 64_000
MAX_STATUS_DIFF_LINES = 800
MAX_STATUS_DIFF_FILE_BYTES = 1_000_000
MAX_CURSOR_OFFSET = 1_000_000


def status(work_root: Path, source_root: Path, snapshot: Snapshot, arguments: dict, *, current: dict[str, tuple[str, int, int]] | None = None) -> tuple[str, dict]:
    """Summarize staged changes against the publication baseline, with optional diffs.

    The baseline is the snapshot the next publication manifest is validated against, so this
    is exactly "what would be published". Secret, dependency/cache, and executor metadata
    paths never appear because ``workspace_changes`` only sees publishable files.
    """
    if set(arguments) - STATUS_ARGUMENTS:
        raise ExecutorToolError(INVALID_ARGUMENTS, "Unknown status arguments")
    requested = arguments.get("max_results", DEFAULT_STATUS_RESULTS)
    if not isinstance(requested, int) or isinstance(requested, bool) or not 1 <= requested <= MAX_STATUS_RESULTS:
        raise ExecutorToolError(INVALID_ARGUMENTS, f"max_results must be an integer from 1 to {MAX_STATUS_RESULTS}")
    include_diffs = arguments.get("include_diffs", False)
    if not isinstance(include_diffs, bool):
        raise ExecutorToolError(INVALID_ARGUMENTS, "include_diffs must be a boolean")
    scopes = _scopes(arguments.get("paths"))
    cursor = decode_cursor(arguments.get("cursor"), "status", maximum=MAX_CURSOR_OFFSET)

    all_changes = workspace_changes(snapshot, work_root, current)
    changes = all_changes
    if scopes is not None:
        changes = [item for item in all_changes if any(item.path == scope or item.path.startswith(scope + "/") for scope in scopes)]
    page = changes[cursor : cursor + requested]
    has_more = cursor + len(page) < len(changes)
    counts = _counts(changes)
    entries = [_entry(item) for item in page]

    lines = [_summary(counts, len(changes), scopes is not None)]
    lines.extend(_status_line(item) for item in page)
    data: dict[str, object] = {
        "baseline_snapshot_id": snapshot.snapshot_id,
        "staged": bool(changes),
        "publication": "pending_review" if changes else "no_changes",
        "counts": counts,
        "changes": entries,
        "returned": len(entries),
        "total_known": len(changes),
        "limit": requested,
        "truncated": has_more,
        "publication_batches": _batch_count(all_changes),
        "publication_batch_limits": {"max_operations": PUBLISH_BATCH_MAX_OPERATIONS, "max_bytes": PUBLISH_BATCH_MAX_BYTES},
    }
    if scopes is not None:
        data["paths"] = scopes
    if has_more:
        data["truncation_reason"] = "result_limit"
        data["next_cursor"] = encode_cursor(cursor + len(page))
    if include_diffs:
        diffs, diff_truncated = _diffs(work_root, source_root, page)
        data["diffs"] = diffs
        data["diff_truncated"] = diff_truncated
        data["max_diff_bytes"] = MAX_STATUS_DIFF_BYTES
        data["max_diff_lines"] = MAX_STATUS_DIFF_LINES
        for item in diffs:
            if item.get("text"):
                lines.append(str(item["text"]))
            elif item.get("kind") != "text":
                lines.append(f"[{item['path']}: {item['kind']} — no textual diff]")
        if diff_truncated:
            lines.append("… [status diff budget exhausted; narrow `paths` or page with `cursor`] …")
    if has_more:
        lines.append(f"… {len(changes) - cursor - len(page)} more change(s); continue with next_cursor.")
    return "\n".join(lines), data


def _scopes(value: object) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_STATUS_PATHS or not all(isinstance(item, str) and item for item in value):
        raise ExecutorToolError(INVALID_ARGUMENTS, f"paths must be a list of 1 to {MAX_STATUS_PATHS} relative paths")
    scopes: list[str] = []
    for item in value:
        try:
            normalized = normalize_relative(item)
        except UnsafePath as exc:
            raise ExecutorToolError(PATH_INVALID, f"Invalid status path {item!r}: {exc}") from exc
        if normalized != "." and is_tool_excluded(normalized):
            raise ExecutorToolError(PATH_MISSING, f"file not found: {item}")
        scopes.append(normalized)
    return None if "." in scopes else list(dict.fromkeys(scopes))


def _counts(changes: list[WorkspaceChange]) -> dict[str, int]:
    return {
        "created": sum(1 for item in changes if item.operation == "create"),
        "modified": sum(1 for item in changes if item.operation == "update"),
        "deleted": sum(1 for item in changes if item.operation == "delete"),
        "permission_changes": sum(1 for item in changes if item.permission_changed),
    }


def _summary(counts: dict[str, int], total: int, scoped: bool) -> str:
    scope = " (filtered)" if scoped else ""
    if not total:
        return f"No staged changes{scope}; staging matches the publication baseline."
    return (
        f"Staged changes vs publication baseline{scope}: {counts['created']} created, {counts['modified']} modified, "
        f"{counts['deleted']} deleted, {counts['permission_changes']} permission change(s); {total} total, host publication pending review."
    )


def _entry(change: WorkspaceChange) -> dict[str, object]:
    return {
        "path": change.path,
        "operation": change.operation,
        "base_sha256": change.base_sha256,
        "staged_sha256": change.staged_sha256,
        "base_size_bytes": change.base_size_bytes,
        "staged_size_bytes": change.staged_size_bytes,
        "base_mode": _octal(change.base_mode),
        "staged_mode": _octal(change.staged_mode),
        "mode_changed": change.permission_changed,
    }


def _status_line(change: WorkspaceChange) -> str:
    code = {"create": "A", "update": "M", "delete": "D"}[change.operation]
    line = f"{code}  {change.path}"
    if change.permission_changed:
        line += f" (mode {_octal(change.base_mode)} -> {_octal(change.staged_mode)})"
    elif change.operation == "create" and change.staged_mode is not None:
        line += f" (mode {_octal(change.staged_mode)})"
    return line


def _octal(mode: int | None) -> str | None:
    return None if mode is None else format(mode, "o")


def _batch_count(changes: list[WorkspaceChange]) -> int | None:
    """Sequential publication batches all pending changes need (0 when clean, None when a
    single file is too large to publish)."""
    try:
        return len(publication_batches(changes))
    except ValueError:
        return None


def _diffs(work_root: Path, source_root: Path, changes: list[WorkspaceChange]) -> tuple[list[dict[str, object]], bool]:
    results: list[dict[str, object]] = []
    used_bytes = 0; used_lines = 0; exhausted = False
    for change in changes:
        if exhausted:
            results.append({"path": change.path, "kind": "omitted", "reason": "diff_budget_exhausted"})
            continue
        if change.operation == "update" and change.base_sha256 == change.staged_sha256:
            results.append({"path": change.path, "kind": "mode_only", "base_mode": _octal(change.base_mode), "staged_mode": _octal(change.staged_mode)})
            continue
        before = _baseline_bytes(work_root, source_root, change) if change.operation != "create" else b""
        after = _staged_bytes(work_root, change) if change.operation != "delete" else b""
        if before is None or after is None:
            reason = "too_large" if _too_large(change) else "baseline_unavailable"
            results.append({"path": change.path, "kind": reason})
            continue
        before_text = _text(before); after_text = _text(after)
        if before_text is None or after_text is None:
            results.append({"path": change.path, "kind": "binary", "base_size_bytes": change.base_size_bytes, "staged_size_bytes": change.staged_size_bytes})
            continue
        lines = list(difflib.unified_diff(
            before_text.splitlines(keepends=True), after_text.splitlines(keepends=True),
            fromfile=f"baseline/{change.path}" if change.operation != "create" else "/dev/null",
            tofile=f"staged/{change.path}" if change.operation != "delete" else "/dev/null",
        ))
        accepted: list[str] = []
        for line in lines:
            if not line.endswith("\n"):
                line += "\n\\ No newline at end of file\n"
            encoded = len(line.encode("utf-8"))
            if used_lines >= MAX_STATUS_DIFF_LINES or used_bytes + encoded > MAX_STATUS_DIFF_BYTES:
                exhausted = True
                break
            accepted.append(line); used_lines += 1; used_bytes += encoded
        added = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
        removed = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
        results.append({"path": change.path, "kind": "text", "text": "".join(accepted).rstrip("\n"), "truncated": len(accepted) < len(lines), "added_lines": added, "removed_lines": removed})
    return results, exhausted


def _too_large(change: WorkspaceChange) -> bool:
    return max(int(change.base_size_bytes or 0), int(change.staged_size_bytes or 0)) > MAX_STATUS_DIFF_FILE_BYTES


def _baseline_bytes(work_root: Path, source_root: Path, change: WorkspaceChange) -> bytes | None:
    """Baseline content by hash: the checkpoint object store first, then the source mount."""
    digest = change.base_sha256
    if not digest or int(change.base_size_bytes or 0) > MAX_STATUS_DIFF_FILE_BYTES:
        return None
    candidates = [work_root / ".local-chat-checkpoints" / "objects" / digest, source_root.joinpath(*change.path.split("/"))]
    for candidate in candidates:
        try:
            if candidate.is_symlink() or not candidate.is_file() or candidate.stat().st_size > MAX_STATUS_DIFF_FILE_BYTES:
                continue
            content = candidate.read_bytes()
        except OSError:
            continue
        if hashlib.sha256(content).hexdigest() == digest:
            return content
    return None


def _staged_bytes(work_root: Path, change: WorkspaceChange) -> bytes | None:
    if int(change.staged_size_bytes or 0) > MAX_STATUS_DIFF_FILE_BYTES:
        return None
    path = work_root.joinpath(*change.path.split("/"))
    try:
        if path.is_symlink() or not path.is_file():
            return None
        return path.read_bytes()
    except OSError:
        return None


def _text(content: bytes) -> str | None:
    if b"\x00" in content:
        return None
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return None

