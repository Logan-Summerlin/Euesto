from __future__ import annotations

import codecs
import hashlib
from pathlib import Path

from ..paths import safe_path

DEFAULT_READ_BYTES = 64_000
MAX_READ_BYTES = 256_000
READ_CHUNK_BYTES = 64 * 1024


def read(root: Path, arguments: dict, *, max_bytes: int) -> tuple[str, dict]:
    allowed = {"path", "start_line", "end_line", "offset", "max_bytes"}
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
    _validate_text_file(path)

    has_range = "start_line" in arguments or "end_line" in arguments
    has_offset = "offset" in arguments
    if has_range and has_offset:
        raise ValueError("read cannot combine line ranges with byte offsets")

    start_line = arguments.get("start_line", 1)
    end_line = arguments.get("end_line")
    if not isinstance(start_line, int) or isinstance(start_line, bool):
        raise ValueError("start_line and end_line must be integers")
    if end_line is not None and (not isinstance(end_line, int) or isinstance(end_line, bool)):
        raise ValueError("start_line and end_line must be integers")
    start_line = max(1, start_line)

    if has_range:
        line_count, start_offset, end_offset = _line_range_offsets(path, start_line, end_line)
        if start_line < 1 or start_line > line_count or (end_line is not None and end_line < start_line):
            raise ValueError(
                f"line range is outside file: start_line={start_line}, end_line={end_line or line_count}, line_count={line_count}"
            )
        requested_end = end_line or line_count
        range_clipped = requested_end > line_count
        requested_end = min(requested_end, line_count)
        raw = _read_bounded(path, start_offset, min(byte_limit, max(0, end_offset - start_offset)))
        text, consumed_bytes = _decode_bounded(raw)
        next_offset = start_offset + consumed_bytes
        truncated = next_offset < end_offset
        next_line = start_line
        if truncated:
            consumed = text.count("\n")
            next_line = start_line + consumed + (1 if text and not text.endswith("\n") else 0)
        else:
            next_line = requested_end + 1
            text = "\n".join(text.splitlines())
        data = _metadata(
            relative, path, size_bytes, text, start_line=start_line,
            end_line=min(requested_end, next_line - 1), byte_offset=start_offset,
            next_offset=next_offset if truncated else None,
            next_start_line=next_line if truncated else None,
            truncated=truncated,
        )
        data["range_clipped"] = range_clipped
        return text, data

    offset = arguments.get("offset", 0)
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    if offset > size_bytes:
        raise ValueError(f"offset is outside file: offset={offset}, size_bytes={size_bytes}")
    if offset < size_bytes:
        with path.open("rb") as handle:
            handle.seek(offset)
            first = handle.read(1)
        if first and 0x80 <= first[0] <= 0xBF:
            raise ValueError("offset must be at a UTF-8 character boundary")
    raw = _read_bounded(path, offset, byte_limit)
    text, consumed_bytes = _decode_bounded(raw)
    next_offset = offset + consumed_bytes
    truncated = next_offset < size_bytes
    if not has_offset and not truncated:
        text = "\n".join(text.splitlines())
    data = _metadata(
        relative, path, size_bytes, text, start_line=1, end_line=text.count("\n") + 1,
        byte_offset=offset, next_offset=next_offset if truncated else None,
        truncated=truncated,
    )
    return text, data


def _validate_text_file(path: Path) -> None:
    decoder = codecs.getincrementaldecoder("utf-8")()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(READ_CHUNK_BYTES)
            if not chunk:
                break
            if b"\x00" in chunk:
                raise ValueError("Binary files are not model-readable")
            try:
                decoder.decode(chunk)
            except UnicodeDecodeError as exc:
                raise ValueError("File is not valid UTF-8 text") from exc
    try:
        decoder.decode(b"", final=True)
    except UnicodeDecodeError as exc:
        raise ValueError("File is not valid UTF-8 text") from exc


def _line_range_offsets(path: Path, start_line: int, end_line: int | None) -> tuple[int, int, int]:
    size = path.stat().st_size
    if size == 0:
        return 0, 0, 0
    newline_count = 0
    start_offset = 0
    end_offset = size
    position = 0
    last_byte = b""
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(READ_CHUNK_BYTES), b""):
            last_byte = chunk[-1:]
            for index, byte in enumerate(chunk):
                if byte != 0x0A:
                    continue
                newline_count += 1
                absolute = position + index
                if newline_count + 1 == start_line:
                    start_offset = absolute + 1
                if end_line is not None and newline_count == end_line:
                    end_offset = absolute
            position += len(chunk)
    line_count = newline_count if last_byte == b"\n" else newline_count + 1
    return line_count, start_offset, end_offset


def _read_bounded(path: Path, offset: int, limit: int) -> bytes:
    with path.open("rb") as handle:
        handle.seek(offset)
        return handle.read(limit)


def _decode_bounded(raw: bytes) -> tuple[str, int]:
    candidate = raw
    while candidate:
        try:
            return candidate.decode("utf-8"), len(candidate)
        except UnicodeDecodeError as exc:
            if exc.reason == "unexpected end of data" and exc.end == len(candidate):
                candidate = candidate[:exc.start]
                continue
            raise ValueError("File is not valid UTF-8 text") from exc
    return "", 0


def _metadata(
    relative: str,
    path: Path,
    size_bytes: int,
    text: str,
    *,
    start_line: int,
    end_line: int,
    byte_offset: int,
    next_offset: int | None,
    next_start_line: int | None = None,
    truncated: bool,
) -> dict:
    return {
        "path": relative,
        "sha256": _sha256(path),
        "size_bytes": size_bytes,
        "content_bytes": len(text.encode("utf-8")),
        "start_line": start_line,
        "end_line": end_line,
        "truncated": truncated,
        "encoding": "utf-8",
        "file_kind": "text",
        "byte_offset": byte_offset,
        "next_offset": next_offset,
        "next_start_line": next_start_line,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(128 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
