from __future__ import annotations

import fnmatch
import time
from collections.abc import Iterator
from pathlib import Path

from ..errors import INVALID_ARGUMENTS, PATH_INVALID_TYPE, ExecutorToolError
from ..paths import is_tool_excluded, safe_path
from .listing import decode_cursor, encode_cursor, listing_line, requested_results

DEFAULT_FIND_RESULTS = 500


def find(root: Path, arguments: dict, *, max_results: int = DEFAULT_FIND_RESULTS, max_seconds: float = 30.0) -> tuple[str, dict]:
    allowed = {"path", "glob", "max_depth", "max_results", "details", "cursor"}
    if set(arguments) - allowed: raise ExecutorToolError(INVALID_ARGUMENTS, "Unknown find arguments")
    relative = arguments.get("path", ".")
    if not isinstance(relative, str): raise ExecutorToolError(INVALID_ARGUMENTS, "find path must be a string")
    scope = safe_path(root, relative, must_exist=True)
    if not scope.is_dir(): raise ExecutorToolError(PATH_INVALID_TYPE, "find target is not a directory")
    pattern = arguments.get("glob", "*")
    if not isinstance(pattern, str) or not pattern or len(pattern) > 500: raise ExecutorToolError(INVALID_ARGUMENTS, "find glob must be a bounded non-empty string")
    max_depth = arguments.get("max_depth", 10)
    if not isinstance(max_depth, int) or isinstance(max_depth, bool) or not 0 <= max_depth <= 20: raise ExecutorToolError(INVALID_ARGUMENTS, "max_depth must be an integer from 0 to 20")
    maximum = min(requested_results(arguments, DEFAULT_FIND_RESULTS), max_results)
    cursor = decode_cursor(arguments.get("cursor"), "find")
    details = bool(arguments.get("details", False))
    walk = _Walk(time.monotonic() + max_seconds); matches: list[Path] = []; skipped = 0; iterator = _iter_matches(root, scope, scope, 0, max_depth, pattern, walk)
    for path in iterator:
        if skipped < cursor: skipped += 1; continue
        matches.append(path)
        if len(matches) >= maximum: break
    full = len(matches) >= maximum
    # Once a full page is collected, an expiring look-ahead only means more entries may remain.
    has_more = full and (next(iterator, None) is not None or walk.expired); timed_out = walk.expired and not full
    lines = [listing_line(root, path, details) for path in matches]
    data: dict[str, object] = {"count": len(lines), "returned": len(lines), "limit": maximum, "truncated": has_more or timed_out, "details": details, "recursive": True, "total_known": None}
    if timed_out:
        # A cursor would replay the same walk into the same budget; narrow path/glob/max_depth instead.
        data["truncation_reason"] = "time_budget"; data["max_seconds"] = max_seconds
    elif has_more:
        data["truncation_reason"] = "result_limit"; data["next_cursor"] = encode_cursor(cursor + len(matches))
    return "\n".join(lines), data


class _Walk:
    """Wall-clock budget shared across the recursive traversal."""
    __slots__ = ("deadline", "expired")

    def __init__(self, deadline: float) -> None:
        self.deadline = deadline; self.expired = False

    def exhausted(self) -> bool:
        if not self.expired and time.monotonic() >= self.deadline: self.expired = True
        return self.expired


def _iter_matches(root: Path, directory: Path, scope: Path, depth: int, max_depth: int, pattern: str, walk: _Walk) -> Iterator[Path]:
    if walk.exhausted(): return
    try: children = sorted(directory.iterdir(), key=lambda item: item.name.casefold())
    except OSError: return
    for path in children:
        if walk.exhausted(): return
        if path.is_symlink(): continue
        relative = path.relative_to(root).as_posix()
        if is_tool_excluded(relative): continue
        if fnmatch.fnmatch(path.relative_to(scope).as_posix(), pattern) or fnmatch.fnmatch(path.name, pattern): yield path
        if path.is_dir() and depth < max_depth: yield from _iter_matches(root, path, scope, depth + 1, max_depth, pattern, walk)

