from __future__ import annotations

import heapq
import time
from pathlib import Path

from ..errors import INVALID_ARGUMENTS, PATH_INVALID_TYPE, ExecutorToolError
from ..paths import is_tool_excluded, safe_path
from .listing import decode_cursor, encode_cursor, listing_line, requested_results

DEFAULT_LS_RESULTS = 500


def ls(root: Path, arguments: dict, *, max_results: int = DEFAULT_LS_RESULTS, max_seconds: float = 30.0) -> tuple[str, dict]:
    allowed = {"path", "max_results", "details", "cursor"}
    if set(arguments) - allowed: raise ExecutorToolError(INVALID_ARGUMENTS, "Unknown ls arguments")
    relative = arguments.get("path", ".")
    if not isinstance(relative, str): raise ExecutorToolError(INVALID_ARGUMENTS, "ls path must be a string")
    directory = safe_path(root, relative, must_exist=True)
    if not directory.is_dir(): raise ExecutorToolError(PATH_INVALID_TYPE, "ls target is not a directory")
    maximum = min(requested_results(arguments, DEFAULT_LS_RESULTS), max_results)
    cursor = decode_cursor(arguments.get("cursor"), "ls")
    details = bool(arguments.get("details", True))
    deadline = time.monotonic() + max_seconds
    expired = False
    def visible():
        nonlocal expired
        for path in directory.iterdir():
            if time.monotonic() >= deadline: expired = True; return
            if path.is_symlink(): continue
            relative_path = path.relative_to(root).as_posix()
            if is_tool_excluded(relative_path): continue
            yield path
    children = heapq.nsmallest(cursor + maximum + 1, visible(), key=lambda item: item.name.casefold())
    page = children[cursor:cursor + maximum]
    has_more = len(children) > cursor + maximum
    lines = [listing_line(root, path, details) for path in page]
    data: dict[str, object] = {"count": len(lines), "returned": len(lines), "limit": maximum, "truncated": has_more or expired, "details": details, "recursive": False, "total_known": None}
    if expired:
        # Entries not yet visited may sort before the returned page, so no cursor is offered.
        data["truncation_reason"] = "time_budget"; data["max_seconds"] = max_seconds
    elif has_more:
        data["truncation_reason"] = "result_limit"; data["next_cursor"] = encode_cursor(cursor + len(page))
    return "\n".join(lines), data

