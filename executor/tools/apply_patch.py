from __future__ import annotations

import difflib
import hashlib
from pathlib import Path

from ..checkpoints import create_checkpoint, restore_checkpoint
from ..errors import ExecutorToolError
from ..paths import safe_path

MAX_DIFF_LINES = 200
MAX_DIFF_BYTES = 24_000
SHRINK_RATIO = 0.5


def apply_patch(
    root: Path,
    arguments: dict,
    *,
    max_bytes: int,
    max_checkpoint_files: int = 300_000,
    max_checkpoint_bytes: int = 2_000_000_000,
) -> tuple[str, dict]:
    """Apply explicit whole-file or exact replacement edits in staging."""
    if set(arguments) - {"edits"}:
        raise ValueError("Unknown apply_patch arguments")
    edits = arguments.get("edits") or []
    if not isinstance(edits, list) or not 1 <= len(edits) <= 100:
        raise ValueError("apply_patch requires 1-100 edits")

    planned: list[tuple[Path, str, str | None, str | None]] = []
    seen: set[str] = set()
    for edit in edits:
        allowed = {"path", "expected_sha256", "mode", "content", "old_str", "new_str", "expected_occurrences"}
        if not isinstance(edit, dict) or set(edit) - allowed:
            raise ValueError("Invalid apply_patch edit")
        relative = str(edit.get("path") or "")
        _claim_path(relative, seen)
        mode = edit.get("mode")
        if mode not in {"replace_file", "replace_exact"}:
            raise ValueError("apply_patch edit mode is required and must be replace_file or replace_exact")
        path = safe_path(root, relative, must_exist=Path(root, relative).exists())
        current = _current_hash(path)
        expected = edit.get("expected_sha256")
        if current != expected:
            raise ValueError(f"Staging hash conflict: {relative}")

        if mode == "replace_file":
            content = edit.get("content")
            if not isinstance(content, str) or "\x00" in content:
                raise ValueError("replace_file requires UTF-8 text content")
        else:
            if not path.exists():
                raise ValueError(f"Exact replacement target is missing: {relative}")
            if path.stat().st_size > max_bytes:
                raise ValueError(f"Replacement target exceeds the mutation limit: {relative}")
            try:
                original = path.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("Only UTF-8 text edits are supported") from exc
            old = edit.get("old_str")
            new = edit.get("new_str")
            occurrences = edit.get("expected_occurrences", 1)
            if (
                not isinstance(old, str)
                or not old
                or not isinstance(new, str)
                or "\x00" in old
                or "\x00" in new
                or not isinstance(occurrences, int)
                or isinstance(occurrences, bool)
                or not 1 <= occurrences <= 1000
            ):
                raise ValueError("Invalid exact replacement values")
            actual = original.count(old)
            if actual != occurrences:
                raise ValueError(
                    f"Replacement match conflict: {relative} expected {occurrences}, found {actual}"
                )
            content = original.replace(old, new)

        if content is not None and len(content.encode("utf-8")) > max_bytes:
            raise ValueError(f"Patch content exceeds the mutation limit: {relative}")
        _guard_shrink(relative, path, content)
        planned.append((path, relative, current, content))

    total = sum(len((content or "").encode("utf-8")) for _, _, _, content in planned)
    if total > max_bytes:
        raise ValueError("Patch content exceeds the mutation limit")

    diffs: list[dict[str, object]] = []
    for path, relative, _current, content in planned:
        diff = _bounded_diff(path, content)
        diffs.append(diff)

    checkpoint_id = create_checkpoint(
        root,
        max_files=max_checkpoint_files,
        max_total_bytes=max_checkpoint_bytes,
        max_storage_bytes=max_checkpoint_bytes,
    )
    changed: list[dict[str, str | None]] = []
    try:
        for path, relative, current, content in planned:
            if content is None:
                path.unlink()
                staged = None
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8", newline="")
                staged = hashlib.sha256(content.encode("utf-8")).hexdigest()
            changed.append({"path": relative, "base_sha256": current, "staged_sha256": staged})
    except Exception:
        restore_checkpoint(root, checkpoint_id)
        raise
    return f"Applied {len(changed)} staged change(s).", {
        "checkpoint_id": checkpoint_id,
        "changed": changed,
        "diffs": diffs,
        "diff_truncated": any(bool(item["truncated"]) for item in diffs),
        "atomicity": "validated-before-write-with-checkpoint-rollback",
    }


def _guard_shrink(relative: str, path: Path, content: str | None) -> None:
    if content is None or not path.exists() or not path.is_file():
        return
    old_bytes = path.stat().st_size
    new_bytes = len(content.encode("utf-8"))
    try:
        old_lines = path.read_text(encoding="utf-8").count("\n") + 1
    except UnicodeDecodeError:
        return
    new_lines = content.count("\n") + 1
    if old_bytes >= 200 and old_lines >= 20 and new_bytes < old_bytes * SHRINK_RATIO and new_lines < old_lines * SHRINK_RATIO:
        raise ExecutorToolError(
            "staging.shrink_warning",
            f"Whole-file edit for {relative} would shrink the file from {old_bytes} to {new_bytes} bytes and from {old_lines} to {new_lines} lines; review the full replacement before retrying.",
        )


def _bounded_diff(path: Path, content: str | None) -> dict[str, object]:
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    new = content or ""
    lines = list(
        difflib.unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile=str(path),
            tofile=str(path),
            lineterm="",
        )
    )
    truncated = len(lines) > MAX_DIFF_LINES
    if truncated:
        lines = lines[:MAX_DIFF_LINES]
    text = "\n".join(lines)
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_DIFF_BYTES:
        text = encoded[:MAX_DIFF_BYTES].decode("utf-8", errors="ignore")
        truncated = True
    return {"path": path.name, "text": text, "truncated": truncated, "lines": len(lines)}


def _claim_path(relative: str, seen: set[str]) -> None:
    key = relative.casefold()
    if not relative or key in seen:
        raise ValueError("Patch paths must be non-empty and unique")
    seen.add(key)


def _current_hash(path: Path) -> str | None:
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_nlink > 1:
        raise ValueError("Patch targets must be regular, non-hard-linked files")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(128 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
