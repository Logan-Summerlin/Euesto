from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from ..checkpoints import create_checkpoint, restore_checkpoint
from ..paths import safe_path


def move_or_copy_file(
    root: Path,
    arguments: dict,
    *,
    max_bytes: int,
    move: bool,
    max_checkpoint_files: int = 300_000,
    max_checkpoint_bytes: int = 2_000_000_000,
) -> tuple[str, dict]:
    allowed = {"source", "destination", "expected_sha256", "destination_sha256"}
    if set(arguments) - allowed:
        raise ValueError("Unknown file operation arguments")
    source_name = str(arguments.get("source") or "")
    destination_name = str(arguments.get("destination") or "")
    if not source_name or not destination_name or source_name.casefold() == destination_name.casefold():
        raise ValueError("Source and destination must be distinct relative paths")
    source = safe_path(root, source_name, must_exist=True)
    destination = safe_path(root, destination_name, must_exist=False)
    if not source.is_file() or source.is_symlink():
        raise ValueError("Source must be a regular file")
    size = source.stat().st_size
    if size > max_bytes:
        raise ValueError("File exceeds the mutation limit")
    expected = arguments.get("expected_sha256")
    if not isinstance(expected, str) or _sha256(source) != expected:
        raise ValueError(f"Staging hash conflict: {source_name}")
    destination_hash = arguments.get("destination_sha256")
    current_destination = _sha256(destination) if destination.exists() else None
    if current_destination != destination_hash:
        raise ValueError(f"Destination hash conflict: {destination_name}")
    checkpoint_id = create_checkpoint(
        root,
        max_files=max_checkpoint_files,
        max_total_bytes=max_checkpoint_bytes,
        max_storage_bytes=max_checkpoint_bytes,
    )
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if move:
            source.replace(destination)
        else:
            shutil.copyfile(source, destination, follow_symlinks=False)
    except Exception:
        restore_checkpoint(root, checkpoint_id)
        raise
    action = "Moved" if move else "Copied"
    return f"{action} {source_name} to {destination_name}.", {
        "checkpoint_id": checkpoint_id,
        "source": source_name,
        "destination": destination_name,
        "size_bytes": size,
        "sha256": _sha256(destination),
        "atomicity": "validated-before-write-with-checkpoint-rollback",
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(128 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
