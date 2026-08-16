from __future__ import annotations

from pathlib import Path

from ..paths import is_secret_path, safe_path


def ls(root: Path, arguments: dict, *, max_results: int = 500) -> tuple[str, dict]:
    allowed = {"path", "max_results", "details"}
    if set(arguments) - allowed:
        raise ValueError("Unknown ls arguments")
    relative = arguments.get("path", ".")
    if not isinstance(relative, str):
        raise ValueError("ls path must be a string")
    directory = safe_path(root, relative, must_exist=True)
    if not directory.is_dir():
        raise ValueError("ls target is not a directory")
    requested = arguments.get("max_results", 200)
    if not isinstance(requested, int) or isinstance(requested, bool) or not 1 <= requested <= 2000:
        raise ValueError("max_results must be an integer from 1 to 2000")
    maximum = min(requested, max_results)
    details = bool(arguments.get("details", True))
    children = []
    for path in sorted(directory.iterdir(), key=lambda item: item.name.casefold()):
        if path.is_symlink():
            continue
        relative_path = path.relative_to(root).as_posix()
        if is_secret_path(relative_path) or any(part.startswith(".local-chat-") for part in path.relative_to(root).parts):
            continue
        children.append(path)
    truncated = len(children) > maximum
    children = children[:maximum]
    lines = []
    for path in children:
        display = path.relative_to(root).as_posix()
        if not details:
            lines.append(display + ("/" if path.is_dir() else ""))
        else:
            kind = "directory" if path.is_dir() else "file"
            size = "-" if path.is_dir() else str(path.stat().st_size)
            lines.append(f"{kind}\t{size}\t{display}")
    return "\n".join(lines), {
        "count": len(lines),
        "returned": len(lines),
        "limit": maximum,
        "truncated": truncated,
        "details": details,
        "recursive": False,
    }
