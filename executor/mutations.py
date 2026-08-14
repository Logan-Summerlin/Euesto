from __future__ import annotations

import difflib
import hashlib
from pathlib import Path

from .checkpoints import create_checkpoint, restore_checkpoint
from .errors import ExecutorToolError

MAX_DIFF_LINES = 200
MAX_DIFF_BYTES = 24_000
SHRINK_RATIO = 0.5

def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(128 * 1024), b""): digest.update(chunk)
    return digest.hexdigest()

def create_mutation_checkpoint(root: Path, *, max_files: int, max_total_bytes: int) -> str:
    return create_checkpoint(root, max_files=max_files, max_total_bytes=max_total_bytes, max_storage_bytes=max_total_bytes)

def rollback_mutation(root: Path, checkpoint_id: str) -> None: restore_checkpoint(root, checkpoint_id)

def guard_shrink(relative: str, path: Path, content: str | None) -> None:
    if content is None or not path.exists() or not path.is_file(): return
    old_bytes = path.stat().st_size; new_bytes = len(content.encode("utf-8"))
    try: old_lines = path.read_text(encoding="utf-8").count("\n") + 1
    except UnicodeDecodeError: return
    new_lines = content.count("\n") + 1
    if old_bytes >= 200 and old_lines >= 20 and new_bytes < old_bytes * SHRINK_RATIO and new_lines < old_lines * SHRINK_RATIO:
        raise ExecutorToolError("staging.shrink_warning", f"Whole-file edit for {relative} would shrink the file from {old_bytes} to {new_bytes} bytes and from {old_lines} to {new_lines} lines; review the full replacement before retrying.")

def bounded_diff(path: Path, old_content: str, new_content: str, *, fromfile: str | None = None, tofile: str | None = None) -> dict[str, object]:
    """Return a bounded diff between the content before and after a mutation."""
    lines = list(difflib.unified_diff(old_content.splitlines(), new_content.splitlines(), fromfile=fromfile or str(path), tofile=tofile or str(path), lineterm=""))
    truncated = len(lines) > MAX_DIFF_LINES
    if truncated: lines = lines[:MAX_DIFF_LINES]
    text = "\n".join(lines); encoded = text.encode("utf-8")
    if len(encoded) > MAX_DIFF_BYTES:
        text = encoded[:MAX_DIFF_BYTES].decode("utf-8", errors="ignore"); truncated = True
    if truncated: text = f"{text}\n… [diff truncated; showing a bounded preview] …"
    added_lines = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++")); removed_lines = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
    return {"path": path.as_posix(), "text": text, "truncated": truncated, "lines": len(lines), "changed_lines": max(added_lines, removed_lines), "added_lines": added_lines, "removed_lines": removed_lines}
