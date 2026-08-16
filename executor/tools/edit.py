from __future__ import annotations

from pathlib import Path

from ..mutations import bounded_diff, create_mutation_checkpoint, guard_shrink, rollback_mutation, sha256
from ..paths import safe_path


def edit(
    root: Path,
    arguments: dict,
    *,
    max_target_bytes: int,
    max_result_bytes: int,
    max_checkpoint_files: int = 300_000,
    max_checkpoint_bytes: int = 2_000_000_000,
) -> tuple[str, dict]:
    allowed = {"path", "old_str", "new_str", "expected_occurrences", "expected_sha256"}
    if set(arguments) - allowed:
        raise ValueError("Unknown edit arguments")
    relative = arguments.get("path")
    old = arguments.get("old_str")
    new = arguments.get("new_str")
    expected_occurrences = arguments.get("expected_occurrences", 1)
    if not isinstance(relative, str) or not relative:
        raise ValueError("edit requires a file path")
    if not isinstance(old, str) or not old or not isinstance(new, str) or "\x00" in old or "\x00" in new:
        raise ValueError("edit requires non-empty old_str and UTF-8 new_str")
    if not isinstance(expected_occurrences, int) or isinstance(expected_occurrences, bool) or not 1 <= expected_occurrences <= 1000:
        raise ValueError("expected_occurrences must be an integer from 1 to 1000")
    path = safe_path(root, relative, must_exist=True)
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink > 1:
        raise ValueError("edit target must be a regular, non-hard-linked file")
    if path.stat().st_size > max_target_bytes:
        raise ValueError("Edit target exceeds the mutation limit")
    try:
        original = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Only UTF-8 text edits are supported") from exc
    actual = original.count(old)
    if actual != expected_occurrences:
        raise ValueError(f"Edit occurrence conflict: expected {expected_occurrences}, found {actual}")
    content = original.replace(old, new)
    content_bytes = content.encode("utf-8")
    if len(content_bytes) > max_result_bytes:
        raise ValueError("Edited content exceeds the mutation limit")
    old_hash = sha256(path)
    expected = arguments.get("expected_sha256")
    if expected is not None:
        if not isinstance(expected, str) or old_hash != expected:
            raise ValueError(f"Staging hash conflict: {relative}")
    elif not isinstance(expected, type(None)):
        raise ValueError("expected_sha256 must be a string when supplied")
    guard_shrink(relative, path, content)

    diff = bounded_diff(path, original, content, fromfile=relative, tofile=relative)
    checkpoint_id = create_mutation_checkpoint(
        root,
        max_files=max_checkpoint_files,
        max_total_bytes=max_checkpoint_bytes,
    )
    try:
        path.write_text(content, encoding="utf-8", newline="")
    except Exception:
        rollback_mutation(root, checkpoint_id)
        raise
    new_hash = sha256(path)
    changed = int(diff["changed_lines"])
    output = f"Edited {relative}. Changed {changed} line{'s' if changed != 1 else ''}."
    return output, {
        "path": relative,
        "old_sha256": old_hash,
        "new_sha256": new_hash,
        "checkpoint_id": checkpoint_id,
        "expected_occurrences": expected_occurrences,
        "actual_occurrences": actual,
        "size_bytes": len(content_bytes),
        "diff": diff,
        "atomicity": "validated-before-write-with-checkpoint-rollback",
    }
