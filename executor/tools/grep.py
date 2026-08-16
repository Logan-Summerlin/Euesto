from __future__ import annotations

from pathlib import Path

from .search_text import search_text


def grep(root: Path, arguments: dict, *, max_bytes: int, max_results: int = 500) -> tuple[str, dict]:
    output, data = search_text(root, arguments, max_bytes=max_bytes, max_results=max_results)
    encoded = output.encode("utf-8")
    if len(encoded) <= max_bytes:
        return output, data
    clipped = encoded[:max_bytes]
    output = clipped.decode("utf-8", errors="ignore")
    data = dict(data)
    data["truncated"] = True
    data["output_truncated"] = True
    return output, data
