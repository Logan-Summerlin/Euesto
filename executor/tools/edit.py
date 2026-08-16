from __future__ import annotations

import codecs
import os
import tempfile
from pathlib import Path

from ..mutations import bounded_diff, bounded_edit_diff, create_mutation_checkpoint, guard_shrink, rollback_mutation, sha256
from ..paths import safe_path

EDIT_CHUNK_BYTES = 64 * 1024


def edit(root: Path, arguments: dict, *, max_target_bytes: int, max_result_bytes: int, max_checkpoint_files: int = 300_000, max_checkpoint_bytes: int = 2_000_000_000) -> tuple[str, dict]:
    allowed = {"path", "old_str", "new_str", "expected_occurrences", "expected_sha256"}
    if set(arguments) - allowed: raise ValueError("Unknown edit arguments")
    relative = arguments.get("path"); old = arguments.get("old_str"); new = arguments.get("new_str"); expected_occurrences = arguments.get("expected_occurrences", 1)
    if not isinstance(relative, str) or not relative: raise ValueError("edit requires a file path")
    if not isinstance(old, str) or not old or not isinstance(new, str) or "\x00" in old or "\x00" in new: raise ValueError("edit requires non-empty old_str and UTF-8 new_str")
    if not isinstance(expected_occurrences, int) or isinstance(expected_occurrences, bool) or not 1 <= expected_occurrences <= 1000: raise ValueError("expected_occurrences must be an integer from 1 to 1000")
    path = safe_path(root, relative, must_exist=True)
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink > 1: raise ValueError("edit target must be a regular, non-hard-linked file")
    target_size = path.stat().st_size
    if target_size > max_target_bytes: raise ValueError("Edit target exceeds the mutation limit")
    original_small = path.read_text(encoding="utf-8") if target_size <= 1_000_000 else None
    old_hash = sha256(path)
    expected = arguments.get("expected_sha256")
    if expected is not None:
        if not isinstance(expected, str) or old_hash != expected: raise ValueError(f"Staging hash conflict: {relative}")
    elif not isinstance(expected, type(None)): raise ValueError("expected_sha256 must be a string when supplied")
    old_bytes = old.encode("utf-8"); new_bytes = new.encode("utf-8")
    checkpoint_id = create_mutation_checkpoint(root, max_files=max_checkpoint_files, max_total_bytes=max_checkpoint_bytes)
    temp_path: Path | None = None
    try:
        temp_path, actual, result_size = _stream_replace(path, old_bytes, new_bytes, max_result_bytes)
        if actual != expected_occurrences: raise ValueError(f"Edit occurrence conflict: expected {expected_occurrences}, found {actual}")
        guard_shrink(relative, path, None, replacement_old=old, replacement_new=new, replacement_occurrences=actual)
        if result_size > max_result_bytes: raise ValueError("Edited content exceeds the mutation limit")
        os.replace(temp_path, path); temp_path = None
    except Exception:
        if temp_path is not None: temp_path.unlink(missing_ok=True)
        rollback_mutation(root, checkpoint_id); raise
    new_hash = sha256(path)
    if original_small is not None:
        diff = bounded_diff(path, original_small, path.read_text(encoding="utf-8"), fromfile=relative, tofile=relative)
    else:
        diff = bounded_edit_diff(relative, expected_occurrences, old, new)
    changed = int(diff["changed_lines"]); output = f"Edited {relative}. Changed {changed} line{'s' if changed != 1 else ''}."
    return output, {"path": relative, "old_sha256": old_hash, "new_sha256": new_hash, "checkpoint_id": checkpoint_id, "expected_occurrences": expected_occurrences, "actual_occurrences": actual, "size_bytes": path.stat().st_size, "diff": diff, "atomicity": "validated-before-write-with-checkpoint-rollback"}


def _stream_replace(path: Path, old: bytes, new: bytes, max_result_bytes: int) -> tuple[Path, int, int]:
    fd, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.edit-", dir=path.parent); temp_path = Path(raw_temp)
    decoder = codecs.getincrementaldecoder("utf-8")(); count = 0; result_size = 0; pending = b""; keep = max(1, len(old))
    try:
        with os.fdopen(fd, "wb") as output:
            with path.open("rb") as source:
                while True:
                    chunk = source.read(EDIT_CHUNK_BYTES)
                    if not chunk: break
                    if b"\x00" in chunk: raise ValueError("Only UTF-8 text edits are supported")
                    try: decoder.decode(chunk)
                    except UnicodeDecodeError as exc: raise ValueError("Only UTF-8 text edits are supported") from exc
                    pending += chunk
                    while True:
                        index = pending.find(old)
                        if index < 0: break
                        prefix = pending[:index]; replaced = prefix + new; result_size += len(replaced)
                        if result_size > max_result_bytes: raise ValueError("Edited content exceeds the mutation limit")
                        output.write(replaced); pending = pending[index + len(old):]; count += 1
                    if len(pending) > keep:
                        prefix = pending[:-keep]; result_size += len(prefix)
                        if result_size > max_result_bytes: raise ValueError("Edited content exceeds the mutation limit")
                        output.write(prefix); pending = pending[-keep:]
            try: decoder.decode(b"", final=True)
            except UnicodeDecodeError as exc: raise ValueError("Only UTF-8 text edits are supported") from exc
            replaced = pending.replace(old, new); count += pending.count(old); result_size += len(replaced)
            if result_size > max_result_bytes: raise ValueError("Edited content exceeds the mutation limit")
            output.write(replaced)
        return temp_path, count, result_size
    except Exception:
        temp_path.unlink(missing_ok=True); raise
