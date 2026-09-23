from __future__ import annotations

import base64
import difflib
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import uuid
from pathlib import Path

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
from .staging import visible_files

DEFAULT_MAX_CHECKPOINTS = 8
DEFAULT_MAX_CHECKPOINT_BYTES = 2_500_000_000
_CHECKPOINT_ID = re.compile(r"^[0-9a-f-]{20,64}$")


class CheckpointError(ExecutorToolError):
    """A checkpoint failure; the raise site names its code like any executor rejection."""


def create_checkpoint(
    work_root: Path,
    *,
    max_files: int = 300_000,
    max_total_bytes: int = DEFAULT_MAX_CHECKPOINT_BYTES,
    max_checkpoints: int = DEFAULT_MAX_CHECKPOINTS,
    max_storage_bytes: int = DEFAULT_MAX_CHECKPOINT_BYTES,
) -> str:
    files = visible_files(work_root)
    total = sum(size for _digest, size, _mode in files.values())
    if len(files) > max_files or total > max_total_bytes:
        raise CheckpointError(LIMIT_EXCEEDED, "Staging is too large for a bounded recovery checkpoint.")
    actual_capacity = shutil.disk_usage(work_root).total
    required_capacity = total + max_total_bytes + ExecutorConfig.REQUIRED_TEMP_HEADROOM_BYTES
    if actual_capacity <= required_capacity:
        raise CheckpointError(
            LIMIT_EXCEEDED,
            "Checkpoint would exceed the combined /work resource budget: "
            f"staging={total}, checkpoint={max_total_bytes}, "
            f"temporary={ExecutorConfig.REQUIRED_TEMP_HEADROOM_BYTES}, capacity={actual_capacity}."
        )
    if max_storage_bytes < max_total_bytes:
        raise CheckpointError(LIMIT_EXCEEDED, "Checkpoint storage budget must cover one complete checkpoint.")

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
        _prune(root, checkpoint_id, max_checkpoints, max_storage_bytes)
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


def inspect_checkpoint(
    work_root: Path,
    checkpoint_id: str,
    *,
    max_results: int = 100,
    cursor: str | None = None,
    diff_paths: list[str] | None = None,
    max_diff_bytes: int = 32_000,
    max_diff_lines: int = 400,
) -> dict[str, object]:
    manifest = _load_manifest(work_root, checkpoint_id)
    files = manifest.get("files")
    values = dict(files) if isinstance(files, dict) else {}
    paths = sorted(values, key=str.casefold)
    start = _decode_index(cursor)
    limit = min(500, max(1, int(max_results)))
    selected_paths = paths[start : start + limit]
    next_cursor = (
        _encode_index(start + len(selected_paths))
        if start + len(selected_paths) < len(paths)
        else None
    )
    result: dict[str, object] = {
        "checkpoint_id": checkpoint_id,
        "file_count": int(manifest.get("file_count") or len(paths)),
        "total_bytes": int(manifest.get("total_bytes") or 0),
        "files": {path: values[path] for path in selected_paths},
        "returned": len(selected_paths),
        "total_known": len(paths),
        "limit": limit,
        "truncated": next_cursor is not None,
        "next_cursor": next_cursor,
    }
    if diff_paths is not None:
        if not 1 <= len(diff_paths) <= 20:
            raise CheckpointError(INVALID_ARGUMENTS, "Checkpoint diffs require 1-20 explicit paths.")
        diffs, truncated = _checkpoint_diffs(
            work_root,
            values,
            diff_paths,
            max_bytes=min(256_000, max(1, int(max_diff_bytes))),
            max_lines=min(2_000, max(1, int(max_diff_lines))),
        )
        result["diffs"] = diffs
        result["diff_truncated"] = truncated
    return result


def _checkpoint_diffs(
    work_root: Path,
    checkpoint_files: dict[str, object],
    paths: list[str],
    *,
    max_bytes: int,
    max_lines: int,
) -> tuple[list[dict[str, object]], bool]:
    result: list[dict[str, object]] = []
    used_bytes = 0
    used_lines = 0
    aggregate_truncated = False
    objects = work_root / ".local-chat-checkpoints" / "objects"
    for relative in paths:
        current_path = safe_path(work_root, relative, must_exist=False)
        raw_metadata = checkpoint_files.get(relative)
        checkpoint_text = ""
        try:
            if isinstance(raw_metadata, dict):
                digest = str(raw_metadata.get("sha256") or "")
                object_path = objects / digest
                if not object_path.is_file() or _sha256(object_path) != digest:
                    raise CheckpointError(CHECKPOINT_CORRUPT, "Checkpoint content is missing or corrupt.")
                checkpoint_text = object_path.read_text(encoding="utf-8")
            current_text = current_path.read_text(encoding="utf-8") if current_path.is_file() else ""
        except UnicodeError:
            result.append({"path": relative, "kind": "binary-or-invalid-utf8"})
            continue
        lines = list(
            difflib.unified_diff(
                checkpoint_text.splitlines(keepends=True),
                current_text.splitlines(keepends=True),
                fromfile=f"checkpoint/{relative}",
                tofile=f"current/{relative}",
            )
        )
        accepted: list[str] = []
        for line in lines:
            encoded = line.encode("utf-8")
            if used_lines >= max_lines or used_bytes + len(encoded) > max_bytes:
                aggregate_truncated = True
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
        if aggregate_truncated:
            break
    return result, aggregate_truncated


