from __future__ import annotations

import base64
import fnmatch
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
    paths = sorted(directory.rglob("*"), key=lambda item: item.as_posix().casefold())
    start = _decode_cursor(arguments.get("cursor"))
    next_cursor = None
    for index, path in enumerate(paths[start:], start):
        relative_from_dir = path.relative_to(directory)
        if len(relative_from_dir.parts) > max_depth or path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        if is_secret_path(relative):
            continue
        if any(part.startswith(".local-chat-") for part in path.relative_to(root).parts):
            continue
        if fnmatch.fnmatch(relative_from_dir.as_posix(), pattern):
            if len(lines) >= maximum:
                truncated = True
                # The current path triggered the page limit but was not emitted;
                # resume at it so the next page cannot skip a visible entry.
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
    data: dict[str, object] = {"count": len(lines), "truncated": truncated, "details": detailed}
    if include_sha256:
        data["include_sha256"] = True
        data["limit"] = maximum
    if truncated or arguments.get("cursor"):
        data["next_cursor"] = next_cursor
        data["total_known"] = len(paths) if not truncated else None
        data["limit"] = maximum
    return "\n".join(lines), data


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
