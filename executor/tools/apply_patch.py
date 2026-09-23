"""Structured multi-file `apply_patch`: several write/edit/delete operations under one checkpoint."""

from __future__ import annotations

from pathlib import Path

from ..checkpoints import create_checkpoint, restore_checkpoint
from ..errors import (
    APPLY_PATCH_MALFORMED,
    INVALID_ARGUMENTS,
    LIMIT_EXCEEDED,
    PATH_INVALID_TYPE,
    PATH_MISSING,
    STAGING_CONFLICT,
    ExecutorToolError,
    classify_error,
)
from ..paths import normalize_relative, safe_path
from ..staging import sha256_file
from .edit import EDIT_ARGUMENTS, apply_edit, edit_result, prepare_edit
from .write import WRITE_ARGUMENTS, commit_write, prepare_write, write_result

PATCH_OPERATIONS = ("write", "edit", "delete")
DELETE_ARGUMENTS = frozenset({"path", "expected_sha256"})
MAX_PATCH_DIFF_BYTES = 64_000


def patch_payload_bytes(operations: object) -> int:
    """UTF-8 bytes of the content-bearing fields, the quantity bounded by max_patch_bytes."""
    total = 0
    for item in operations if isinstance(operations, list) else ():
        if isinstance(item, dict):
            for key in ("content", "old_str", "new_str"):
                if isinstance(item.get(key), str):
                    total += len(item[key].encode("utf-8"))
    return total


def apply_patch(
    root: Path,
    arguments: dict,
    *,
    max_operations: int,
    max_patch_bytes: int,
    max_write_bytes: int,
    max_edit_target_bytes: int,
    max_edit_result_bytes: int,
    max_checkpoint_files: int = 300_000,
    max_checkpoint_bytes: int = 2_000_000_000,
    max_staging_bytes: int | None = None,
) -> tuple[str, dict]:
    """Apply ordered operations atomically: every operation lands, or none do.

    Operations run in order against the staged state left by the previous one, so several
    edits to one file compose. One checkpoint is taken first and restored if any operation
    fails, raises, or is interrupted.
    """
    if set(arguments) - {"operations"}:
        raise ExecutorToolError(INVALID_ARGUMENTS, "Unknown apply_patch arguments")
    operations = arguments.get("operations")
    if not isinstance(operations, list) or not operations:
        raise _malformed("apply_patch requires a non-empty operations array")
    if len(operations) > max_operations:
        raise ExecutorToolError(LIMIT_EXCEEDED, f"apply_patch accepts at most {max_operations} operations; split the change into smaller patches", details={"failure": "too_many_operations", "operations": len(operations), "max_patch_operations": max_operations})
    payload = patch_payload_bytes(operations)
    if payload > max_patch_bytes:
        raise ExecutorToolError(LIMIT_EXCEEDED, f"apply_patch content totals {payload} bytes, above the {max_patch_bytes}-byte patch limit", details={"failure": "patch_too_large", "payload_bytes": payload, "max_patch_bytes": max_patch_bytes})
    for index, item in enumerate(operations):
        _validate_shape(index, item)

    checkpoint_id = create_checkpoint(root, max_files=max_checkpoint_files, max_total_bytes=max_checkpoint_bytes)
    results: list[dict] = []
    try:
        for item in operations:
            results.append(_apply_one(root, item, max_write_bytes=max_write_bytes, max_edit_target_bytes=max_edit_target_bytes, max_edit_result_bytes=max_edit_result_bytes, max_staging_bytes=max_staging_bytes))
    except BaseException as exc:
        restore_checkpoint(root, checkpoint_id)
        if not isinstance(exc, Exception):
            raise
        index = len(results)
        cause = classify_error(exc)
        details = {"failure": "operation_failed", "failed_operation": index, "operation": operations[index].get("operation"), "path": operations[index].get("path"), "applied_before_failure": len(results), "rolled_back": True, "cause_code": cause.code}
        if cause.details:
            details["cause"] = cause.details
        raise ExecutorToolError(cause.code, f"apply_patch operation {index} ({operations[index].get('operation')} {operations[index].get('path')}) failed: {cause.message} No operation was applied.", cause.retryable, details) from exc

    _bound_diffs(results)
    paths = list(dict.fromkeys(item["path"] for item in results))
    shrink_warnings = [item["shrink_details"] for item in results if item.get("shrink_warning")]
    counts = {name: sum(1 for item in results if item["operation"] == name) for name in PATCH_OPERATIONS}
    output = f"Applied patch: {len(results)} operation{'s' if len(results) != 1 else ''} across {len(paths)} file{'s' if len(paths) != 1 else ''} ({counts['write']} write, {counts['edit']} edit, {counts['delete']} delete)."
    if shrink_warnings:
        output += f" Warning: {len(shrink_warnings)} confirmed large shrink(s)."
    data = {
        "operations": results,
        "paths": paths,
        "operation_counts": counts,
        "checkpoint_id": checkpoint_id,
        "shrink_warnings": shrink_warnings,
        "atomicity": "single-checkpoint-all-or-nothing",
    }
    return output, data


