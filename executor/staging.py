from __future__ import annotations

import hashlib
import os
import shutil
import stat
import threading
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from shared.tools import PUBLISH_BATCH_MAX_BYTES, PUBLISH_BATCH_MAX_OPERATIONS, PublishOperation

from .config import ExecutorConfig
from .errors import LIMIT_EXCEEDED, ExecutorToolError
from .paths import (
    UnsafePath,
    assert_unique_paths,
    is_hidden_part,
    is_secret_path,
    is_staging_excluded,
)

HASH_CHUNK_BYTES = 128 * 1024


@dataclass(frozen=True, slots=True)
class Snapshot:
    snapshot_id: str
    hashes: dict[str, str]
    total_bytes: int = 0
    sizes: dict[str, int] = field(default_factory=dict)
    modes: dict[str, int] = field(default_factory=dict)
    # Host (size, mtime_ns) per baseline file when it was last compared, so a later rebase
    # re-hashes only host files whose signature changed (incremental seeding, plan 2.5).
    source_signatures: dict[str, tuple[int, int]] = field(default_factory=dict)

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
        for chunk in iter(lambda: handle.read(HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_with_sha256(reader: BinaryIO, writer: BinaryIO) -> str:
    """Copy *reader* into *writer* and return the digest of exactly the bytes written."""
    digest = hashlib.sha256()
    for chunk in iter(lambda: reader.read(HASH_CHUNK_BYTES), b""):
        digest.update(chunk)
        writer.write(chunk)
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
# Symbolic links found by the last walk of each staging root. A link (for example a virtualenv's
# ``bin/python``) is an unpublishable entry: it is never followed, hashed, or published, and
# the file tools still refuse to traverse it.
_link_caches: dict[str, tuple[str, ...]] = {}
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


def _store_links(root: Path, links: list[str] | tuple[str, ...]) -> None:
    with _hash_cache_lock:
        _link_caches[_cache_key(root)] = tuple(sorted(set(links), key=str.casefold))


def staging_links(root: Path) -> list[str]:
    """Unpublishable symbolic links in staging, as of the last walk (walking once if needed)."""
    with _hash_cache_lock:
        cached = _link_caches.get(_cache_key(root))
    if cached is None:
        visible_files(root)
        with _hash_cache_lock:
            cached = _link_caches.get(_cache_key(root), ())
    return list(cached)


def clear_hash_cache(root: Path | None = None) -> None:
    """Forget cached digests for one staging root, or for every root."""
    with _hash_cache_lock:
        if root is None:
            _hash_caches.clear()
            _link_caches.clear()
        else:
            _hash_caches.pop(_cache_key(root), None)
            _link_caches.pop(_cache_key(root), None)


def _trusted(entry: tuple[_Signature, str, int] | None, signature: _Signature) -> str | None:
    if entry is None or entry[0] != signature:
        return None
    last_change = max(signature[3], signature[4])
    return entry[1] if last_change + RACY_WINDOW_NS <= entry[2] else None


def _copy_source_file(source: Path, destination: Path) -> str:
    """Copy a source file without following a link; return the digest of the bytes written."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(source, flags), "rb") as reader, destination.open("wb") as writer:
        return copy_with_sha256(reader, writer)


def seed_staging(config: ExecutorConfig) -> Snapshot:
    source = config.source_root.resolve(strict=True)
    work = config.work_root
    work.mkdir(parents=True, exist_ok=True)
    # Seeding fills an empty volume, so its free space is the capacity staging can use.
    config.validate_storage_capacity(shutil.disk_usage(work).free)
    if any(work.iterdir()):
        raise RuntimeError("Staging volume must be empty and fresh")
    hashes: dict[str, str] = {}
    sizes: dict[str, int] = {}
    modes: dict[str, int] = {}
    signatures: dict[str, tuple[int, int]] = {}
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
            info = path.stat()
            size = info.st_size
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
            digest = _copy_source_file(path, destination)
            os.chmod(destination, stat.S_IMODE(mode))
            hashes[relative] = digest
            sizes[relative] = size
            modes[relative] = stat.S_IMODE(mode)
            signatures[relative] = (info.st_size, info.st_mtime_ns)
            cache[relative] = (_signature(destination.lstat()), digest, observed)
    assert_unique_paths(relative_paths)
    snapshot = Snapshot(str(uuid.uuid4()), hashes, total, sizes, modes, signatures)
    _store_hash_cache(work, cache)
    return snapshot


def clear_staging(work_root: Path) -> None:
    """Remove everything in the staging area, including hidden metadata and logs."""
    work_root.mkdir(parents=True, exist_ok=True)
    for child in tuple(work_root.iterdir()):
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    clear_hash_cache(work_root)


def discard_staging(config: ExecutorConfig) -> Snapshot:
    """Empty the staging area and reseed it from the read-only source."""
    clear_staging(config.work_root)
    return seed_staging(config)


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
                raise RuntimeError(
                    f"Published delete no longer matches staging: {operation.path}"
                )
            hashes.pop(operation.path, None)
            sizes.pop(operation.path, None)
            modes.pop(operation.path, None)
            continue
        matches = (
            value is not None
            and value[0] == operation.staged_sha256
            and operation.staged_mode in (None, value[2])
        )
        if not matches:
            raise RuntimeError(
                f"Published staging no longer matches the manifest: {operation.path}"
            )
        hashes[operation.path], sizes[operation.path], modes[operation.path] = value
    return Snapshot(
        str(uuid.uuid4()),
        hashes,
        sum(sizes.values()),
        sizes,
        modes,
        dict(snapshot.source_signatures),
    )


def visible_files(root: Path) -> dict[str, tuple[str, int, int]]:
    """Return files eligible for staging/reconciliation/publication.

    Every call walks and stats the whole tree, but only files whose stat signature changed
    since the previous call (or that are too recently modified to trust) are re-hashed, so
    an unchanged tree costs one ``lstat`` per entry rather than a full content hash.
    """
    result: dict[str, tuple[str, int, int]] = {}
    links: list[str] = []
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
            hidden = is_hidden_part(entry.name)
            if hidden:
                continue
            if stat.S_ISLNK(mode):
                links.append(relative)
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
    _store_links(root, links)
    return result


def refresh_visible_files(
    root: Path,
    base: dict[str, tuple[str, int, int]],
    paths: Sequence[str],
) -> dict[str, tuple[str, int, int]]:
    """Update a recent ``visible_files`` listing for only the given relative paths.

    Callers must know that nothing else changed since ``base`` was captured (for example a
    write/edit that checkpointed immediately before touching exactly ``paths``); this avoids
    a second whole-tree walk just to report post-mutation status.
    """
    current = dict(base)
    links = set(staging_links(root))
    for relative in paths:
        links.discard(relative)
        if any(is_hidden_part(part) for part in relative.split("/")):
            current.pop(relative, None)
            continue
        path = root.joinpath(*relative.split("/"))
        try:
            info = path.lstat()
        except FileNotFoundError:
            current.pop(relative, None)
            continue
        if stat.S_ISLNK(info.st_mode):
            current.pop(relative, None)
            links.add(relative)
            continue
        if not stat.S_ISREG(info.st_mode):
            current.pop(relative, None)
            continue
        current[relative] = (sha256_file(path), info.st_size, stat.S_IMODE(info.st_mode))
    _store_links(root, tuple(links))
    return current


def workspace_changes(
    snapshot: Snapshot,
    work_root: Path,
    current: dict[str, tuple[str, int, int]] | None = None,
) -> list[WorkspaceChange]:
    """Compare the current staged files with the last publication baseline."""
    if current is None:
        current = visible_files(work_root)
    paths = sorted(set(snapshot.hashes) | set(current), key=str.casefold)
    changes: list[WorkspaceChange] = []
    for relative in paths:
        base_hash = snapshot.hashes.get(relative)
        base_mode = snapshot.modes.get(relative)
        staged_hash, staged_size, staged_mode = current.get(relative, (None, None, None))
        if staged_hash == base_hash and base_mode == staged_mode:
            continue
        if staged_hash is None:
            operation = "delete"
        elif base_hash is None:
            operation = "create"
        else:
            operation = "update"
        changes.append(
            WorkspaceChange(
                relative,
                operation,
                base_hash,
                staged_hash,
                snapshot.sizes.get(relative),
                staged_size,
                base_mode,
                staged_mode,
            )
        )
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
            raise ExecutorToolError(
                LIMIT_EXCEEDED,
                f"Staged file exceeds the {PUBLISH_BATCH_MAX_BYTES}-byte publication batch limit: "
                f"{change.path}",
            )
        full = (
            len(current) >= PUBLISH_BATCH_MAX_OPERATIONS
            or size + item_size > PUBLISH_BATCH_MAX_BYTES
        )
        if current and full:
            batches.append(current)
            current, size = [], 0
        current.append(change)
        size += item_size
    if current:
        batches.append(current)
    return batches
