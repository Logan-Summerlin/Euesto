from __future__ import annotations

import fnmatch
import os
import re
import time
from collections.abc import Iterator
from pathlib import Path

from ..errors import INVALID_ARGUMENTS, ExecutorToolError
from ..paths import is_tool_excluded, safe_path
from .listing import decode_cursor, encode_cursor


def grep(
    root: Path,
    arguments: dict,
    *,
    max_scan_bytes: int,
    max_output_bytes: int,
    max_results: int = 500,
    max_seconds: float = 30.0,
) -> tuple[str, dict]:
    """Search file contents line by line.

    ``max_scan_bytes`` only decides which candidate files are too large to scan;
    ``max_output_bytes`` independently bounds the combined output returned to the caller.
    """
    scope = safe_path(root, str(arguments.get("path") or "."), must_exist=True)
    pattern = _pattern(arguments)
    limit = min(max_results, max(1, int(arguments.get("max_results") or max_results)))
    include = str(arguments.get("include_glob") or "*")
    exclude = str(arguments.get("exclude_glob") or "")
    context_lines = min(5, max(0, int(arguments.get("context_lines") or 0)))
    include_metadata = bool(arguments.get("include_metadata"))
    skipped_matches = decode_cursor(arguments.get("cursor"), "search", maximum=None)

    started = time.monotonic()
    matches: list[dict[str, object]] = []
    seen_matches = files_considered = files_searched = skipped_large = cumulative_bytes = 0
    truncation_reason: str | None = None
    for path in _iter_files(root, scope):
        if time.monotonic() - started >= max_seconds:
            truncation_reason = "time_budget"
            break
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        if is_tool_excluded(relative) or not _matches_glob(relative, include):
            continue
        if exclude and _matches_glob(relative, exclude):
            continue
        files_considered += 1
        size = path.stat().st_size
        if size > max_scan_bytes:
            skipped_large += 1
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (UnicodeError, OSError):
            continue
        files_searched += 1
        cumulative_bytes += size
        for number, line in enumerate(lines, 1):
            if not pattern.search(line):
                continue
            seen_matches += 1
            if seen_matches <= skipped_matches:
                continue
            if len(matches) >= limit:
                truncation_reason = "result_limit"
                break
            item: dict[str, object] = {"path": relative, "line": number, "text": line[:500]}
            if context_lines:
                item["context_before"] = lines[max(0, number - 1 - context_lines) : number - 1]
                item["context_after"] = lines[number : number + context_lines]
            if include_metadata:
                item["file_kind"] = "text"
                item["encoding"] = "utf-8"
            matches.append(item)
        if truncation_reason:
            break

    truncated = truncation_reason is not None
    data: dict[str, object] = {
        "matches_returned": len(matches),
        "files_considered": files_considered,
        "files_searched": files_searched,
        "truncated": truncated,
        "files_skipped_too_large": skipped_large,
        "scan_scope_complete": not skipped_large and not truncated,
    }
    if truncated:
        data["truncation_reason"] = truncation_reason
        data["cumulative_bytes_scanned"] = cumulative_bytes
        data["elapsed_seconds"] = time.monotonic() - started
        data["next_cursor"] = encode_cursor(max(0, seen_matches - 1))
    if context_lines:
        data["context_lines"] = context_lines
        data["matches"] = matches
    output = "\n".join(f"{item['path']}:{item['line']}:{item['text']}" for item in matches)
    encoded = output.encode("utf-8")
    if len(encoded) <= max_output_bytes:
        return output, data
    data.update(truncated=True, output_truncated=True, max_output_bytes=max_output_bytes)
    return encoded[:max_output_bytes].decode("utf-8", errors="ignore"), data


def _pattern(arguments: dict) -> re.Pattern[str]:
    query = str(arguments.get("query") or "")
    if not query or len(query) > 1000:
        raise ExecutorToolError(INVALID_ARGUMENTS, "A bounded search query is required")
    flags = 0 if arguments.get("case_sensitive") else re.IGNORECASE
    try:
        return re.compile(query if arguments.get("regex") else re.escape(query), flags)
    except re.error as exc:
        raise ExecutorToolError(INVALID_ARGUMENTS, f"Invalid search regex: {exc}") from exc


def _iter_files(root: Path, scope: Path) -> Iterator[Path]:
    if scope.is_file():
        yield scope
        return
    for directory, dirnames, filenames in os.walk(scope, topdown=True):
        current = Path(directory)
        dirnames[:] = sorted(
            name for name in dirnames if not is_tool_excluded(current.joinpath(name).relative_to(root).as_posix())
        )
        for name in sorted(filenames, key=str.casefold):
            yield current / name


def _matches_glob(relative: str, pattern: str) -> bool:
    return fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(Path(relative).name, pattern)
