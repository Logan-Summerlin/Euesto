from __future__ import annotations

from pathlib import Path

from .search_text import search_text


def grep(root: Path, arguments: dict, *, max_scan_bytes: int, max_output_bytes: int, max_results: int = 500, max_seconds: float = 30.0) -> tuple[str, dict]:
    """Search file contents.

    ``max_scan_bytes`` only decides which candidate files are too large to scan;
    ``max_output_bytes`` independently bounds the combined output returned to the caller.
    """
    output, data = search_text(root, arguments, max_bytes=max_scan_bytes, max_results=max_results, max_seconds=max_seconds)
    encoded = output.encode("utf-8")
    if len(encoded) <= max_output_bytes:
        return output, data
    clipped = encoded[:max_output_bytes]
    output = clipped.decode("utf-8", errors="ignore")
    data = dict(data)
    data["truncated"] = True
    data["output_truncated"] = True
    data["max_output_bytes"] = max_output_bytes
    return output, data
