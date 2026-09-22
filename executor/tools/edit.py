from __future__ import annotations

import codecs
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..errors import ExecutorToolError
from ..mutations import bounded_diff, bounded_edit_diff, create_mutation_checkpoint, guard_shrink, rollback_mutation, sha256
from ..paths import safe_path

EDIT_CHUNK_BYTES = 64 * 1024
EDIT_DIFF_MEMORY_BYTES = 1_000_000
EDIT_ARGUMENTS = frozenset({"path", "old_str", "new_str", "expected_occurrences", "expected_sha256"})
MAX_DIAGNOSTIC_MATCH_LINES = 10
MAX_DIAGNOSTIC_PREVIEW_CHARS = 240


@dataclass(frozen=True, slots=True)
class PreparedEdit:
    relative: str
    path: Path
    old: str
    new: str
    expected_occurrences: int
    old_hash: str
    original_small: str | None


@dataclass(frozen=True, slots=True)
class AppliedEdit:
    old: str
    new: str
    actual_occurrences: int
    line_ending_adjustment: str | None
    shrink_warning: dict | None


def edit(root: Path, arguments: dict, *, max_target_bytes: int, max_result_bytes: int, max_checkpoint_files: int = 300_000, max_checkpoint_bytes: int = 2_000_000_000) -> tuple[str, dict]:
    if set(arguments) - EDIT_ARGUMENTS: raise ValueError("Unknown edit arguments")
    prepared = prepare_edit(root, arguments, max_target_bytes=max_target_bytes)
    checkpoint_id = create_mutation_checkpoint(root, max_files=max_checkpoint_files, max_total_bytes=max_checkpoint_bytes)
    try:
        applied = apply_edit(prepared, max_result_bytes=max_result_bytes)
    except BaseException:
        rollback_mutation(root, checkpoint_id); raise
    data = edit_result(prepared, applied)
    data["checkpoint_id"] = checkpoint_id
    changed = int(data["diff"]["changed_lines"]); output = f"Edited {prepared.relative}. Changed {changed} line{'s' if changed != 1 else ''}."
    if applied.line_ending_adjustment: output += f" Matched after line-ending translation ({applied.line_ending_adjustment})."
    if applied.shrink_warning: output += " Warning: confirmed large shrink."
    return output, data


def prepare_edit(root: Path, arguments: dict, *, max_target_bytes: int) -> PreparedEdit:
    """Validate one exact edit against the current staged state without changing anything."""
    relative = arguments.get("path"); old = arguments.get("old_str"); new = arguments.get("new_str"); expected_occurrences = arguments.get("expected_occurrences", 1)
    if not isinstance(relative, str) or not relative: raise ValueError("edit requires a file path")
    if not isinstance(old, str) or not old or not isinstance(new, str) or "\x00" in old or "\x00" in new:
        raise ExecutorToolError("edit.malformed_context", "edit requires a non-empty old_str and a new_str without NUL characters", details={"failure": "malformed_context", "path": relative})
    if not isinstance(expected_occurrences, int) or isinstance(expected_occurrences, bool) or not 1 <= expected_occurrences <= 1000:
        raise ExecutorToolError("edit.malformed_context", "expected_occurrences must be an integer from 1 to 1000", details={"failure": "malformed_context", "path": relative})
    path = safe_path(root, relative, must_exist=True)
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink > 1: raise ValueError("edit target must be a regular, non-hard-linked file")
    target_size = path.stat().st_size
    if target_size > max_target_bytes: raise ValueError("Edit target exceeds the mutation limit")
    original_small = path.read_text(encoding="utf-8") if target_size <= EDIT_DIFF_MEMORY_BYTES else None
    old_hash = sha256(path)
    expected = arguments.get("expected_sha256")
    if expected is not None:
        if not isinstance(expected, str): raise ValueError("expected_sha256 must be a string when supplied")
        if old_hash != expected:
            raise ExecutorToolError("staging.conflict", f"Staging hash conflict: {relative}", retryable=True, details={"failure": "hash_conflict", "path": relative, "expected_sha256": expected, "actual_sha256": old_hash})
    return PreparedEdit(relative, path, old, new, expected_occurrences, old_hash, original_small)


def apply_edit(prepared: PreparedEdit, *, max_result_bytes: int) -> AppliedEdit:
    """Apply a prepared edit; the caller owns the surrounding checkpoint.

    Newline policy: the exact ``old_str`` is tried first. Only when it matches nothing and it
    spans a line break is it retried once with its line breaks translated to the other
    convention (LF <-> CRLF), and ``new_str`` is translated the same way so the file keeps
    its own convention. The occurrence count must still match exactly; nothing fuzzy is
    ever applied.
    """
    path = prepared.path; old = prepared.old; new = prepared.new; adjustment = None
    temp_path, actual, result_size = _stream_replace(path, old.encode("utf-8"), new.encode("utf-8"), max_result_bytes)
    try:
        if actual == 0:
            variant = _line_ending_variant(old, new)
            if variant is not None:
                alt_old, alt_new, label = variant
                alt_temp, alt_actual, alt_size = _stream_replace(path, alt_old.encode("utf-8"), alt_new.encode("utf-8"), max_result_bytes)
                if alt_actual:
                    temp_path.unlink(missing_ok=True)
                    temp_path, actual, result_size, old, new, adjustment = alt_temp, alt_actual, alt_size, alt_old, alt_new, label
                else:
                    alt_temp.unlink(missing_ok=True)
        if actual != prepared.expected_occurrences:
            raise _match_error(prepared, actual, old, adjustment)
        shrink_warning = guard_shrink(prepared.relative, path, None, replacement_old=old, replacement_new=new, replacement_occurrences=actual, advisory=True)
        if result_size > max_result_bytes: raise ValueError("Edited content exceeds the mutation limit")
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True); raise
    return AppliedEdit(old, new, actual, adjustment, shrink_warning)


