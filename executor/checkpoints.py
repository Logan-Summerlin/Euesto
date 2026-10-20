from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
import uuid
from pathlib import Path

from .atomic_io import atomic_write_bytes
from .config import ExecutorConfig
from .errors import (
    CHECKPOINT_CORRUPT,
    CHECKPOINT_NOT_FOUND,
    INVALID_ARGUMENTS,
    LIMIT_EXCEEDED,
    PATH_UNSAFE,
    ExecutorToolError,
)
from .paths import safe_path
from .staging import copy_with_sha256, sha256_file, visible_files

DEFAULT_MAX_CHECKPOINTS = 8
DEFAULT_MAX_CHECKPOINT_FILES = 300_000
DEFAULT_MAX_CHECKPOINT_BYTES = 2_500_000_000
_CHECKPOINT_ID = re.compile(r"^[0-9a-f-]{20,64}$")
# chmod without following links where the platform supports it (Windows Python 3.12 does not).
_NO_FOLLOW = {"follow_symlinks": False} if os.chmod in os.supports_follow_symlinks else {}


def create_checkpoint(
    work_root: Path,
    *,
    max_files: int = DEFAULT_MAX_CHECKPOINT_FILES,
    max_total_bytes: int = DEFAULT_MAX_CHECKPOINT_BYTES,
    max_checkpoints: int = DEFAULT_MAX_CHECKPOINTS,
) -> str:
    """Record the visible staging files so a failed mutation can be rolled back.

    ``max_total_bytes`` bounds both this checkpoint and the retained checkpoint store.
    """
    files = visible_files(work_root)
    total = sum(size for _digest, size, _mode in files.values())
    if len(files) > max_files or total > max_total_bytes:
        raise ExecutorToolError(LIMIT_EXCEEDED, "Staging is too large for a bounded recovery checkpoint.")
    actual_capacity = shutil.disk_usage(work_root).total
    required_capacity = total + max_total_bytes + ExecutorConfig.REQUIRED_TEMP_HEADROOM_BYTES
    if actual_capacity <= required_capacity:
        raise ExecutorToolError(
            LIMIT_EXCEEDED,
            "Checkpoint would exceed the combined /work resource budget: "
            f"staging={total}, checkpoint={max_total_bytes}, "
            f"temporary={ExecutorConfig.REQUIRED_TEMP_HEADROOM_BYTES}, capacity={actual_capacity}."
        )

    root = work_root / ".local-chat-checkpoints"
    objects = root / "objects"
    objects.mkdir(parents=True, exist_ok=True)
    checkpoint_id = str(uuid.uuid4())
    directory = root / checkpoint_id
    directory.mkdir()
    try:
        # Objects are content-addressed and verified when stored (and again before any
        # restore), so one directory scan decides which blobs are missing. Re-hashing or
        # re-statting the whole store here would make every mutation O(repository size).
        with os.scandir(objects) as iterator:
            stored = {entry.name for entry in iterator if entry.is_file(follow_symlinks=False)}
        for relative, (digest, _size, _mode) in files.items():
            if digest in stored:
                continue
            _store_object(work_root / relative, objects / digest, digest)
            stored.add(digest)
        manifest = {
            "version": 2,
            "checkpoint_id": checkpoint_id,
            "files": {
                relative: {"sha256": digest, "size_bytes": size, "mode": mode}
                for relative, (digest, size, mode) in sorted(files.items())
            },
            "file_count": len(files),
            "total_bytes": total,
        }
        manifest_path = directory / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
        _remember_references(manifest_path, {digest: size for digest, size, _mode in files.values()})
        _remember_files(checkpoint_id, files)
        _prune(root, checkpoint_id, max_checkpoints, max_total_bytes)
        return checkpoint_id
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise


# Parsed-manifest caches. _prune needs every retained checkpoint's referenced digests on each
# mutation, and the executor's post-mutation status can start from the file listing the
# checkpoint just walked; both are keyed so that a rewritten manifest is never reused.
_MAX_REMEMBERED_FILES = 4
_reference_cache: dict[str, tuple[int, dict[str, int]]] = {}
_recent_files: dict[str, dict[str, tuple[str, int, int]]] = {}
_cache_lock = threading.Lock()


