from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..atomic_io import atomic_write_text
from ..checkpoints import create_checkpoint, restore_checkpoint
from ..errors import (
    INVALID_ARGUMENTS,
    INVALID_UTF8,
    LIMIT_EXCEEDED,
    PATH_MISSING,
    ExecutorToolError,
)
from ..mutations import bounded_diff, bounded_edit_diff, check_expected_sha256, guard_shrink
from ..paths import require_regular_file, safe_path
from ..staging import sha256_file
from ..utf8 import validate_utf8_file

WRITE_DIFF_MEMORY_BYTES = 1_000_000
WRITE_ARGUMENTS = frozenset({"path", "content", "expected_sha256", "create_parents"})


@dataclass(frozen=True, slots=True)
class PreparedWrite:
    relative: str
    path: Path
    content: str
    requested_bytes: int
    old_hash: str | None
    create_parents: bool
    diff: dict
    shrink_warning: dict | None


def write(root: Path, arguments: dict, *, max_bytes: int, max_checkpoint_files: int = 300_000, max_checkpoint_bytes: int, max_staging_bytes: int | None = None) -> tuple[str, dict]:
    if set(arguments) - WRITE_ARGUMENTS:
        raise ExecutorToolError(INVALID_ARGUMENTS, "Unknown write arguments")
    prepared = prepare_write(root, arguments, max_bytes=max_bytes, max_staging_bytes=max_staging_bytes)
    checkpoint_id = create_checkpoint(root, max_files=max_checkpoint_files, max_total_bytes=max_checkpoint_bytes)
    try:
        commit_write(root, prepared)
    except BaseException:
        restore_checkpoint(root, checkpoint_id)
        raise
    data = write_result(prepared, max_bytes=max_bytes, max_staging_bytes=max_staging_bytes)
    data["checkpoint_id"] = checkpoint_id
    changed = int(prepared.diff["changed_lines"])
    verb = "Created" if prepared.old_hash is None else "Wrote"
    output = f"{verb} {prepared.relative}. Changed {changed} line{'s' if changed != 1 else ''}."
    if prepared.shrink_warning:
        output += " Warning: confirmed large shrink."
    return output, data


def prepare_write(root: Path, arguments: dict, *, max_bytes: int, max_staging_bytes: int | None = None) -> PreparedWrite:
    """Validate one write against the current staged state without changing anything."""
    relative = arguments.get("path")
    content = arguments.get("content")
    if not isinstance(relative, str) or not relative:
        raise ExecutorToolError(INVALID_ARGUMENTS, "write requires a file path")
    if not isinstance(content, str) or "\x00" in content:
        raise ExecutorToolError(INVALID_UTF8, "write requires UTF-8 text content")
    requested_bytes = len(content.encode("utf-8"))
    if requested_bytes > max_bytes:
        raise ExecutorToolError(LIMIT_EXCEEDED, "Write content exceeds the mutation limit")
    if max_staging_bytes is not None and requested_bytes > max_staging_bytes:
        raise ExecutorToolError(LIMIT_EXCEEDED, "Write content exceeds staging capacity")

    path = safe_path(root, relative, must_exist=False)
    old_hash = None
    original = None
    if path.exists():
        require_regular_file(path, "write")
        validate_utf8_file(path, "Only UTF-8 text writes are supported")
        old_hash = sha256_file(path)
        if path.stat().st_size <= WRITE_DIFF_MEMORY_BYTES:
            original = path.read_text(encoding="utf-8")

    expected = arguments.get("expected_sha256")
    check_expected_sha256(relative, expected, old_hash)
    # A matching expected_sha256 proves the caller reviewed the current content, so a large
    # shrink is a deliberate rewrite and is reported rather than refused.
    shrink_warning = guard_shrink(relative, path, content, advisory=expected is not None) if old_hash is not None else None

    create_parents = bool(arguments.get("create_parents", False))
    parent = path.parent
    if not parent.exists() and not create_parents:
        raise ExecutorToolError(PATH_MISSING, f"Parent directory does not exist: {parent.relative_to(root).as_posix()}")
    if parent.exists():
        safe_path(root, parent.relative_to(root).as_posix(), must_exist=True)
    else:
        _validate_new_parents(root, parent)

    if old_hash is None:
        diff = bounded_diff(path, "", content, fromfile=relative, tofile=relative)
    elif original is not None:
        diff = bounded_diff(path, original, content, fromfile=relative, tofile=relative)
    else:
        diff = bounded_edit_diff(relative, 1, "<whole-file>", content)
    return PreparedWrite(relative, path, content, requested_bytes, old_hash, create_parents, diff, shrink_warning)


def commit_write(root: Path, prepared: PreparedWrite) -> None:
    """Apply a prepared write; the caller owns the surrounding checkpoint."""
    if prepared.create_parents:
        prepared.path.parent.mkdir(parents=True, exist_ok=True)
    target = safe_path(root, prepared.relative, must_exist=False)
    if target.exists():
        require_regular_file(target, "write")
    atomic_write_text(target, prepared.content)


def write_result(prepared: PreparedWrite, *, max_bytes: int, max_staging_bytes: int | None) -> dict:
    data = {
        "path": prepared.relative, "old_sha256": prepared.old_hash, "new_sha256": sha256_file(prepared.path),
        "size_bytes": prepared.requested_bytes, "requested_write_bytes": prepared.requested_bytes, "max_write_bytes": max_bytes,
        "staging_capacity_bytes": max_staging_bytes, "diff": prepared.diff,
        "atomicity": "tempfile-fsync-atomic-replace-with-checkpoint-rollback",
    }
    if prepared.shrink_warning:
        data["shrink_warning"] = True
        data["shrink_details"] = prepared.shrink_warning
    return data


def _validate_new_parents(root: Path, parent: Path) -> None:
    relative = parent.relative_to(root).as_posix(); current = root
    for part in relative.split("/") if relative != "." else []:
        current = current / part
        if current.exists(): safe_path(root, current.relative_to(root).as_posix(), must_exist=True)

