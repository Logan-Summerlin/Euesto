from __future__ import annotations

import base64
import difflib
import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path

from .paths import safe_path
from .staging import visible_files

DEFAULT_MAX_CHECKPOINTS = 8
DEFAULT_MAX_CHECKPOINT_BYTES = 2_000_000_000
_CHECKPOINT_ID = re.compile(r"^[0-9a-f-]{20,64}$")

class CheckpointError(ValueError):
    pass

def create_checkpoint(work_root: Path, *, max_files: int = 300_000, max_total_bytes: int = DEFAULT_MAX_CHECKPOINT_BYTES, max_checkpoints: int = DEFAULT_MAX_CHECKPOINTS, max_storage_bytes: int = DEFAULT_MAX_CHECKPOINT_BYTES) -> str:
    files = visible_files(work_root)
    total = sum(size for _digest, size, _mode in files.values())
    if len(files) > max_files or total > max_total_bytes:
        raise CheckpointError("Staging is too large for a bounded recovery checkpoint.")
    root = work_root / ".local-chat-checkpoints"; objects = root / "objects"; objects.mkdir(parents=True, exist_ok=True)
    checkpoint_id = str(uuid.uuid4()); directory = root / checkpoint_id; directory.mkdir()
    try:
        for relative, (digest, _size, _mode) in files.items():
            object_path = objects / digest
            if not object_path.exists():
                object_path.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(work_root / relative, object_path, follow_symlinks=False)
            if not object_path.is_file() or _sha256(object_path) != digest: raise CheckpointError("Checkpoint content verification failed.")
        manifest = {"version": 2, "checkpoint_id": checkpoint_id, "files": {relative: {"sha256": digest, "size_bytes": size, "mode": mode} for relative, (digest, size, mode) in sorted(files.items())}, "file_count": len(files), "total_bytes": total}
        (directory / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8"); _prune(root, checkpoint_id, max_checkpoints, max_storage_bytes); return checkpoint_id
    except Exception:
        shutil.rmtree(directory, ignore_errors=True); raise

def inspect_checkpoint(work_root: Path, checkpoint_id: str, *, max_results: int = 100, cursor: str | None = None, diff_paths: list[str] | None = None, max_diff_bytes: int = 32_000, max_diff_lines: int = 400) -> dict[str, object]:
    manifest = _load_manifest(work_root, checkpoint_id); files = manifest.get("files"); values = dict(files) if isinstance(files, dict) else {}; paths = sorted(values, key=str.casefold); start = _decode_index(cursor); limit = min(500, max(1, int(max_results))); selected_paths = paths[start : start + limit]; next_cursor = _encode_index(start + len(selected_paths)) if start + len(selected_paths) < len(paths) else None
    result: dict[str, object] = {"checkpoint_id": checkpoint_id, "file_count": int(manifest.get("file_count") or len(paths)), "total_bytes": int(manifest.get("total_bytes") or 0), "files": {path: values[path] for path in selected_paths}, "returned": len(selected_paths), "total_known": len(paths), "limit": limit, "truncated": next_cursor is not None, "next_cursor": next_cursor}
    if diff_paths is not None:
        if not 1 <= len(diff_paths) <= 20: raise CheckpointError("Checkpoint diffs require 1-20 explicit paths.")
        diffs, truncated = _checkpoint_diffs(work_root, values, diff_paths, max_bytes=min(256_000, max(1, int(max_diff_bytes))), max_lines=min(2_000, max(1, int(max_diff_lines)))); result["diffs"] = diffs; result["diff_truncated"] = truncated
    return result

def _checkpoint_diffs(work_root: Path, checkpoint_files: dict[str, object], paths: list[str], *, max_bytes: int, max_lines: int) -> tuple[list[dict[str, object]], bool]:
    result: list[dict[str, object]] = []; used_bytes = 0; used_lines = 0; aggregate_truncated = False; objects = work_root / ".local-chat-checkpoints" / "objects"
    for relative in paths:
        current_path = safe_path(work_root, relative, must_exist=False); raw_metadata = checkpoint_files.get(relative); checkpoint_text = ""
        try:
            if isinstance(raw_metadata, dict):
                digest = str(raw_metadata.get("sha256") or ""); object_path = objects / digest
                if not object_path.is_file() or _sha256(object_path) != digest: raise CheckpointError("Checkpoint content is missing or corrupt.")
                checkpoint_text = object_path.read_text(encoding="utf-8")
            current_text = current_path.read_text(encoding="utf-8") if current_path.is_file() else ""
        except UnicodeError:
            result.append({"path": relative, "kind": "binary-or-invalid-utf8"}); continue
        lines = list(difflib.unified_diff(checkpoint_text.splitlines(keepends=True), current_text.splitlines(keepends=True), fromfile=f"checkpoint/{relative}", tofile=f"current/{relative}")); accepted: list[str] = []
        for line in lines:
            encoded = line.encode("utf-8")
            if used_lines >= max_lines or used_bytes + len(encoded) > max_bytes: aggregate_truncated = True; break
            accepted.append(line); used_lines += 1; used_bytes += len(encoded)
        result.append({"path": relative, "kind": "text", "text": "".join(accepted), "truncated": len(accepted) < len(lines)})
        if aggregate_truncated: break
    return result, aggregate_truncated

def restore_checkpoint(work_root: Path, checkpoint_id: str, *, preview: bool = False, max_files: int = 300_000, max_total_bytes: int = DEFAULT_MAX_CHECKPOINT_BYTES) -> dict[str, object]:
    manifest = _load_manifest(work_root, checkpoint_id); raw_files = manifest.get("files")
    if not isinstance(raw_files, dict) or len(raw_files) > max_files: raise CheckpointError("Checkpoint file count exceeds the executor limit.")
    expected: dict[str, tuple[str, int, int | None]] = {}
    for relative, value in raw_files.items():
        if not isinstance(relative, str) or not isinstance(value, dict): raise CheckpointError("Checkpoint manifest is invalid.")
        digest = str(value.get("sha256") or ""); size = max(0, int(value.get("size_bytes") or 0)); mode = int(value["mode"]) if value.get("mode") is not None else None; safe_path(work_root, relative, must_exist=False); expected[relative] = (digest, size, mode)
    total = sum(size for _digest, size, _mode in expected.values())
    if total > max_total_bytes: raise CheckpointError("Checkpoint size exceeds the executor limit.")
    current = visible_files(work_root); changed = sorted(set(current) | set(expected), key=str.casefold); changed = [relative for relative in changed if current.get(relative, (None, None, None))[0] != expected.get(relative, (None, None, None))[0] or current.get(relative, (None, None, None))[2] != expected.get(relative, (None, None, None))[2]]
    if preview: return {"checkpoint_id": checkpoint_id, "preview": True, "changed_paths": changed, "count": len(changed)}
    prepared: dict[str, tuple[bytes, int | None]] = {}; objects = work_root / ".local-chat-checkpoints" / "objects"
    for relative in changed:
        if relative not in expected: continue
        digest, _size, mode = expected[relative]; object_path = objects / digest
        if not object_path.is_file() or _sha256(object_path) != digest: raise CheckpointError("Checkpoint content is missing or corrupt.")
        prepared[relative] = (object_path.read_bytes(), mode)
    for relative in changed:
        target = safe_path(work_root, relative, must_exist=False)
        if relative not in expected:
            if target.exists():
                if target.is_symlink() or not target.is_file(): raise CheckpointError("Checkpoint restore target is unsafe.")
                target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True); _atomic_bytes(target, prepared[relative][0]); mode = prepared[relative][1]
        if mode is not None: os.chmod(target, mode, follow_symlinks=False)
    return {"checkpoint_id": checkpoint_id, "preview": False, "restored_paths": changed, "count": len(changed)}

