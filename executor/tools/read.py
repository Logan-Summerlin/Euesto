from __future__ import annotations

import re
from pathlib import Path

from ..errors import INVALID_ARGUMENTS, INVALID_UTF8, PATH_MISSING, ExecutorToolError
from ..paths import is_tool_excluded, normalize_relative, require_regular_file, safe_path
from ..staging import sha256_file
from ..utf8 import CHUNK_BYTES, validate_utf8_file

DEFAULT_READ_BYTES = 32_000
MAX_OUTLINE_ENTRIES = 40
MAX_OUTLINE_LINE_CHARS = 120
# Top-level definitions across common languages, matched only at column 0 so the outline of a
# truncated file stays short: Python, JS/TS, Go, Rust, Java/C#/Kotlin, C/C++, shell, Markdown.
_OUTLINE = re.compile(
    r"^(?:(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:def|class|function|interface|type|enum)\b"
    r"|func\b|(?:pub(?:\([^)]*\))?\s+)?(?:fn|struct|trait|impl|mod|enum)\b"
    r"|(?:public|private|protected|internal|abstract|final|static|sealed|data)\s"
    r"|(?:export\s+)?(?:const|let|var)\s+\w+\s*=\s*(?:async\s*)?(?:\(|function)"
    r"|#{1,3}\s|[A-Za-z_][\w:<>,*&\s]*\s[*&]?[A-Za-z_][\w:]*\s*\([^;]*$"
    r"|[A-Za-z_][\w-]*\s*\(\)\s*\{)"
)


