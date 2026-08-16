from __future__ import annotations

import codecs
from pathlib import Path

from ..mutations import bounded_diff, bounded_edit_diff, create_mutation_checkpoint, guard_shrink, rollback_mutation, sha256
from ..paths import safe_path

WRITE_DIFF_MEMORY_BYTES = 1_000_000


def write(root: Path, arguments: dict, *, max_bytes: int, max_checkpoint_files: int = 300_000, max_checkpoint_bytes: int = 2_000_000_000, max_staging_bytes: int | None = None) -> tuple[str, dict]:
    allowed = {"path", "content", "expected_sha256", "create_parents"}
    if set(arguments) - allowed:
        raise ValueError("Unknown write arguments")
    relative = arguments.get("path")
    content = arguments.get("content")
    if not isinstance(relative, str) or not relative:
        raise ValueError("write requires a file path")
    if not isinstance(content, str) or "\x00" in content:
        raise ValueError("write requires UTF-8 text content")
    content_bytes = content.encode("utf-8")
    requested_bytes = len(content_bytes)
    if requested_bytes > max_bytes:
        raise ValueError("Write content exceeds the mutation limit")
    if max_staging_bytes is not None and requested_bytes > max_staging_bytes:
        raise ValueError("Write content exceeds staging capacity")

    path = safe_path(root, relative, must_exist=False)
    old_hash = None
    original = None
    old_size = 0
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.stat().st_nlink > 1:
            raise ValueError("write target must be a regular, non-hard-linked file")
        old_size = path.stat().st_size
        _validate_existing_text(path)
        old_hash = sha256(path)
        if old_size <= WRITE_DIFF_MEMORY_BYTES:
            original = path.read_text(encoding="utf-8")
        guard_shrink(relative, path, content)

    expected = arguments.get("expected_sha256")
    if expected is not None:
        if not isinstance(expected, str) or old_hash != expected:
            raise ValueError(f"Staging hash conflict: {relative}")
    elif not isinstance(expected, type(None)):
        raise ValueError("expected_sha256 must be a string when supplied")

    create_parents = bool(arguments.get("create_parents", False))
    parent = path.parent
    if not parent.exists() and not create_parents:
        raise ValueError(f"Parent directory does not exist: {parent.relative_to(root).as_posix()}")
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

    checkpoint_id = create_mutation_checkpoint(root, max_files=max_checkpoint_files, max_total_bytes=max_checkpoint_bytes)
    try:
        if create_parents:
            parent.mkdir(parents=True, exist_ok=True)
        target = safe_path(root, relative, must_exist=False)
        if target.exists() and (target.is_symlink() or not target.is_file() or target.stat().st_nlink > 1):
            raise ValueError("write target must be a regular, non-hard-linked file")
        target.write_text(content, encoding="utf-8", newline="")
    except Exception:
        rollback_mutation(root, checkpoint_id)
        raise

    new_hash = sha256(path)
    changed = int(diff["changed_lines"])
    verb = "Created" if old_hash is None else "Wrote"
    return f"{verb} {relative}. Changed {changed} line{'s' if changed != 1 else ''}.", {
        "path": relative, "old_sha256": old_hash, "new_sha256": new_hash, "checkpoint_id": checkpoint_id,
        "size_bytes": requested_bytes, "requested_write_bytes": requested_bytes, "max_write_bytes": max_bytes,
        "staging_capacity_bytes": max_staging_bytes, "diff": diff,
        "atomicity": "validated-before-write-with-checkpoint-rollback",
    }


def _validate_new_parents(root: Path, parent: Path) -> None:
    relative = parent.relative_to(root).as_posix(); current = root
    for part in relative.split("/") if relative != "." else []:
        current = current / part
        if current.exists(): safe_path(root, current.relative_to(root).as_posix(), must_exist=True)


def _validate_existing_text(path: Path) -> None:
    decoder = codecs.getincrementaldecoder("utf-8")()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            if b"\x00" in chunk:
                raise ValueError("Only UTF-8 text writes are supported")
            try:
                decoder.decode(chunk)
            except UnicodeDecodeError as exc:
                raise ValueError("Only UTF-8 text writes are supported") from exc
    try:
        decoder.decode(b"", final=True)
    except UnicodeDecodeError as exc:
        raise ValueError("Only UTF-8 text writes are supported") from exc