def discard_staging(config: object) -> object:
    work_root = config.work_root; work_root.mkdir(parents=True, exist_ok=True)
    for child in tuple(work_root.iterdir()):
        if child.is_dir() and not child.is_symlink(): shutil.rmtree(child)
        else: child.unlink()
    from .staging import seed_staging
    return seed_staging(config)

def _load_manifest(work_root: Path, checkpoint_id: str) -> dict[str, object]:
    if not isinstance(checkpoint_id, str) or not _CHECKPOINT_ID.fullmatch(checkpoint_id): raise CheckpointError("Invalid checkpoint identity.")
    path = work_root / ".local-chat-checkpoints" / checkpoint_id / "manifest.json"
    try: value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc: raise CheckpointError("Checkpoint was not found or is invalid.") from exc
    if not isinstance(value, dict) or value.get("checkpoint_id") != checkpoint_id: raise CheckpointError("Checkpoint manifest identity is invalid.")
    return value

def _prune(root: Path, current_id: str, max_count: int, max_storage_bytes: int) -> None:
    directories = sorted((path for path in root.iterdir() if path.is_dir() and _CHECKPOINT_ID.fullmatch(path.name)), key=lambda path: path.stat().st_mtime); keep = set(path.name for path in directories[-max(1, max_count):]) | {current_id}
    for directory in directories:
        if directory.name not in keep: shutil.rmtree(directory, ignore_errors=True)
    objects = root / "objects"; referenced = {str(item.get("sha256")) for directory in directories if directory.exists() for item in _manifest_files(directory).values() if isinstance(item, dict) and item.get("sha256")}; object_files = sorted((path for path in objects.iterdir() if path.is_file()), key=lambda path: path.stat().st_mtime) if objects.exists() else []
    for path in object_files:
        if path.name not in referenced: path.unlink(missing_ok=True)
    total = sum(path.stat().st_size for path in object_files if path.exists())
    for path in object_files:
        if total <= max_storage_bytes or path.name in referenced: continue
        total -= path.stat().st_size; path.unlink(missing_ok=True)

def _manifest_files(directory: Path) -> dict[str, object]:
    try: data = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError): return {}
    value = data.get("files") if isinstance(data, dict) else None; return value if isinstance(value, dict) else {}

def _atomic_bytes(target: Path, content: bytes) -> None:
    descriptor, name = tempfile.mkstemp(prefix=".local-chat-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle: handle.write(content); handle.flush(); os.fsync(handle.fileno())
        os.replace(name, target)
    finally:
        try: os.unlink(name)
        except FileNotFoundError: pass

def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(128 * 1024), b""): digest.update(chunk)
    return digest.hexdigest()

def _encode_index(index: int) -> str: return base64.urlsafe_b64encode(str(max(0, index)).encode()).decode().rstrip("=")
def _decode_index(value: object) -> int:
    if not value: return 0
    try:
        padding = "=" * (-len(str(value)) % 4); return max(0, int(base64.urlsafe_b64decode(str(value) + padding).decode()))
    except (ValueError, UnicodeError, base64.binascii.Error): raise CheckpointError("Invalid checkpoint result cursor") from None
