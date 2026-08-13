from pathlib import Path
from .search_text import search_text

def grep(root: Path, arguments: dict, *, max_bytes: int) -> tuple[str, dict]:
    return search_text(root, arguments, max_bytes=max_bytes)
