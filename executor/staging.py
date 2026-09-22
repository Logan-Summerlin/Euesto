from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import threading
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from shared.tools import PUBLISH_BATCH_MAX_BYTES, PUBLISH_BATCH_MAX_OPERATIONS, PublishOperation
from .config import ExecutorConfig
from .paths import SECRET_PARTS, STAGING_EXCLUDED_PARTS, UnsafePath, assert_unique_paths, is_secret_path, is_staging_excluded


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

    @property
    def permission_changed(self) -> bool:
        """An existing file whose mode changed (new files report their mode as created)."""
        return self.operation == "update" and self.mode_changed


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(128 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# visible_files() runs on the hot path of every mutation (checkpoint before, status after),
# so it reuses a file's previous digest while its stat signature is unchanged instead of
# re-hashing the whole tree. The signature includes ctime, which user code cannot set, so
# any content change (even one that restores mtime) invalidates the entry. A digest is only
# trusted once the file's last change is older than RACY_WINDOW_NS at the time it was
# hashed: a same-size rewrite within one coarse filesystem timestamp tick of the
# observation could otherwise keep an identical signature ("racily clean" entries).
RACY_WINDOW_NS = 250_000_000
_Signature = tuple[int, int, int, int, int]
_HashCache = dict[str, tuple[_Signature, str, int]]
_hash_caches: dict[str, _HashCache] = {}
_hash_cache_lock = threading.Lock()


def _signature(info: os.stat_result) -> _Signature:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _cache_key(root: Path) -> str:
    return os.path.normcase(str(root.resolve()))


def _load_hash_cache(root: Path) -> _HashCache:
    with _hash_cache_lock:
        return _hash_caches.get(_cache_key(root), {})


def _store_hash_cache(root: Path, cache: _HashCache) -> None:
    with _hash_cache_lock:
        _hash_caches[_cache_key(root)] = cache


def clear_hash_cache(root: Path | None = None) -> None:
    """Forget cached digests for one staging root, or for every root."""
    with _hash_cache_lock:
        if root is None:
            _hash_caches.clear()
        else:
            _hash_caches.pop(_cache_key(root), None)


def _trusted(entry: tuple[_Signature, str, int] | None, signature: _Signature) -> str | None:
    if entry is None or entry[0] != signature:
        return None
    last_change = max(signature[3], signature[4])
    return entry[1] if last_change + RACY_WINDOW_NS <= entry[2] else None


def _copy_and_hash(source: Path, destination: Path) -> str:
    """Copy a regular file and return the digest of exactly the bytes written."""
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(source, flags)
    with os.fdopen(descriptor, "rb") as reader, destination.open("wb") as writer:
        for chunk in iter(lambda: reader.read(128 * 1024), b""):
            digest.update(chunk)
            writer.write(chunk)
    return digest.hexdigest()


def seed_staging(config: ExecutorConfig) -> Snapshot:
    source = config.source_root.resolve(strict=True)
    work = config.work_root
    work.mkdir(parents=True, exist_ok=True)
    actual_capacity = shutil.disk_usage(work).total
    config.validate_storage_capacity(actual_capacity)
    if any(work.iterdir()):
        raise RuntimeError("Staging volume must be empty and fresh")
    hashes: dict[str, str] = {}
    sizes: dict[str, int] = {}
    modes: dict[str, int] = {}
    total = 0
    files = 0
    relative_paths: list[str] = []
    cache: _HashCache = {}
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
            if files > config.max_staged_files or total > config.max_staging_bytes:
                raise RuntimeError(
                    "Workspace exceeds executor staging limits: "
                    f"{files} files/{total} bytes; limits are "
                    f"{config.max_staged_files} files/{config.max_staging_bytes} bytes."
                )
            destination = work / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            observed = time.time_ns()
            digest = _copy_and_hash(path, destination)
            os.chmod(destination, stat.S_IMODE(mode))
            hashes[relative] = digest
            sizes[relative] = size
            modes[relative] = stat.S_IMODE(mode)
            cache[relative] = (_signature(destination.lstat()), digest, observed)
    assert_unique_paths(relative_paths)
    snapshot = Snapshot(str(uuid.uuid4()), hashes, total, sizes, modes)
    _write_snapshot(work, snapshot)
    _store_hash_cache(work, cache)
    return snapshot


def snapshot_current_staging(work_root: Path) -> Snapshot:
    """Create a baseline from the entire staged workspace.

    This helper is retained for callers that intentionally want a complete
    snapshot; publication uses ``advance_published_staging`` so unrelated staged
    changes are never implicitly marked as published.
    """
    current = visible_files(work_root)
    hashes = {path: value[0] for path, value in current.items()}
    sizes = {path: value[1] for path, value in current.items()}
    modes = {path: value[2] for path, value in current.items()}
    snapshot = Snapshot(str(uuid.uuid4()), hashes, sum(sizes.values()), sizes, modes)
    _write_snapshot(work_root, snapshot)
    return snapshot


def advance_published_staging(
    work_root: Path,
    snapshot: Snapshot,
    operations: Sequence[PublishOperation],
) -> Snapshot:
    """Advance the baseline only for operations successfully published to the host.

    The executor keeps the staged files for subsequent agent turns. Every
    published operation must still match its manifest hash/mode; otherwise the
    publication handoff is rejected instead of accidentally marking a newer
    staged mutation as published. Unrelated staged changes remain relative to
    the previous baseline and therefore continue to block Auto mode.
    """
    current = visible_files(work_root)
    hashes = dict(snapshot.hashes)
    sizes = dict(snapshot.sizes)
    modes = dict(snapshot.modes)
    for operation in operations:
        value = current.get(operation.path)
        if operation.operation == "delete":
            if value is not None:
                raise RuntimeError(f"Published delete no longer matches staging: {operation.path}")
            hashes.pop(operation.path, None)
            sizes.pop(operation.path, None)
            modes.pop(operation.path, None)
            continue
        if value is None or value[0] != operation.staged_sha256 or (operation.staged_mode is not None and value[2] != operation.staged_mode):
            raise RuntimeError(f"Published staging no longer matches the manifest: {operation.path}")
        hashes[operation.path] = value[0]
        sizes[operation.path] = value[1]
        modes[operation.path] = value[2]
    updated = Snapshot(str(uuid.uuid4()), hashes, sum(sizes.values()), sizes, modes)
    _write_snapshot(work_root, updated)
    return updated


def _write_snapshot(work: Path, snapshot: Snapshot) -> None:
    (work / ".local-chat-snapshot.json").write_text(
        json.dumps({"snapshot_id": snapshot.snapshot_id, "hashes": snapshot.hashes, "sizes": snapshot.sizes, "modes": snapshot.modes, "total_bytes": snapshot.total_bytes}, sort_keys=True),
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
    """Return files eligible for staging/reconciliation/publication.

    Every call walks and stats the whole tree, but only files whose stat signature changed
    since the previous call (or that are too recently modified to trust) are re-hashed, so
    an unchanged tree costs one ``lstat`` per entry rather than a full content hash.
    """
    result: dict[str, tuple[str, int, int]] = {}
    previous = _load_hash_cache(root)
    cache: _HashCache = {}
    # Ancestors are filtered before descent, so each entry only needs its own name checked.
    pending: list[tuple[str, str]] = [("", os.fspath(root))]
    while pending:
        prefix, directory = pending.pop()
        with os.scandir(directory) as iterator:
            entries = sorted(iterator, key=lambda item: item.name)
        subdirectories: list[tuple[str, str]] = []
        for entry in entries:
            relative = prefix + entry.name
            info = entry.stat(follow_symlinks=False)
            mode = info.st_mode
            hidden = _hidden_name(entry.name)
            if stat.S_ISLNK(mode):
                if hidden and not entry.is_dir():
                    continue
                raise UnsafePath(f"Staging link is forbidden: {relative}")
            if hidden:
                continue
            if stat.S_ISDIR(mode):
                subdirectories.append((relative + "/", entry.path))
                continue
            if not stat.S_ISREG(mode):
                continue
            signature = _signature(info)
            cached = previous.get(relative)
            digest = _trusted(cached, signature)
            if digest is None:
                observed = time.time_ns()
                digest = sha256_file(Path(entry.path))
                cached = (signature, digest, observed)
            cache[relative] = cached
            result[relative] = (digest, info.st_size, stat.S_IMODE(mode))
        pending.extend(reversed(subdirectories))
    _store_hash_cache(root, cache)
    return result


def _hidden_name(name: str) -> bool:
    """Whether one path segment is secret, staging-excluded, or executor metadata."""
    folded = name.casefold()
    return (
        folded in SECRET_PARTS
        or folded.startswith(".env")
        or folded in STAGING_EXCLUDED_PARTS
        or name.startswith(".local-chat-")
    )


def _is_executor_metadata(relative: str) -> bool:
    return any(part.startswith(".local-chat-") for part in Path(relative).parts)


def refresh_visible_files(root: Path, base: dict[str, tuple[str, int, int]], paths: Sequence[str]) -> dict[str, tuple[str, int, int]]:
    """Update a recent ``visible_files`` listing for only the given relative paths.

    Callers must know that nothing else changed since ``base`` was captured (for example a
    write/edit that checkpointed immediately before touching exactly ``paths``); this avoids
    a second whole-tree walk just to report post-mutation status.
    """
    current = dict(base)
    for relative in paths:
        if any(_hidden_name(part) for part in relative.split("/")):
            current.pop(relative, None)
            continue
        path = root.joinpath(*relative.split("/"))
        try:
            info = path.lstat()
        except FileNotFoundError:
            current.pop(relative, None)
            continue
        if stat.S_ISLNK(info.st_mode):
            raise UnsafePath(f"Staging link is forbidden: {relative}")
        if not stat.S_ISREG(info.st_mode):
            current.pop(relative, None)
            continue
        current[relative] = (sha256_file(path), info.st_size, stat.S_IMODE(info.st_mode))
    return current


def workspace_changes(snapshot: Snapshot, work_root: Path, current: dict[str, tuple[str, int, int]] | None = None) -> list[WorkspaceChange]:
    """Compare the current staged files with the last publication baseline."""
    if current is None:
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


def publication_batches(changes: Sequence[WorkspaceChange]) -> list[list[WorkspaceChange]]:
    """Split pending changes, in path order, into batches the desktop broker accepts.

    Each batch holds at most ``PUBLISH_BATCH_MAX_OPERATIONS`` operations and
    ``PUBLISH_BATCH_MAX_BYTES`` bytes of staged content. A single file larger than one batch
    cannot be published and is reported rather than silently skipped.
    """
    batches: list[list[WorkspaceChange]] = []
    current: list[WorkspaceChange] = []
    size = 0
    for change in changes:
        item_size = 0 if change.operation == "delete" else int(change.staged_size_bytes or 0)
        if item_size > PUBLISH_BATCH_MAX_BYTES:
            raise ValueError(f"Staged file exceeds the {PUBLISH_BATCH_MAX_BYTES}-byte publication batch limit: {change.path}")
        if current and (len(current) >= PUBLISH_BATCH_MAX_OPERATIONS or size + item_size > PUBLISH_BATCH_MAX_BYTES):
            batches.append(current)
            current, size = [], 0
        current.append(change)
        size += item_size
    if current:
        batches.append(current)
    return batches
