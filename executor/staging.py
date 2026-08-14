from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .config import ExecutorConfig
from .paths import UnsafePath, assert_unique_paths, is_secret_path, is_staging_excluded


@dataclass(frozen=True, slots=True)
class Snapshot:
    snapshot_id: str
    hashes: dict[str, str]
    total_bytes: int = 0
    sizes: dict[str, int] = field(default_factory=dict)
    modes: dict[str, int] = field(default_factory=dict)

    @property
    def file_count(self) -> int:
        return len(self.hashes)

    @property
    def empty(self) -> bool:
        return not self.hashes


@dataclass(frozen=True, slots=True)
class WorkspaceChange:
    path: str
    operation: str
    base_sha256: str | None
    staged_sha256: str | None
    base_size_bytes: int | None
    staged_size_bytes: int | None
    base_mode: int | None = None
    staged_mode: int | None = None

    @property
    def mode_changed(self) -> bool:
        return self.base_mode != self.staged_mode


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(128 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def seed_staging(config: ExecutorConfig) -> Snapshot:
    source = config.source_root.resolve(strict=True)
    work = config.work_root
    work.mkdir(parents=True, exist_ok=True)
    if any(work.iterdir()):
        raise RuntimeError("Staging volume must be empty and fresh")
    hashes: dict[str, str] = {}
    sizes: dict[str, int] = {}
    modes: dict[str, int] = {}
    total = 0
    files = 0
    relative_paths: list[str] = []
    for current, dirnames, filenames in os.walk(source, topdown=True, followlinks=False):
        current_path = Path(current)
        retained_dirs: list[str] = []
        for dirname in sorted(dirnames):
            path = current_path / dirname
            relative = path.relative_to(source).as_posix()
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise UnsafePath(f"Source link is forbidden: {relative}")
            if is_secret_path(relative) or is_staging_excluded(relative):
                continue
            retained_dirs.append(dirname)
            relative_paths.append(relative)
            (work / relative).mkdir(parents=True, exist_ok=True)
        dirnames[:] = retained_dirs

        for filename in sorted(filenames):
            path = current_path / filename
            relative = path.relative_to(source).as_posix()
            if is_secret_path(relative) or is_staging_excluded(relative):
                continue
            relative_paths.append(relative)
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise UnsafePath(f"Source link is forbidden: {relative}")
            if not stat.S_ISREG(mode) or path.stat().st_nlink > 1:
                raise UnsafePath(f"Unsupported source file: {relative}")
            size = path.stat().st_size
            total += size
            files += 1
            if files > config.max_files or total > config.max_total_bytes:
                raise RuntimeError(
                    "Workspace exceeds executor snapshot limits: "
                    f"{files} files/{total} bytes; limits are "
                    f"{config.max_files} files/{config.max_total_bytes} bytes."
                )
            destination = work / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination, follow_symlinks=False)
            os.chmod(destination, stat.S_IMODE(mode))
            hashes[relative] = sha256_file(path)
            sizes[relative] = size
            modes[relative] = stat.S_IMODE(mode)
    assert_unique_paths(relative_paths)
    snapshot = Snapshot(str(uuid.uuid4()), hashes, total, sizes, modes)
    _write_snapshot(work, snapshot)
    return snapshot


def snapshot_current_staging(work_root: Path) -> Snapshot:
    """Create a new publication baseline from the current staged workspace.

    This does not copy or discard files. It records the exact executor state that
    was just published, so those files remain available for subsequent agent turns
    without being treated as new host changes.
    """
    current = visible_files(work_root)
    hashes = {path: value[0] for path, value in current.items()}
    sizes = {path: value[1] for path, value in current.items()}
    modes = {path: value[2] for path, value in current.items()}
    snapshot = Snapshot(str(uuid.uuid4()), hashes, sum(sizes.values()), sizes, modes)
    _write_snapshot(work_root, snapshot)
    return snapshot


def _write_snapshot(work: Path, snapshot: Snapshot) -> None:
    (work / ".local-chat-snapshot.json").write_text(
        json.dumps(
            {"snapshot_id": snapshot.snapshot_id, "hashes": snapshot.hashes, "sizes": snapshot.sizes, "modes": snapshot.modes, "total_bytes": snapshot.total_bytes},
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def load_snapshot(work_root: Path) -> Snapshot:
    data = json.loads((work_root / ".local-chat-snapshot.json").read_text(encoding="utf-8"))
    hashes = {str(k): str(v) for k, v in data["hashes"].items()}
    sizes = {str(k): max(0, int(v)) for k, v in (data.get("sizes") or {}).items() if isinstance(k, str)}
    modes = {str(k): int(v) for k, v in (data.get("modes") or {}).items() if isinstance(k, str)}
    total_bytes = data.get("total_bytes")
    if total_bytes is None:
        total_bytes = sum(path.stat().st_size for relative in hashes if (path := work_root / relative).is_file())
    return Snapshot(str(data["snapshot_id"]), hashes, max(0, int(total_bytes or 0)), sizes, modes)


def visible_files(root: Path) -> dict[str, tuple[str, int, int]]:
    """Return staged files eligible for review/publication, including POSIX mode."""
    result: dict[str, tuple[str, int, int]] = {}
    for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        retained_dirs: list[str] = []
        for dirname in sorted(dirnames):
            path = current_path / dirname
            relative = path.relative_to(root).as_posix()
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise UnsafePath(f"Staging link is forbidden: {relative}")
            if is_staging_excluded(relative) or _is_executor_metadata(relative):
                continue
            retained_dirs.append(dirname)
        dirnames[:] = retained_dirs

        for filename in sorted(filenames):
            path = current_path / filename
            relative = path.relative_to(root).as_posix()
            if is_staging_excluded(relative) or _is_executor_metadata(relative):
                continue
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise UnsafePath(f"Staging link is forbidden: {relative}")
            if not stat.S_ISREG(mode):
                continue
            result[relative] = (sha256_file(path), path.stat().st_size, stat.S_IMODE(mode))
    return result


def _is_executor_metadata(relative: str) -> bool:
    return any(part.startswith(".local-chat-") for part in Path(relative).parts)


def workspace_changes(snapshot: Snapshot, work_root: Path) -> list[WorkspaceChange]:
    """Compare the current staged files with the last publication baseline."""
    current = visible_files(work_root)
    paths = sorted(set(snapshot.hashes) | set(current), key=str.casefold)
    changes: list[WorkspaceChange] = []
    for relative in paths:
        base_hash = snapshot.hashes.get(relative)
        current_value = current.get(relative)
        staged_hash = current_value[0] if current_value else None
        base_mode = snapshot.modes.get(relative)
        staged_mode = current_value[2] if current_value else None
        if staged_hash == base_hash and base_mode == staged_mode:
            continue
        if current_value is None:
            operation = "delete"
        elif base_hash is None:
            operation = "create"
        else:
            operation = "update"
        changes.append(WorkspaceChange(relative, operation, base_hash, staged_hash, snapshot.sizes.get(relative), current_value[1] if current_value else None, base_mode, staged_mode))
    return changes