def _remember_references(manifest_path: Path, references: dict[str, int]) -> None:
    with _cache_lock:
        _reference_cache[str(manifest_path)] = (manifest_path.stat().st_mtime_ns, references)


def _remember_files(checkpoint_id: str, files: dict[str, tuple[str, int, int]]) -> None:
    with _cache_lock:
        _recent_files[checkpoint_id] = files
        while len(_recent_files) > _MAX_REMEMBERED_FILES:
            _recent_files.pop(next(iter(_recent_files)))


def checkpoint_files(checkpoint_id: str) -> dict[str, tuple[str, int, int]] | None:
    """Return the visible-file listing captured by a recent checkpoint, if still cached."""
    with _cache_lock:
        files = _recent_files.get(checkpoint_id)
    return dict(files) if files is not None else None


class checkpointed:
    """Checkpoint staging on entry (yielding its id) and restore it if the body raises.

    Restoration also covers ``BaseException`` (cancellation, interpreter shutdown), so a
    mutation is never left half applied. A class rather than ``@contextmanager``, which
    would rewrite ``__traceback__`` on the frozen ``ExecutorToolError``.
    """

    def __init__(self, work_root: Path, *, max_files: int, max_total_bytes: int) -> None:
        self.work_root = work_root
        self.max_files = max_files
        self.max_total_bytes = max_total_bytes
        self.checkpoint_id = ""

    def __enter__(self) -> str:
        self.checkpoint_id = create_checkpoint(
            self.work_root, max_files=self.max_files, max_total_bytes=self.max_total_bytes
        )
        return self.checkpoint_id

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if exc_type is not None:
            restore_checkpoint(self.work_root, self.checkpoint_id)


def restore_checkpoint(
    work_root: Path,
    checkpoint_id: str,
    *,
    max_files: int = DEFAULT_MAX_CHECKPOINT_FILES,
    max_total_bytes: int = DEFAULT_MAX_CHECKPOINT_BYTES,
) -> dict[str, object]:
    manifest = _load_manifest(work_root, checkpoint_id)
    raw_files = manifest.get("files")
    if not isinstance(raw_files, dict) or len(raw_files) > max_files:
        raise ExecutorToolError(LIMIT_EXCEEDED, "Checkpoint file count exceeds the executor limit.")
    expected: dict[str, tuple[str, int, int | None]] = {}
    for relative, value in raw_files.items():
        if not isinstance(relative, str) or not isinstance(value, dict):
            raise ExecutorToolError(CHECKPOINT_CORRUPT, "Checkpoint manifest is invalid.")
        digest = str(value.get("sha256") or "")
        size = max(0, int(value.get("size_bytes") or 0))
        mode = int(value["mode"]) if value.get("mode") is not None else None
        safe_path(work_root, relative, must_exist=False)
        expected[relative] = (digest, size, mode)
    total = sum(size for _digest, size, _mode in expected.values())
    if total > max_total_bytes:
        raise ExecutorToolError(LIMIT_EXCEEDED, "Checkpoint size exceeds the executor limit.")
    current = visible_files(work_root)
    absent = (None, None, None)
    changed = [
        relative
        for relative in sorted(set(current) | set(expected), key=str.casefold)
        if _content_and_mode(current.get(relative, absent)) != _content_and_mode(expected.get(relative, absent))
    ]
    prepared: dict[str, tuple[bytes, int | None]] = {}
    objects = work_root / ".local-chat-checkpoints" / "objects"
    for relative in changed:
        if relative not in expected:
            continue
        digest, _size, mode = expected[relative]
        object_path = objects / digest
        if not object_path.is_file() or sha256_file(object_path) != digest:
            raise ExecutorToolError(CHECKPOINT_CORRUPT, "Checkpoint content is missing or corrupt.")
        prepared[relative] = (object_path.read_bytes(), mode)
    for relative in changed:
        target = safe_path(work_root, relative, must_exist=False)
        if relative not in expected:
            if target.exists():
                if target.is_symlink() or not target.is_file():
                    raise ExecutorToolError(PATH_UNSAFE, "Checkpoint restore target is unsafe.")
                target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(target, prepared[relative][0], prefix=".local-chat-")
        mode = prepared[relative][1]
        if mode is not None:
            os.chmod(target, mode, **_NO_FOLLOW)
    return {
        "checkpoint_id": checkpoint_id,
        "restored_paths": changed,
        "count": len(changed),
    }


