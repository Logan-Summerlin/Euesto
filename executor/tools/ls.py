from __future__ import annotations

import heapq
import time
from collections.abc import Iterator
from pathlib import Path

from ..paths import is_tool_excluded
from .listing import (
    decode_cursor,
    encode_cursor,
    listing_directory,
    listing_line,
    requested_results,
)

DEFAULT_LS_RESULTS = 500
LS_ARGUMENTS = frozenset({"path", "max_results", "details", "cursor"})


def ls(
    root: Path,
    arguments: dict,
    *,
    max_results: int = DEFAULT_LS_RESULTS,
    max_seconds: float = 30.0,
) -> tuple[str, dict]:
    directory = listing_directory(root, arguments, LS_ARGUMENTS, "ls")
    maximum = min(requested_results(arguments, DEFAULT_LS_RESULTS), max_results)
    cursor = decode_cursor(arguments.get("cursor"), "ls")
    details = bool(arguments.get("details", True))
    deadline = time.monotonic() + max_seconds
    expired = False

    def visible() -> Iterator[Path]:
        nonlocal expired
        for path in directory.iterdir():
            if time.monotonic() >= deadline:
                expired = True
                return
            if not path.is_symlink() and not is_tool_excluded(
                path.relative_to(root).as_posix()
            ):
                yield path

    children = heapq.nsmallest(
        cursor + maximum + 1, visible(), key=lambda item: item.name.casefold()
    )
    page = children[cursor : cursor + maximum]
    has_more = len(children) > cursor + maximum
    lines = [listing_line(root, path, details) for path in page]
    data: dict[str, object] = {
        "count": len(lines),
        "returned": len(lines),
        "limit": maximum,
        "truncated": has_more or expired,
        "details": details,
        "recursive": False,
        "total_known": None,
    }
    if expired:
        # Entries not yet visited may sort before the returned page, so no cursor is offered.
        data["truncation_reason"] = "time_budget"
        data["max_seconds"] = max_seconds
    elif has_more:
        data["truncation_reason"] = "result_limit"
        data["next_cursor"] = encode_cursor(cursor + len(page))
    return "\n".join(lines), data
