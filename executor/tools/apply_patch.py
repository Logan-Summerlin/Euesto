from __future__ import annotations

import hashlib
from pathlib import Path

from ..checkpoints import create_checkpoint, restore_checkpoint
from ..paths import safe_path


def apply_patch(
    root: Path,
    arguments: dict,
    *,
    max_bytes: int,
    max_checkpoint_files: int = 300_000,
    max_checkpoint_bytes: int = 2_000_000_000,
) -> tuple[str, dict]:
    """Apply preconditioned whole-file edits or exact replacements in staging."""
    if set(arguments) - {"edits", "replacements"}:
        raise ValueError("Unknown apply_patch arguments")
    edits = arguments.get("edits") or []
    replacements = arguments.get("replacements") or []
    if not isinstance(edits, list) or not isinstance(replacements, list):
        raise ValueError("edits and replacements must be arrays")
    if not 1 <= len(edits) + len(replacements) <= 100:
        raise ValueError("apply_patch requires 1-100 changes")

    planned: list[tuple[Path, str, str | None, str | None]] = []
    seen: set[str] = set()
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) - {"path", "expected_sha256", "content"}:
            raise ValueError("Invalid typed edit")
        relative = str(edit.get("path") or "")
        _claim_path(relative, seen)
        path = safe_path(root, relative, must_exist=Path(root, relative).exists())
        current = _current_hash(path)
        expected = edit.get("expected_sha256")
        if current != expected:
            raise ValueError(f"Staging hash conflict: {relative}")
        content = edit.get("content")
        if content is None:
            if not path.exists():
                raise ValueError(f"Cannot delete missing file: {relative}")
        elif not isinstance(content, str) or "\x00" in content:
            raise ValueError("Only UTF-8 text edits are supported")
        planned.append((path, relative, current, content))

    for replacement in replacements:
        allowed = {
            "path",
            "expected_sha256",
            "old_text",
            "new_text",
            "expected_occurrences",
        }
        if not isinstance(replacement, dict) or set(replacement) - allowed:
            raise ValueError("Invalid exact replacement")
        relative = str(replacement.get("path") or "")
        _claim_path(relative, seen)
        path = safe_path(root, relative, must_exist=True)
        current = _current_hash(path)
        expected = replacement.get("expected_sha256")
        if not isinstance(expected, str) or current != expected:
            raise ValueError(f"Staging hash conflict: {relative}")
        if path.stat().st_size > max_bytes:
            raise ValueError(f"Replacement target exceeds the mutation limit: {relative}")
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Only UTF-8 text edits are supported") from exc
        old = replacement.get("old_text")
        new = replacement.get("new_text")
        occurrences = replacement.get("expected_occurrences", 1)
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
        actual = content.count(old)
        if actual != occurrences:
            raise ValueError(
                f"Replacement match conflict: {relative} expected {occurrences}, found {actual}"
            )
        planned.append((path, relative, current, content.replace(old, new)))

    total = sum(len((content or "").encode("utf-8")) for _, _, _, content in planned)
    if total > max_bytes:
        raise ValueError("Patch content exceeds the mutation limit")

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
        "atomicity": "validated-before-write-with-checkpoint-rollback",
    }


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
