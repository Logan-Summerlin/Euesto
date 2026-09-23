from __future__ import annotations

import difflib
from pathlib import Path

from .errors import (
    INVALID_ARGUMENTS,
    STAGING_CONFLICT,
    STAGING_SHRINK_WARNING,
    ExecutorToolError,
)

MAX_DIFF_LINES = 200
MAX_DIFF_BYTES = 24_000
SHRINK_RATIO = 0.5
LINE_COUNT_CHUNK_BYTES = 64 * 1024


def check_expected_sha256(relative: str, expected: object, actual: str | None) -> None:
    """Refuse a mutation whose caller-supplied ``expected_sha256`` no longer matches."""
    if expected is None:
        return
    if not isinstance(expected, str):
        raise ExecutorToolError(INVALID_ARGUMENTS, "expected_sha256 must be a string when supplied")
    if expected != actual:
        raise ExecutorToolError(STAGING_CONFLICT, f"Staging hash conflict: {relative}", details={"failure": "hash_conflict", "path": relative, "expected_sha256": expected, "actual_sha256": actual})


def guard_shrink(relative: str, path: Path, content: str | None, *, replacement_old: str | None = None, replacement_new: str | None = None, replacement_occurrences: int | None = None, advisory: bool = False) -> dict[str, object] | None:
    """Detect a whole-file mutation that would drop most of an existing file.

    An unconfirmed replacement is rejected. When ``advisory`` is true the caller has already
    proven the change deliberate (an exact edit whose occurrence count matched, or a write
    whose ``expected_sha256`` matched), so the finding is returned as a warning instead.
    """
    if not path.exists() or not path.is_file(): return None
    old_bytes = path.stat().st_size
    if content is not None:
        new_bytes = len(content.encode("utf-8")); new_lines = content.count("\n") + 1
    elif replacement_old is not None and replacement_new is not None:
        old_match_bytes = len(replacement_old.encode("utf-8")); new_match_bytes = len(replacement_new.encode("utf-8")); occurrences = max(0, int(replacement_occurrences or 0))
        new_bytes = old_bytes + occurrences * (new_match_bytes - old_match_bytes); old_lines = _count_lines(path)
        new_lines = max(1, old_lines + occurrences * (replacement_new.count("\n") - replacement_old.count("\n")))
    else: return None
    old_lines = _count_lines(path)
    if not (old_bytes >= 200 and old_lines >= 20 and new_bytes < old_bytes * SHRINK_RATIO and new_lines < old_lines * SHRINK_RATIO):
        return None
    message = f"Whole-file edit for {relative} would shrink the file from {old_bytes} to {new_bytes} bytes and from {old_lines} to {new_lines} lines"
    if not advisory:
        raise ExecutorToolError(STAGING_SHRINK_WARNING, f"{message}; review the full replacement before retrying, or pass the current expected_sha256 to confirm a deliberate rewrite.", details={"failure": "shrink_guard", "path": relative, "old_bytes": old_bytes, "new_bytes": new_bytes, "old_lines": old_lines, "new_lines": new_lines})
    return {"path": relative, "old_bytes": old_bytes, "new_bytes": new_bytes, "old_lines": old_lines, "new_lines": new_lines, "message": f"{message}; applied because the change was confirmed."}


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
