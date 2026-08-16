from __future__ import annotations

import difflib
import hashlib
from pathlib import Path

from .checkpoints import create_checkpoint, restore_checkpoint
from .errors import ExecutorToolError

MAX_DIFF_LINES = 200
MAX_DIFF_BYTES = 24_000
SHRINK_RATIO = 0.5
LINE_COUNT_CHUNK_BYTES = 64 * 1024


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(128 * 1024), b""): digest.update(chunk)
    return digest.hexdigest()


def create_mutation_checkpoint(root: Path, *, max_files: int, max_total_bytes: int) -> str:
    return create_checkpoint(root, max_files=max_files, max_total_bytes=max_total_bytes, max_storage_bytes=max_total_bytes)


def rollback_mutation(root: Path, checkpoint_id: str) -> None:
    restore_checkpoint(root, checkpoint_id)


def guard_shrink(relative: str, path: Path, content: str | None, *, replacement_old: str | None = None, replacement_new: str | None = None, replacement_occurrences: int | None = None) -> None:
    if not path.exists() or not path.is_file(): return
    old_bytes = path.stat().st_size
    if content is not None:
        new_bytes = len(content.encode("utf-8")); new_lines = content.count("\n") + 1
    elif replacement_old is not None and replacement_new is not None:
        old_match_bytes = len(replacement_old.encode("utf-8")); new_match_bytes = len(replacement_new.encode("utf-8")); occurrences = max(0, int(replacement_occurrences or 0))
        new_bytes = old_bytes + occurrences * (new_match_bytes - old_match_bytes); old_lines = _count_lines(path)
        new_lines = max(1, old_lines + occurrences * (replacement_new.count("\n") - replacement_old.count("\n")))
    else: return
    old_lines = _count_lines(path)
    if old_bytes >= 200 and old_lines >= 20 and new_bytes < old_bytes * SHRINK_RATIO and new_lines < old_lines * SHRINK_RATIO:
        raise ExecutorToolError("staging.shrink_warning", f"Whole-file edit for {relative} would shrink the file from {old_bytes} to {new_bytes} bytes and from {old_lines} to {new_lines} lines; review the full replacement before retrying.")


def _count_lines(path: Path) -> int:
    size = path.stat().st_size
    if size == 0: return 0
    count = 0; last_byte = b""
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(LINE_COUNT_CHUNK_BYTES), b""):
            count += chunk.count(b"\n"); last_byte = chunk[-1:]
    return count + (0 if last_byte == b"\n" else 1)


def bounded_diff(path: Path, old_content: str, new_content: str, *, fromfile: str | None = None, tofile: str | None = None) -> dict[str, object]:
    lines = list(difflib.unified_diff(old_content.splitlines(), new_content.splitlines(), fromfile=fromfile or str(path), tofile=tofile or str(path), lineterm=""))
    truncated = len(lines) > MAX_DIFF_LINES
    if truncated: lines = lines[:MAX_DIFF_LINES]
    text = "\n".join(lines); encoded = text.encode("utf-8")
    if len(encoded) > MAX_DIFF_BYTES: text = encoded[:MAX_DIFF_BYTES].decode("utf-8", errors="ignore"); truncated = True
    if truncated: text = f"{text}\n… [diff truncated; showing a bounded preview] …"
    added_lines = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++")); removed_lines = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
    return {"path": path.as_posix(), "text": text, "truncated": truncated, "lines": len(lines), "changed_lines": max(added_lines, removed_lines), "added_lines": added_lines, "removed_lines": removed_lines}


def bounded_edit_diff(relative: str, occurrences: int, old: str, new: str) -> dict[str, object]:
    changed_lines = max(occurrences * max(1, old.count("\n") + 1), occurrences * max(1, new.count("\n") + 1))
    preview = f"{relative}: replaced {occurrences} occurrence{'s' if occurrences != 1 else ''} in a large file; full diff omitted to preserve bounded memory."
    if len(preview.encode("utf-8")) > MAX_DIFF_BYTES: preview = preview.encode("utf-8")[:MAX_DIFF_BYTES].decode("utf-8", errors="ignore")
    return {"path": relative, "text": preview, "truncated": True, "lines": 0, "changed_lines": changed_lines, "added_lines": occurrences * max(1, new.count("\n") + 1), "removed_lines": occurrences * max(1, old.count("\n") + 1)}