def edit_result(prepared: PreparedEdit, applied: AppliedEdit) -> dict:
    path = prepared.path
    if prepared.original_small is not None:
        diff = bounded_diff(path, prepared.original_small, path.read_text(encoding="utf-8"), fromfile=prepared.relative, tofile=prepared.relative)
    else:
        diff = bounded_edit_diff(prepared.relative, applied.actual_occurrences, applied.old, applied.new)
    data = {"path": prepared.relative, "old_sha256": prepared.old_hash, "new_sha256": sha256(path), "expected_occurrences": prepared.expected_occurrences, "actual_occurrences": applied.actual_occurrences, "size_bytes": path.stat().st_size, "diff": diff, "line_ending_adjustment": applied.line_ending_adjustment, "atomicity": "validated-before-write-with-checkpoint-rollback"}
    if applied.shrink_warning:
        data["shrink_warning"] = True
        data["shrink_details"] = applied.shrink_warning
    return data


def _line_ending_variant(old: str, new: str) -> tuple[str, str, str] | None:
    if "\r\n" in old:
        return old.replace("\r\n", "\n"), new.replace("\r\n", "\n"), "crlf_to_lf"
    if "\n" in old:
        return old.replace("\n", "\r\n"), new.replace("\r\n", "\n").replace("\n", "\r\n"), "lf_to_crlf"
    return None


def _match_error(prepared: PreparedEdit, actual: int, matched_old: str, adjustment: str | None) -> ExecutorToolError:
    expected = prepared.expected_occurrences
    if actual == 0:
        code, failure = "edit.no_match", "zero_matches"
    elif actual > expected:
        code, failure = "edit.too_many_matches", "too_many_matches"
    else:
        code, failure = "edit.too_few_matches", "too_few_matches"
    details: dict[str, object] = {"failure": failure, "path": prepared.relative, "expected_occurrences": expected, "actual_occurrences": actual, "line_ending_adjustment": adjustment}
    hint = ""
    if prepared.original_small is not None:
        details.update(_diagnostics(prepared.original_small, matched_old if actual else prepared.old, actual))
        if details.get("hints"):
            hint = " Hints: " + "; ".join(str(item) for item in details["hints"]) + "."
    else:
        details["diagnostics"] = "omitted for a target larger than 1,000,000 bytes"
    message = f"Edit occurrence conflict: expected {expected}, found {actual} in {prepared.relative} ({failure.replace('_', ' ')}).{hint}"
    return ExecutorToolError(code, message, details=details)


def _diagnostics(text: str, old: str, actual: int) -> dict[str, object]:
    """Bounded, escaped hints about why an exact edit did not match; never applied."""
    result: dict[str, object] = {"file_line_endings": _line_endings(text), "old_str_line_endings": _line_endings(old)}
    hints: list[str] = []
    if actual:
        lines: list[int] = []; start = 0
        while len(lines) < MAX_DIAGNOSTIC_MATCH_LINES:
            index = text.find(old, start)
            if index < 0: break
            lines.append(text.count("\n", 0, index) + 1); start = index + 1
        result["match_lines"] = lines
        hints.append("set expected_occurrences to the intended count or add surrounding context to old_str")
        return {**result, "hints": hints}
    if result["file_line_endings"] != result["old_str_line_endings"] and "none" not in (result["file_line_endings"], result["old_str_line_endings"]):
        hints.append("line endings differ between old_str and the file")
    if _collapse(old) and _collapse(old) in _collapse(text):
        hints.append("old_str matches only if whitespace or indentation is ignored; copy it exactly from a fresh read")
    first = next((line.strip() for line in old.splitlines() if line.strip()), "")
    if first:
        for number, line in enumerate(text.splitlines(), 1):
            if first in line:
                result["closest_line"] = number
                offset = sum(len(item) for item in text.splitlines(keepends=True)[: number - 1])
                excerpt = text[max(0, offset - 40): offset + MAX_DIAGNOSTIC_PREVIEW_CHARS]
                result["context_preview"] = repr(excerpt)[: MAX_DIAGNOSTIC_PREVIEW_CHARS + 2]
                hints.append(f"the first non-blank line of old_str appears at line {number}; compare the context preview")
                break
    if not hints:
        hints.append("no similar text found; re-read the file before retrying")
    result["hints"] = hints
    return result


def _line_endings(value: str) -> str:
    crlf = value.count("\r\n"); lf = value.count("\n") - crlf
    if not crlf and not lf: return "none"
    if crlf and lf: return "mixed"
    return "crlf" if crlf else "lf"


def _collapse(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


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
    except BaseException:
        temp_path.unlink(missing_ok=True); raise