def _content_and_mode(entry: tuple[object, object, object]) -> tuple[object, object]:
    digest, _size, mode = entry
    return digest, mode


def _load_manifest(work_root: Path, checkpoint_id: str) -> dict[str, object]:
    if not isinstance(checkpoint_id, str) or not _CHECKPOINT_ID.fullmatch(checkpoint_id):
        raise ExecutorToolError(INVALID_ARGUMENTS, "Invalid checkpoint identity.")
    path = work_root / ".local-chat-checkpoints" / checkpoint_id / "manifest.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ExecutorToolError(CHECKPOINT_NOT_FOUND, "Checkpoint was not found or is invalid.") from exc
    if not isinstance(value, dict) or value.get("checkpoint_id") != checkpoint_id:
        raise ExecutorToolError(CHECKPOINT_CORRUPT, "Checkpoint manifest identity is invalid.")
    return value


def _prune(root: Path, current_id: str, max_count: int, max_storage_bytes: int) -> None:
    directories = sorted(
        (path for path in root.iterdir() if path.is_dir() and _CHECKPOINT_ID.fullmatch(path.name)),
        key=lambda path: path.stat().st_mtime,
    )
    keep = {path.name for path in directories[-max(1, max_count) :]} | {current_id}
    for directory in directories:
        if directory.name not in keep:
            _remove_checkpoint(directory)

    objects = root / "objects"
    while True:
        kept_directories = [
            path
            for path in root.iterdir()
            if path.is_dir() and _CHECKPOINT_ID.fullmatch(path.name)
        ] if root.exists() else []
        # Sizes come from the manifests (verified at store time), so the object store is
        # never statted blob-by-blob.
        referenced: dict[str, int] = {}
        for directory in kept_directories:
            referenced.update(_manifest_references(directory))
        if sum(referenced.values()) <= max_storage_bytes:
            if objects.exists():
                with os.scandir(objects) as iterator:
                    unreferenced = [entry.path for entry in iterator if entry.name not in referenced]
                for path in unreferenced:
                    try:
                        os.unlink(path)
                    except (FileNotFoundError, IsADirectoryError, PermissionError):
                        pass
            return
        removable = [path for path in kept_directories if path.name != current_id]
        if not removable:
            raise ExecutorToolError(LIMIT_EXCEEDED, "Checkpoint storage budget is exhausted by the current checkpoint set.")
        _remove_checkpoint(min(removable, key=lambda path: path.stat().st_mtime))


def _remove_checkpoint(directory: Path) -> None:
    shutil.rmtree(directory, ignore_errors=True)
    with _cache_lock:
        _reference_cache.pop(str(directory / "manifest.json"), None)


def _manifest_references(directory: Path) -> dict[str, int]:
    manifest_path = directory / "manifest.json"
    try:
        mtime = manifest_path.stat().st_mtime_ns
    except OSError:
        return {}
    key = str(manifest_path)
    with _cache_lock:
        cached = _reference_cache.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    references: dict[str, int] = {}
    for item in _manifest_files(directory).values():
        if isinstance(item, dict) and item.get("sha256"):
            try:
                references[str(item["sha256"])] = max(0, int(item.get("size_bytes") or 0))
            except (TypeError, ValueError):
                references[str(item["sha256"])] = 0
    with _cache_lock:
        _reference_cache[key] = (mtime, references)
    return references


def _manifest_files(directory: Path) -> dict[str, object]:
    try:
        data = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    value = data.get("files") if isinstance(data, dict) else None
    return value if isinstance(value, dict) else {}


def _store_object(source: Path, object_path: Path, digest: str) -> None:
    """Copy one staged file into the object store, verifying the bytes actually stored."""
    object_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".object-", dir=object_path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as writer, source.open("rb") as reader:
            stored = copy_with_sha256(reader, writer)
        if stored != digest:
            raise ExecutorToolError(CHECKPOINT_CORRUPT, "Checkpoint content verification failed.")
        os.replace(temporary, object_path)
    finally:
        temporary.unlink(missing_ok=True)