def apply_patch_paths(arguments: dict) -> list[str]:
    """Normalized operation paths in request order (used for status refresh and permissions)."""
    operations = arguments.get("operations")
    paths: list[str] = []
    for item in operations if isinstance(operations, list) else ():
        if isinstance(item, dict) and isinstance(item.get("path"), str):
            paths.append(normalize_relative(item["path"]))
    return list(dict.fromkeys(paths))


def _validate_shape(index: int, item: object) -> None:
    if not isinstance(item, dict):
        raise _malformed(f"apply_patch operation {index} must be an object", index)
    operation = item.get("operation")
    if operation not in PATCH_OPERATIONS:
        raise _malformed(f"apply_patch operation {index} must be one of {', '.join(PATCH_OPERATIONS)}", index)
    allowed = {"write": WRITE_ARGUMENTS, "edit": EDIT_ARGUMENTS, "delete": DELETE_ARGUMENTS}[operation] | {"operation"}
    unknown = sorted(set(item) - allowed)
    if unknown:
        raise _malformed(f"apply_patch operation {index} ({operation}) has unknown fields: {', '.join(unknown)}", index)
    if not isinstance(item.get("path"), str) or not item["path"]:
        raise _malformed(f"apply_patch operation {index} requires a path", index)
    normalize_relative(item["path"])


def _apply_one(root: Path, item: dict, *, max_write_bytes: int, max_edit_target_bytes: int, max_edit_result_bytes: int, max_staging_bytes: int | None) -> dict:
    operation = item["operation"]
    arguments = {key: value for key, value in item.items() if key != "operation"}
    if operation == "write":
        prepared = prepare_write(root, arguments, max_bytes=max_write_bytes, max_staging_bytes=max_staging_bytes)
        commit_write(root, prepared)
        data = write_result(prepared, max_bytes=max_write_bytes, max_staging_bytes=max_staging_bytes)
        data["operation"] = "write"
        data["created"] = prepared.old_hash is None
        return data
    if operation == "edit":
        prepared_edit = prepare_edit(root, arguments, max_target_bytes=max_edit_target_bytes)
        applied = apply_edit(prepared_edit, max_result_bytes=max_edit_result_bytes)
        data = edit_result(prepared_edit, applied)
        data["operation"] = "edit"
        return data
    return _delete(root, arguments)


def _delete(root: Path, arguments: dict) -> dict:
    relative = arguments["path"]
    try:
        path = safe_path(root, relative, must_exist=True)
    except FileNotFoundError as exc:
        raise ExecutorToolError(PATH_MISSING, f"delete target not found: {relative}") from exc
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink > 1:
        raise ExecutorToolError(PATH_INVALID_TYPE, "delete target must be a regular, non-hard-linked file")
    old_hash = sha256_file(path)
    expected = arguments.get("expected_sha256")
    if expected is not None:
        if not isinstance(expected, str):
            raise ExecutorToolError(INVALID_ARGUMENTS, "expected_sha256 must be a string when supplied")
        if expected != old_hash:
            raise ExecutorToolError(STAGING_CONFLICT, f"Staging hash conflict: {relative}", retryable=True, details={"failure": "hash_conflict", "path": relative, "expected_sha256": expected, "actual_sha256": old_hash})
    size = path.stat().st_size
    path.unlink()
    return {"operation": "delete", "path": relative, "old_sha256": old_hash, "new_sha256": None, "size_bytes": 0, "deleted_bytes": size, "diff": {"path": relative, "text": f"{relative}: deleted ({size} bytes).", "truncated": False, "lines": 0, "changed_lines": 0, "added_lines": 0, "removed_lines": 0}}


def _bound_diffs(results: list[dict]) -> None:
    used = 0
    for item in results:
        diff = item.get("diff")
        if not isinstance(diff, dict):
            continue
        text = str(diff.get("text") or "")
        encoded = text.encode("utf-8")
        if used + len(encoded) > MAX_PATCH_DIFF_BYTES:
            remaining = max(0, MAX_PATCH_DIFF_BYTES - used)
            diff["text"] = encoded[:remaining].decode("utf-8", errors="ignore") + "\n… [patch diff budget exhausted] …"
            diff["truncated"] = True
            used = MAX_PATCH_DIFF_BYTES
        else:
            used += len(encoded)


def _malformed(message: str, index: int | None = None) -> ExecutorToolError:
    details: dict[str, object] = {"failure": "malformed_patch"}
    if index is not None:
        details["failed_operation"] = index
    return ExecutorToolError(APPLY_PATCH_MALFORMED, message, details=details)
