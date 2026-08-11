from __future__ import annotations

import base64
import fnmatch
import re
from pathlib import Path

from ..paths import is_secret_path, safe_path


def search_text(root: Path, arguments: dict, *, max_bytes: int, max_results: int = 200) -> tuple[str, dict]:
    scope = safe_path(root, str(arguments.get("path") or "."), must_exist=True)
    query = str(arguments.get("query") or "")
    if not query or len(query) > 1000:
        raise ValueError("A bounded search query is required")
    limit = min(max_results, max(1, int(arguments.get("max_results") or 100)))
    flags = 0 if arguments.get("case_sensitive") else re.IGNORECASE
    try:
        pattern = re.compile(query if arguments.get("regex") else re.escape(query), flags)
    except re.error as exc:
        raise ValueError(f"Invalid search regex: {exc}") from exc
    include = str(arguments.get("include_glob") or "*")
    exclude = str(arguments.get("exclude_glob") or "")
    files = [scope] if scope.is_file() else sorted(scope.rglob("*"), key=lambda item: item.as_posix().casefold())
    matches: list[dict[str, object]] = []
    files_scanned = 0
    truncated = False
    skipped_large = 0
    context_lines = min(5, max(0, int(arguments.get("context_lines") or 0)))
    include_metadata = bool(arguments.get("include_metadata"))
    skipped_matches = _decode_cursor(arguments.get("cursor"))
    seen_matches = 0
    for path in files:
        if not path.is_file() or path.is_symlink():
            continue
        if path.stat().st_size > max_bytes:
            skipped_large += 1
            continue
        relative = path.relative_to(root).as_posix()
        if is_secret_path(relative) or any(
            part.startswith(".local-chat-") for part in path.relative_to(root).parts
        ):
            continue
        if not _matches_glob(relative, include) or (
            exclude and _matches_glob(relative, exclude)
        ):
            continue
        files_scanned += 1
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeError, OSError):
            continue
        text_lines = text.splitlines()
        for number, line in enumerate(text_lines, 1):
            if pattern.search(line):
                seen_matches += 1
                if seen_matches <= skipped_matches:
                    continue
                if len(matches) >= limit:
                    truncated = True
                    break
                before = text_lines[max(0, number - 1 - context_lines) : number - 1]
                after = text_lines[number : number + context_lines]
                item: dict[str, object] = {
                    "path": relative,
                    "line": number,
                    "text": line[:500],
                }
                if context_lines:
                    item["context_before"] = before
                    item["context_after"] = after
                if include_metadata:
                    item["file_kind"] = "text"
                    item["encoding"] = "utf-8"
                matches.append(item)
        if truncated:
            break
    output = "\n".join(
        f"{item['path']}:{item['line']}:{item['text']}" for item in matches
    )
    data: dict[str, object] = {
        "matches_returned": len(matches),
        "files_scanned": files_scanned,
        "truncated": truncated,
    }
    if truncated:
        data["next_cursor"] = _encode_cursor(max(0, seen_matches - 1))
    if context_lines:
        data["context_lines"] = context_lines
        data["matches"] = matches
    if include_metadata:
        data["files_skipped_too_large"] = skipped_large
    return output, data


def _matches_glob(relative: str, pattern: str) -> bool:
    return fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(Path(relative).name, pattern)


def _encode_cursor(value: int) -> str:
    return base64.urlsafe_b64encode(str(max(0, value)).encode()).decode().rstrip("=")


def _decode_cursor(value: object) -> int:
    if not value:
        return 0
    try:
        padding = "=" * (-len(str(value)) % 4)
        return max(0, int(base64.urlsafe_b64decode(str(value) + padding).decode()))
    except (ValueError, UnicodeError, base64.binascii.Error):
        raise ValueError("Invalid search result cursor") from None
