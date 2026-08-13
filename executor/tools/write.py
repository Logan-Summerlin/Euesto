from __future__ import annotations

import hashlib
from pathlib import Path

from ..checkpoints import create_checkpoint, restore_checkpoint
from ..paths import safe_path


def write(
    root: Path,
    arguments: dict,
    *,
    max_bytes: int,
    max_checkpoint_files: int = 300_000,
    max_checkpoint_bytes: int = 2_000_000_000,
) -> tuple[str, dict]:
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
    if len(content_bytes) > max_bytes:
        raise ValueError("Write content exceeds the mutation limit")
    path = safe_path(root, relative, must_exist=False)
    old_hash = None
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.stat().st_nlink > 1:
            raise ValueError("write target must be a regular, non-hard-linked file")
        old_hash = _sha256(path)
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

    checkpoint_id = create_checkpoint(
        root,
        max_files=max_checkpoint_files,
        max_total_bytes=max_checkpoint_bytes,
        max_storage_bytes=max_checkpoint_bytes,
    )
    try:
        if create_parents:
            parent.mkdir(parents=True, exist_ok=True)
        # Re-check the final target after parent creation and before mutation.
        target = safe_path(root, relative, must_exist=False)
        if target.exists():
            if target.is_symlink() or not target.is_file() or target.stat().st_nlink > 1:
                raise ValueError("write target must be a regular, non-hard-linked file")
        target.write_text(content, encoding="utf-8", newline="")
    except Exception:
        restore_checkpoint(root, checkpoint_id)
        raise
    new_hash = hashlib.sha256(content_bytes).hexdigest()
    return f"Wrote {relative}.", {
        "path": relative,
        "old_sha256": old_hash,
        "new_sha256": new_hash,
        "checkpoint_id": checkpoint_id,
        "size_bytes": len(content_bytes),
        "atomicity": "validated-before-write-with-checkpoint-rollback",
    }


def _validate_new_parents(root: Path, parent: Path) -> None:
    relative = parent.relative_to(root).as_posix()
    current = root
    for part in relative.split("/") if relative != "." else []:
        current = current / part
        if current.exists():
            safe_path(root, current.relative_to(root).as_posix(), must_exist=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(128 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
