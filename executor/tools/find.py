from __future__ import annotations

import base64
import fnmatch
from pathlib import Path
from typing import Iterator

from ..paths import is_secret_path, safe_path

MAX_CURSOR_OFFSET = 100_000


def find(root: Path, arguments: dict, *, max_results: int = 500) -> tuple[str, dict]:
    allowed = {"path", "glob", "max_depth", "max_results", "details", "cursor"}
    if set(arguments) - allowed: raise ValueError("Unknown find arguments")
    relative = arguments.get("path", ".")
    if not isinstance(relative, str): raise ValueError("find path must be a string")
    scope = safe_path(root, relative, must_exist=True)
    if not scope.is_dir(): raise ValueError("find target is not a directory")
    pattern = arguments.get("glob", "*")
    if not isinstance(pattern, str) or not pattern or len(pattern) > 500: raise ValueError("find glob must be a bounded non-empty string")
    max_depth = arguments.get("max_depth", 10); requested = arguments.get("max_results", 500)
    if not isinstance(max_depth, int) or isinstance(max_depth, bool) or not 0 <= max_depth <= 20: raise ValueError("max_depth must be an integer from 0 to 20")
    if not isinstance(requested, int) or isinstance(requested, bool) or not 1 <= requested <= 2000: raise ValueError("max_results must be an integer from 1 to 2000")
    maximum = min(requested, max_results); cursor = _decode_cursor(arguments.get("cursor")); details = bool(arguments.get("details", False))
    matches: list[Path] = []; skipped = 0; iterator = _iter_matches(root, scope, scope, 0, max_depth, pattern)
    for path in iterator:
        if skipped < cursor: skipped += 1; continue
        matches.append(path)
        if len(matches) >= maximum: break
    has_more = next(iterator, None) is not None
    lines = []
    for path in matches:
        display = path.relative_to(root).as_posix()
        if not details: lines.append(display + ("/" if path.is_dir() else "")); continue
        kind = "directory" if path.is_dir() else "file"; size = "-" if path.is_dir() else str(path.stat().st_size); lines.append(f"{kind}\t{size}\t{display}")
    data: dict[str, object] = {"count": len(lines), "returned": len(lines), "limit": maximum, "truncated": has_more, "details": details, "recursive": True, "total_known": None}
    if has_more: data["next_cursor"] = _encode_cursor(cursor + len(matches))
    return "\n".join(lines), data


def _iter_matches(root: Path, directory: Path, scope: Path, depth: int, max_depth: int, pattern: str) -> Iterator[Path]:
    try: children = sorted(directory.iterdir(), key=lambda item: item.name.casefold())
    except OSError: return
    for path in children:
        if path.is_symlink(): continue
        relative = path.relative_to(root).as_posix()
        if is_secret_path(relative) or any(part.startswith(".local-chat-") for part in path.relative_to(root).parts): continue
        if fnmatch.fnmatch(path.relative_to(scope).as_posix(), pattern) or fnmatch.fnmatch(path.name, pattern): yield path
        if path.is_dir() and depth < max_depth: yield from _iter_matches(root, path, scope, depth + 1, max_depth, pattern)


def _encode_cursor(value: int) -> str: return base64.urlsafe_b64encode(str(max(0, value)).encode()).decode().rstrip("=")

def _decode_cursor(value: object) -> int:
    if not value: return 0
    try:
        padding = "=" * (-len(str(value)) % 4); parsed = int(base64.urlsafe_b64decode(str(value) + padding).decode())
    except (ValueError, UnicodeError, base64.binascii.Error): raise ValueError("Invalid find result cursor") from None
    if parsed < 0 or parsed > MAX_CURSOR_OFFSET: raise ValueError("Find result cursor is outside the bounded pagination range")
    return parsed
