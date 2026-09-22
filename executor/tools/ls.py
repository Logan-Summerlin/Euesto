from __future__ import annotations

import base64
import heapq
import time
from pathlib import Path

from ..errors import INVALID_ARGUMENTS, PATH_INVALID_TYPE, ExecutorToolError
from ..paths import is_tool_excluded, safe_path

MAX_CURSOR_OFFSET = 100_000
DEFAULT_LS_RESULTS = 500


def ls(root: Path, arguments: dict, *, max_results: int = DEFAULT_LS_RESULTS, max_seconds: float = 30.0) -> tuple[str, dict]:
    allowed = {"path", "max_results", "details", "cursor"}
    if set(arguments) - allowed: raise ExecutorToolError(INVALID_ARGUMENTS, "Unknown ls arguments")
    relative = arguments.get("path", ".")
    if not isinstance(relative, str): raise ExecutorToolError(INVALID_ARGUMENTS, "ls path must be a string")
    directory = safe_path(root, relative, must_exist=True)
    if not directory.is_dir(): raise ExecutorToolError(PATH_INVALID_TYPE, "ls target is not a directory")
    requested = arguments.get("max_results", DEFAULT_LS_RESULTS)
    if not isinstance(requested, int) or isinstance(requested, bool) or not 1 <= requested <= 2000: raise ExecutorToolError(INVALID_ARGUMENTS, "max_results must be an integer from 1 to 2000")
    maximum = min(requested, max_results); cursor = _decode_cursor(arguments.get("cursor")); details = bool(arguments.get("details", True)); needed = cursor + maximum + 1
    deadline = time.monotonic() + max_seconds; expired = False
    def visible():
        nonlocal expired
        for path in directory.iterdir():
            if time.monotonic() >= deadline: expired = True; return
            if path.is_symlink(): continue
            relative_path = path.relative_to(root).as_posix()
            if is_tool_excluded(relative_path): continue
            yield path
    children = heapq.nsmallest(needed, visible(), key=lambda item: item.name.casefold()); page = children[cursor:cursor + maximum]; has_more = len(children) > cursor + maximum
    lines = []
    for path in page:
        display = path.relative_to(root).as_posix()
        if not details: lines.append(display + ("/" if path.is_dir() else "")); continue
        kind = "directory" if path.is_dir() else "file"; size = "-" if path.is_dir() else str(path.stat().st_size); lines.append(f"{kind}\t{size}\t{display}")
    data: dict[str, object] = {"count": len(lines), "returned": len(lines), "limit": maximum, "truncated": has_more or expired, "details": details, "recursive": False, "total_known": None}
    if expired:
        # Entries not yet visited may sort before the returned page, so no cursor is offered.
        data["truncation_reason"] = "time_budget"; data["max_seconds"] = max_seconds
    elif has_more:
        data["truncation_reason"] = "result_limit"; data["next_cursor"] = _encode_cursor(cursor + len(page))
    return "\n".join(lines), data


def _encode_cursor(value: int) -> str: return base64.urlsafe_b64encode(str(max(0, value)).encode()).decode().rstrip("=")

def _decode_cursor(value: object) -> int:
    if not value: return 0
    try:
        padding = "=" * (-len(str(value)) % 4); parsed = int(base64.urlsafe_b64decode(str(value) + padding).decode())
    except (ValueError, UnicodeError, base64.binascii.Error): raise ExecutorToolError(INVALID_ARGUMENTS, "Invalid ls result cursor") from None
    if parsed < 0 or parsed > MAX_CURSOR_OFFSET: raise ExecutorToolError(INVALID_ARGUMENTS, "Ls result cursor is outside the bounded pagination range")
    return parsed
