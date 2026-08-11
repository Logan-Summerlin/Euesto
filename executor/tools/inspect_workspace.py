from __future__ import annotations

import base64
import difflib
from pathlib import Path

from ..paths import safe_path
from ..staging import Snapshot, WorkspaceChange, workspace_changes


def inspect_workspace(
    base_root: Path,
    staging_root: Path,
    snapshot: Snapshot,
    arguments: dict,
    *,
    max_bytes: int,
) -> tuple[str, dict]:
    allowed = {
        "max_results",
        "include_diff",
        "paths",
        "max_diff_bytes",
        "max_diff_lines",
        "cursor",
    }
    if set(arguments) - allowed:
        raise ValueError("Unknown inspect_workspace arguments")
    changes = workspace_changes(snapshot, staging_root)
    limit = min(500, max(1, int(arguments.get("max_results") or 100)))
    start = _decode_index(arguments.get("cursor"))
    selected = changes[start : start + limit]
    truncated = start + len(selected) < len(changes)
    next_cursor = _encode_index(start + len(selected)) if truncated else None
    data: dict[str, object] = {
        "mode": "staging",
        "changes": [_change_dict(item) for item in selected],
        "returned": len(selected),
        "total_known": len(changes),
        "limit": limit,
        "truncated": truncated,
        "next_cursor": next_cursor,
    }
    if arguments.get("include_diff"):
        paths = arguments.get("paths")
        if not isinstance(paths, list) or not 1 <= len(paths) <= 20:
            raise ValueError("include_diff requires 1-20 explicit paths")
        diffs, diff_truncated = _diffs(
            base_root,
            staging_root,
            [str(path) for path in paths],
            max_bytes=min(max_bytes, max(1, int(arguments.get("max_diff_bytes") or 32_000))),
            max_lines=min(2_000, max(1, int(arguments.get("max_diff_lines") or 400))),
        )
        data["diffs"] = diffs
        data["diff_truncated"] = diff_truncated
    output = "\n".join(
        f"{item.operation}\t{item.path}\t{item.staged_size_bytes if item.staged_size_bytes is not None else '-'}"
        for item in selected
    )
    return output, data


def _change_dict(change: WorkspaceChange) -> dict[str, object]:
    return {
        "path": change.path,
        "operation": change.operation,
        "base_sha256": change.base_sha256,
        "staged_sha256": change.staged_sha256,
        "base_size_bytes": change.base_size_bytes,
        "staged_size_bytes": change.staged_size_bytes,
    }


def _diffs(
    base_root: Path,
    staging_root: Path,
    paths: list[str],
    *,
    max_bytes: int,
    max_lines: int,
) -> tuple[list[dict[str, object]], bool]:
    result: list[dict[str, object]] = []
    used_bytes = 0
    used_lines = 0
    truncated = False
    for relative in paths:
        staged = safe_path(staging_root, relative, must_exist=False)
        base = safe_path(base_root, relative, must_exist=False)
        try:
            before = base.read_text(encoding="utf-8") if base.is_file() else ""
            after = staged.read_text(encoding="utf-8") if staged.is_file() else ""
        except UnicodeError:
            result.append({"path": relative, "kind": "binary-or-invalid-utf8"})
            continue
        lines = list(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"a/{relative}",
                tofile=f"b/{relative}",
            )
        )
        accepted: list[str] = []
        for line in lines:
            encoded = line.encode("utf-8")
            if used_lines >= max_lines or used_bytes + len(encoded) > max_bytes:
                truncated = True
                break
            accepted.append(line)
            used_lines += 1
            used_bytes += len(encoded)
        result.append(
            {
                "path": relative,
                "kind": "text",
                "text": "".join(accepted),
                "truncated": len(accepted) < len(lines),
            }
        )
        if truncated:
            break
    return result, truncated


def _encode_index(index: int) -> str:
    return base64.urlsafe_b64encode(str(max(0, index)).encode()).decode().rstrip("=")


def _decode_index(value: object) -> int:
    if not value:
        return 0
    try:
        padding = "=" * (-len(str(value)) % 4)
        return max(0, int(base64.urlsafe_b64decode(str(value) + padding).decode()))
    except (ValueError, UnicodeError, base64.binascii.Error):
        raise ValueError("Invalid workspace result cursor") from None