def restore_checkpoint(
    work_root: Path,
    checkpoint_id: str,
    *,
    preview: bool = False,
    max_files: int = 300_000,
    max_total_bytes: int = DEFAULT_MAX_CHECKPOINT_BYTES,
) -> dict[str, object]:
    manifest = _load_manifest(work_root, checkpoint_id)
    raw_files = manifest.get("files")
    if not isinstance(raw_files, dict) or len(raw_files) > max_files:
        raise CheckpointError(LIMIT_EXCEEDED, "Checkpoint file count exceeds the executor limit.")
    expected: dict[str, tuple[str, int, int | None]] = {}
    for relative, value in raw_files.items():
        if not isinstance(relative, str) or not isinstance(value, dict):
            raise CheckpointError(CHECKPOINT_CORRUPT, "Checkpoint manifest is invalid.")
        digest = str(value.get("sha256") or "")
        size = max(0, int(value.get("size_bytes") or 0))
        mode = int(value["mode"]) if value.get("mode") is not None else None
        safe_path(work_root, relative, must_exist=False)
        expected[relative] = (digest, size, mode)
    total = sum(size for _digest, size, _mode in expected.values())
    if total > max_total_bytes:
        raise CheckpointError(LIMIT_EXCEEDED, "Checkpoint size exceeds the executor limit.")
    current = visible_files(work_root)
    changed = sorted(set(current) | set(expected), key=str.casefold)
    changed = [
        relative
        for relative in changed
        if current.get(relative, (None, None, None))[0]
        != expected.get(relative, (None, None, None))[0]
        or current.get(relative, (None, None, None))[2]
        != expected.get(relative, (None, None, None))[2]
    ]
    if preview:
        return {
            "checkpoint_id": checkpoint_id,
            "preview": True,
            "changed_paths": changed,
            "count": len(changed),
        }
    prepared: dict[str, tuple[bytes, int | None]] = {}
    objects = work_root / ".local-chat-checkpoints" / "objects"
    for relative in changed:
        if relative not in expected:
            continue
        digest, _size, mode = expected[relative]
        object_path = objects / digest
        if not object_path.is_file() or _sha256(object_path) != digest:
            raise CheckpointError(CHECKPOINT_CORRUPT, "Checkpoint content is missing or corrupt.")
        prepared[relative] = (object_path.read_bytes(), mode)
    for relative in changed:
        target = safe_path(work_root, relative, must_exist=False)
        if relative not in expected:
            if target.exists():
                if target.is_symlink() or not target.is_file():
                    raise CheckpointError(PATH_UNSAFE, "Checkpoint restore target is unsafe.")
                target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_bytes(target, prepared[relative][0])
        mode = prepared[relative][1]
        if mode is not None:
            os.chmod(target, mode, follow_symlinks=False)
    return {
        "checkpoint_id": checkpoint_id,
        "preview": False,
        "restored_paths": changed,
        "count": len(changed),
    }


def discard_staging(config: object) -> object:
    work_root = config.work_root
    work_root.mkdir(parents=True, exist_ok=True)
    for child in tuple(work_root.iterdir()):
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    from .staging import seed_staging
    return seed_staging(config)


def _load_manifest(work_root: Path, checkpoint_id: str) -> dict[str, object]:
    if not isinstance(checkpoint_id, str) or not _CHECKPOINT_ID.fullmatch(checkpoint_id):
        raise CheckpointError(INVALID_ARGUMENTS, "Invalid checkpoint identity.")
    path = work_root / ".local-chat-checkpoints" / checkpoint_id / "manifest.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CheckpointError(CHECKPOINT_NOT_FOUND, "Checkpoint was not found or is invalid.") from exc
    if not isinstance(value, dict) or value.get("checkpoint_id") != checkpoint_id:
        raise CheckpointError(CHECKPOINT_CORRUPT, "Checkpoint manifest identity is invalid.")
    return value


def _prune(root: Path, current_id: str, max_count: int, max_storage_bytes: int) -> None:
    directories = sorted(
        (path for path in root.iterdir() if path.is_dir() and _CHECKPOINT_ID.fullmatch(path.name)),
        key=lambda path: path.stat().st_mtime,
    )
    keep = set(path.name for path in directories[-max(1, max_count) :]) | {current_id}
    for directory in directories:
        if directory.name not in keep:
            shutil.rmtree(directory, ignore_errors=True)
            with _cache_lock:
                _reference_cache.pop(str(directory / "manifest.json"), None)

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
            raise CheckpointError(LIMIT_EXCEEDED, "Checkpoint storage budget is exhausted by the current checkpoint set.")
        oldest = min(removable, key=lambda path: path.stat().st_mtime)
        shutil.rmtree(oldest, ignore_errors=True)
        with _cache_lock:
            _reference_cache.pop(str(oldest / "manifest.json"), None)


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
        hasher = hashlib.sha256()
        with os.fdopen(descriptor, "wb") as writer, source.open("rb") as reader:
            for chunk in iter(lambda: reader.read(128 * 1024), b""):
                hasher.update(chunk)
                writer.write(chunk)
        if hasher.hexdigest() != digest:
            raise CheckpointError(CHECKPOINT_CORRUPT, "Checkpoint content verification failed.")
        os.replace(temporary, object_path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_bytes(target: Path, content: bytes) -> None:
    descriptor, name = tempfile.mkstemp(prefix=".local-chat-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, target)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(128 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _encode_index(index: int) -> str:
    return base64.urlsafe_b64encode(str(max(0, index)).encode()).decode().rstrip("=")


def _decode_index(value: object) -> int:
    if not value:
        return 0
    try:
        padding = "=" * (-len(str(value)) % 4)
        return max(0, int(base64.urlsafe_b64decode(str(value) + padding).decode()))
    except (ValueError, UnicodeError, base64.binascii.Error):
        raise CheckpointError(INVALID_ARGUMENTS, "Invalid checkpoint result cursor") from None