def read(root: Path, arguments: dict, *, max_bytes: int) -> tuple[str, dict]:
    allowed = {"path", "start_line", "end_line", "offset", "max_bytes"}
    if set(arguments) - allowed:
        raise ExecutorToolError(INVALID_ARGUMENTS, "Unknown read arguments")
    relative = arguments.get("path")
    if not isinstance(relative, str) or not relative:
        raise ExecutorToolError(INVALID_ARGUMENTS, "read requires a file path")
    requested = arguments.get("max_bytes", DEFAULT_READ_BYTES)
    if not isinstance(requested, int) or isinstance(requested, bool) or requested < 1:
        raise ExecutorToolError(INVALID_ARGUMENTS, "max_bytes must be a positive integer")
    # The executor already clamped max_bytes to the profile's max_read_bytes and its hard ceiling.
    byte_limit = min(max_bytes, requested)
    # Paths hidden from find/grep/ls (VCS metadata, dependency/cache directories, executor
    # metadata) are equally invisible to read, whether Plan reads the source mount or Agent
    # reads staging.
    if is_tool_excluded(normalize_relative(relative)):
        raise ExecutorToolError(PATH_MISSING, f"file not found: {relative}")
    try:
        path = safe_path(root, relative, must_exist=True)
    except FileNotFoundError as exc:
        raise ExecutorToolError(PATH_MISSING, f"file not found: {relative}") from exc
    require_regular_file(path, "read")
    size_bytes = path.stat().st_size
    validate_utf8_file(
        path, "Binary files are not model-readable; read requires valid UTF-8 text"
    )

    has_range = "start_line" in arguments or "end_line" in arguments
    has_offset = "offset" in arguments
    if has_range and has_offset:
        raise ExecutorToolError(
            INVALID_ARGUMENTS, "read cannot combine line ranges with byte offsets"
        )

    start_line = arguments.get("start_line", 1)
    end_line = arguments.get("end_line")
    if not isinstance(start_line, int) or isinstance(start_line, bool):
        raise ExecutorToolError(INVALID_ARGUMENTS, "start_line and end_line must be integers")
    if end_line is not None and (not isinstance(end_line, int) or isinstance(end_line, bool)):
        raise ExecutorToolError(INVALID_ARGUMENTS, "start_line and end_line must be integers")
    start_line = max(1, start_line)

    if has_range:
        line_count, start_offset, end_offset = _line_range_offsets(path, start_line, end_line)
        if (
            start_line < 1
            or start_line > line_count
            or (end_line is not None and end_line < start_line)
        ):
            raise ExecutorToolError(
                INVALID_ARGUMENTS,
                f"line range is outside file: start_line={start_line}, end_line={end_line or line_count}, line_count={line_count}",
            )
        requested_end = end_line or line_count
        range_clipped = requested_end > line_count
        requested_end = min(requested_end, line_count)
        raw = _read_bounded(
            path, start_offset, min(byte_limit, max(0, end_offset - start_offset))
        )
        text, consumed_bytes = _decode_bounded(raw)
        next_offset = start_offset + consumed_bytes
        truncated = next_offset < end_offset
        next_line = start_line
        if truncated:
            # Continue at the first line not shown completely; a partial last line is re-read.
            # A single line longer than the limit has no line continuation, only next_offset.
            next_line = start_line + text.count("\n")
        else:
            next_line = requested_end + 1
            text = "\n".join(text.splitlines())
        data = _metadata(
            relative,
            path,
            size_bytes,
            text,
            start_line=start_line,
            end_line=max(start_line, min(requested_end, next_line - 1)),
            byte_offset=start_offset,
            next_offset=next_offset if truncated else None,
            next_start_line=next_line if truncated and next_line > start_line else None,
            truncated=truncated,
        )
        data["range_clipped"] = range_clipped
        data["line_count"] = line_count
        if truncated:
            data["outline"] = outline(path, start_line, data["end_line"])
        return text, data

    offset = arguments.get("offset", 0)
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ExecutorToolError(INVALID_ARGUMENTS, "offset must be a non-negative integer")
    if offset > size_bytes:
        raise ExecutorToolError(
            INVALID_ARGUMENTS,
            f"offset is outside file: offset={offset}, size_bytes={size_bytes}",
        )
    if offset < size_bytes:
        with path.open("rb") as handle:
            handle.seek(offset)
            first = handle.read(1)
        if first and 0x80 <= first[0] <= 0xBF:
            raise ExecutorToolError(
                INVALID_ARGUMENTS, "offset must be at a UTF-8 character boundary"
            )
    raw = _read_bounded(path, offset, byte_limit)
    text, consumed_bytes = _decode_bounded(raw)
    next_offset = offset + consumed_bytes
    truncated = next_offset < size_bytes
    if not has_offset and not truncated:
        text = "\n".join(text.splitlines())
    data = _metadata(
        relative,
        path,
        size_bytes,
        text,
        start_line=1,
        end_line=text.count("\n") + 1,
        byte_offset=offset,
        next_offset=next_offset if truncated else None,
        truncated=truncated,
    )
    data["line_count"] = _line_count(path)
    if truncated and not has_offset:
        newlines = text.count("\n")
        data["end_line"] = max(1, newlines)
        if newlines:
            data["next_start_line"] = newlines + 1
        data["outline"] = outline(path, 1, data["end_line"])
    return text, data


def _line_count(path: Path) -> int:
    newlines = 0
    last = b""
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_BYTES), b""):
            newlines += chunk.count(b"\n")
            last = chunk[-1:]
    if not last:
        return 0
    return newlines if last == b"\n" else newlines + 1


def outline(path: Path, shown_start: int, shown_end: int) -> list[dict[str, object]]:
    """Top-level definitions outside the lines already shown, so the next read can target a
    range. Bounded in entries and characters; only column-0 lines are considered."""
    entries: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        for number, line in enumerate(handle, start=1):
            if shown_start <= number <= shown_end or not line[:1].strip():
                continue
            text = line.rstrip("\r\n")
            if _OUTLINE.match(text):
                entries.append({"line": number, "text": text[:MAX_OUTLINE_LINE_CHARS]})
                if len(entries) >= MAX_OUTLINE_ENTRIES:
                    break
    return entries


def _line_range_offsets(
    path: Path, start_line: int, end_line: int | None
) -> tuple[int, int, int]:
    size = path.stat().st_size
    if size == 0:
        return 0, 0, 0
    newline_count = 0
    start_offset = 0
    end_offset = size
    position = 0
    last_byte = b""
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_BYTES), b""):
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
                candidate = candidate[: exc.start]
                continue
            raise ExecutorToolError(INVALID_UTF8, "File is not valid UTF-8 text") from exc
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
        "sha256": sha256_file(path),
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
