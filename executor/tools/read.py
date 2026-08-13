from __future__ import annotations

import hashlib
from pathlib import Path

from ..paths import safe_path

DEFAULT_READ_BYTES = 64_000
MAX_READ_BYTES = 256_000


def read(root: Path, arguments: dict, *, max_bytes: int) -> tuple[str, dict]:
    allowed = {"path", "start_line", "end_line", "max_bytes"}
    if set(arguments) - allowed:
        raise ValueError("Unknown read arguments")
    relative = arguments.get("path")
    if not isinstance(relative, str) or not relative:
        raise ValueError("read requires a file path")
    requested = arguments.get("max_bytes", DEFAULT_READ_BYTES)
    if not isinstance(requested, int) or isinstance(requested, bool) or requested < 1:
        raise ValueError("max_bytes must be a positive integer")
    byte_limit = min(max_bytes, MAX_READ_BYTES, requested)
    try:
        path = safe_path(root, relative, must_exist=True)
    except FileNotFoundError as exc:
        raise ValueError(f"file not found: {relative}") from exc
    if path.is_symlink() or not path.is_file():
        raise ValueError("read requires a regular file")
    stat = path.stat()
    if stat.st_nlink > 1:
        raise ValueError("read rejects hard-linked files")
    size_bytes = stat.st_size
    line_range = "start_line" in arguments or "end_line" in arguments
    if line_range and size_bytes > byte_limit:
        raise ValueError(
            f"line range cannot be validated within max_bytes: size_bytes={size_bytes}, max_bytes={byte_limit}"
        )
    with path.open("rb") as handle:
        raw = handle.read(byte_limit + 4)
    truncated = len(raw) > byte_limit
    raw = raw[:byte_limit]
    if b"\x00" in raw:
        raise ValueError("Binary files are not model-readable")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("File is not valid UTF-8 text") from exc

    lines = text.splitlines()
    start = arguments.get("start_line", 1)
    end = arguments.get("end_line", len(lines))
    if not isinstance(start, int) or isinstance(start, bool) or not isinstance(end, int) or isinstance(end, bool):
        raise ValueError("start_line and end_line must be integers")
    start = max(1, start)
    if line_range and (start > end or start > len(lines) or end > len(lines)):
        raise ValueError(
            f"line range is outside file: start_line={start}, end_line={end}, line_count={len(lines)}"
        )
    selected = lines[start - 1 : end]
    content = "\n".join(selected)
    return content, {
        "path": relative,
        "sha256": _sha256(path),
        "size_bytes": size_bytes,
        "content_bytes": len(content.encode("utf-8")),
        "start_line": start,
        "end_line": start + len(selected) - 1,
        "truncated": truncated,
        "encoding": "utf-8",
        "file_kind": "text",
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(128 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
