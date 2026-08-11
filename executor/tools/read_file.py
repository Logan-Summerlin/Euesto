from __future__ import annotations

import base64
import hashlib
from pathlib import Path

from ..paths import safe_path

DEFAULT_READ_BYTES = 64_000
MAX_READ_BYTES = 256_000
MAX_BATCH_FILES = 20


def read_file(root: Path, arguments: dict, *, max_bytes: int) -> tuple[str, dict]:
    single = arguments.get("path")
    batch = arguments.get("paths")
    if bool(single) == bool(batch):
        raise ValueError("read_file requires exactly one of path or paths")
    if batch is not None:
        if arguments.get("cursor") or arguments.get("start_byte"):
            raise ValueError("Cursors and byte offsets require a single file path")
        if (
            not isinstance(batch, list)
            or not 1 <= len(batch) <= MAX_BATCH_FILES
            or any(not isinstance(item, str) or not item for item in batch)
        ):
            raise ValueError("paths must contain 1-20 explicit file paths")
        paths = list(batch)
    else:
        paths = [str(single)]
    if len({value.casefold() for value in paths}) != len(paths):
        raise ValueError("read_file paths must be unique")

    requested = min(
        max_bytes,
        MAX_READ_BYTES,
        max(1, int(arguments.get("max_bytes") or DEFAULT_READ_BYTES)),
    )
    remaining = requested
    contents: list[str] = []
    metadata: list[dict[str, object]] = []
    for index, relative in enumerate(paths):
        share = remaining // (len(paths) - index)
        content, item = _read_one(root, relative, arguments, share)
        contents.append(content)
        metadata.append(item)
        remaining = max(0, remaining - int(item["content_bytes"]))

    truncated = any(bool(item["truncated"]) for item in metadata)
    if len(paths) == 1:
        return contents[0], {**metadata[0], "count": 1, "truncated": truncated}
    output = "\n\n".join(
        f"--- {item['path']} ---\n{content}"
        for item, content in zip(metadata, contents, strict=True)
    )
    return output, {
        "count": len(metadata),
        "content_bytes": sum(int(item["content_bytes"]) for item in metadata),
        "truncated": truncated,
        "files": metadata,
    }


def _read_one(
    root: Path, relative: str, arguments: dict, byte_limit: int
) -> tuple[str, dict[str, object]]:
    path = safe_path(root, relative, must_exist=True)
    if not path.is_file() or path.stat().st_nlink > 1:
        raise ValueError("read_file requires a regular, non-hard-linked file")
    size_bytes = path.stat().st_size
    offset = _cursor_offset(arguments.get("cursor"))
    if arguments.get("start_byte") is not None:
        offset = max(0, int(arguments.get("start_byte") or 0))
    if offset > size_bytes:
        raise ValueError("Read cursor is past the end of the file")
    with path.open("rb") as handle:
        handle.seek(offset)
        raw = handle.read(byte_limit + 4)
    truncated = len(raw) > byte_limit or offset + min(len(raw), byte_limit) < size_bytes
    raw = raw[:byte_limit]
    if b"\x00" in raw:
        raise ValueError("Binary files are not model-readable")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        while raw and exc.end == len(raw):
            raw = raw[:-1]
            try:
                text = raw.decode("utf-8")
                break
            except UnicodeDecodeError as retry:
                exc = retry
        else:
            raise ValueError("File is not valid UTF-8 text") from exc
    next_cursor = (
        _encode_cursor(offset + len(raw))
        if offset + len(raw) < size_bytes
        else None
    )
    lines = text.splitlines()
    start = max(1, int(arguments.get("start_line") or 1))
    end = int(arguments.get("end_line") or len(lines))
    selected = lines[start - 1 : max(start - 1, end)]
    content = "\n".join(selected)
    metadata = {
        "path": relative,
        "sha256": _sha256(path),
        "size_bytes": size_bytes,
        "content_bytes": len(content.encode("utf-8")),
        "start_line": start,
        "end_line": start + len(selected) - 1,
        "truncated": truncated,
    }
    if next_cursor or arguments.get("cursor") or arguments.get("start_byte") is not None:
        metadata.update(
            {
                "byte_start": offset,
                "byte_end": offset + len(raw),
                "next_cursor": next_cursor,
                "encoding": "utf-8",
                "file_kind": "text",
            }
        )
    return content, metadata


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(128 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(max(0, offset)).encode()).decode().rstrip("=")


def _cursor_offset(value: object) -> int:
    if not value:
        return 0
    try:
        padding = "=" * (-len(str(value)) % 4)
        return max(0, int(base64.urlsafe_b64decode(str(value) + padding).decode()))
    except (ValueError, UnicodeError, base64.binascii.Error):
        raise ValueError("Invalid file result cursor") from None
