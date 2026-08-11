from __future__ import annotations

import base64
import fnmatch
from collections.abc import Iterator
from pathlib import Path

from ..paths import is_secret_path, safe_path
from ..staging import sha256_file

MAX_HASH_RESULTS = 100


def list_files(root: Path, arguments: dict, *, limit: int = 1000) -> tuple[str, dict]:
    directory = safe_path(root, str(arguments.get("directory") or "."), must_exist=True)
    if not directory.is_dir():
        raise ValueError("list_files target is not a directory")
    maximum = min(limit, max(1, int(arguments.get("max_results") or 500)))
    max_depth = min(20, max(0, int(arguments.get("max_depth") or 8)))
    pattern = str(arguments.get("glob") or "*")
    detailed = bool(arguments.get("details"))
    include_sha256 = bool(arguments.get("include_sha256"))
    if include_sha256:
        maximum = min(maximum, MAX_HASH_RESULTS)
    lines: list[str] = []
    truncated = False
    matched_count = 0
    start = _decode_cursor(arguments.get("cursor"))
    next_cursor = None

    for index, path in enumerate(_iter_paths(directory, max_depth), start=0):
        if index < start:
            continue
        relative_from_dir = path.relative_to(directory)
        if path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        if is_secret_path(relative):
            continue
        if any(part.startswith(".local-chat-") for part in path.relative_to(root).parts):
            continue
        if not fnmatch.fnmatch(relative_from_dir.as_posix(), pattern):
            continue
        matched_count += 1
        if len(lines) >= maximum:
            truncated = True
            next_cursor = _encode_cursor(index)
            break
        display = relative + ("/" if path.is_dir() else "")
        if include_sha256:
            kind = "directory" if path.is_dir() else "file"
            size = "-" if path.is_dir() else str(path.stat().st_size)
            digest = "-" if path.is_dir() else sha256_file(path)
            lines.append(f"{kind}\t{size}\t{digest}\t{display}")
        elif detailed:
            kind = "directory" if path.is_dir() else "file"
            size = "-" if path.is_dir() else str(path.stat().st_size)
            lines.append(f"{kind}\t{size}\t{display}")
        else:
            lines.append(display)

    data: dict[str, object] = {
        "count": len(lines),
        "truncated": truncated,
        "has_more": truncated,
        "details": detailed,
    }
    if include_sha256:
        data["include_sha256"] = True
        data["limit"] = maximum
    if truncated:
        data["next_cursor"] = next_cursor
        data["limit"] = maximum
    elif not arguments.get("cursor"):
        data["total_known"] = matched_count
    else:
        data["next_cursor"] = None
        data["limit"] = maximum
    return "\n".join(lines), data


def _iter_paths(directory: Path, max_depth: int) -> Iterator[Path]:
    """Yield paths in deterministic order without materializing the tree."""
    if max_depth <= 0:
        return
    yield from _walk(directory, directory, 0, max_depth)


def _walk(directory: Path, root: Path, depth: int, max_depth: int) -> Iterator[Path]:
    try:
        children = sorted(directory.iterdir(), key=lambda item: item.name.casefold())
    except OSError:
        return
    for path in children:
        yield path
        if depth >= max_depth - 1 or path.is_symlink() or not path.is_dir():
            continue
        relative = path.relative_to(root).as_posix()
        if is_secret_path(relative) or any(
            part.startswith(".local-chat-") for part in path.relative_to(root).parts
        ):
            continue
        yield from _walk(path, root, depth + 1, max_depth)


def _encode_cursor(index: int) -> str:
    return base64.urlsafe_b64encode(str(max(0, index)).encode()).decode().rstrip("=")


def _decode_cursor(value: object) -> int:
    if not value:
        return 0
    try:
        padding = "=" * (-len(str(value)) % 4)
        return max(0, int(base64.urlsafe_b64decode(str(value) + padding).decode()))
    except (ValueError, UnicodeError, base64.binascii.Error):
        raise ValueError("Invalid list result cursor") from None
