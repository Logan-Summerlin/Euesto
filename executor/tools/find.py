from __future__ import annotations

import fnmatch
from pathlib import Path

from ..paths import is_secret_path, safe_path


def find(root: Path, arguments: dict, *, max_results: int = 500) -> tuple[str, dict]:
    allowed = {"path", "glob", "max_depth", "max_results", "details"}
    if set(arguments) - allowed:
        raise ValueError("Unknown find arguments")
    relative = arguments.get("path", ".")
    if not isinstance(relative, str):
        raise ValueError("find path must be a string")
    scope = safe_path(root, relative, must_exist=True)
    if not scope.is_dir():
        raise ValueError("find target is not a directory")
    pattern = arguments.get("glob", "*")
    if not isinstance(pattern, str) or not pattern or len(pattern) > 500:
        raise ValueError("find glob must be a bounded non-empty string")
    max_depth = arguments.get("max_depth", 10)
    requested = arguments.get("max_results", 500)
    if not isinstance(max_depth, int) or isinstance(max_depth, bool) or not 0 <= max_depth <= 20:
        raise ValueError("max_depth must be an integer from 0 to 20")
    if not isinstance(requested, int) or isinstance(requested, bool) or not 1 <= requested <= 2000:
        raise ValueError("max_results must be an integer from 1 to 2000")
    maximum = min(requested, max_results)
    details = bool(arguments.get("details", False))
    matches: list[Path] = []
    _walk(root, scope, scope, 0, max_depth, pattern, matches, maximum)
    lines = []
    for path in matches:
        display = path.relative_to(root).as_posix()
        if not details:
            lines.append(display + ("/" if path.is_dir() else ""))
            continue
        kind = "directory" if path.is_dir() else "file"
        size = "-" if path.is_dir() else str(path.stat().st_size)
        lines.append(f"{kind}\t{size}\t{display}")
    truncated = len(matches) >= maximum and _has_more(root, scope, scope, 0, max_depth, pattern, maximum)
    return "\n".join(lines), {
        "count": len(lines),
        "returned": len(lines),
        "limit": maximum,
        "truncated": truncated,
        "details": details,
        "recursive": True,
    }


def _walk(root: Path, directory: Path, scope: Path, depth: int, max_depth: int, pattern: str, matches: list[Path], limit: int) -> None:
    if depth > max_depth or len(matches) >= limit:
        return
    try:
        children = sorted(directory.iterdir(), key=lambda item: item.name.casefold())
    except OSError:
        return
    for path in children:
        if len(matches) >= limit or path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        if is_secret_path(relative) or any(part.startswith(".local-chat-") for part in path.relative_to(root).parts):
            continue
        from_scope = path.relative_to(scope).as_posix()
        if fnmatch.fnmatch(from_scope, pattern) or fnmatch.fnmatch(path.name, pattern):
            matches.append(path)
        if path.is_dir() and depth < max_depth:
            _walk(root, path, scope, depth + 1, max_depth, pattern, matches, limit)


def _has_more(root: Path, directory: Path, scope: Path, depth: int, max_depth: int, pattern: str, limit: int) -> bool:
    count = 0
    stack = [(directory, depth)]
    while stack and count <= limit:
        current, current_depth = stack.pop()
        try:
            children = sorted(current.iterdir(), key=lambda item: item.name.casefold(), reverse=True)
        except OSError:
            continue
        for path in children:
            if path.is_symlink():
                continue
            relative = path.relative_to(root).as_posix()
            if is_secret_path(relative) or any(part.startswith(".local-chat-") for part in path.relative_to(root).parts):
                continue
            if fnmatch.fnmatch(path.relative_to(scope).as_posix(), pattern) or fnmatch.fnmatch(path.name, pattern):
                count += 1
                if count > limit:
                    return True
            if path.is_dir() and current_depth < max_depth:
                stack.append((path, current_depth + 1))
    return False
