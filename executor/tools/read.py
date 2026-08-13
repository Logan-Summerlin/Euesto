from pathlib import Path
from .read_file import MAX_READ_BYTES, read_file

def read(root: Path, arguments: dict, *, max_bytes: int) -> tuple[str, dict]:
    return read_file(root, arguments, max_bytes=max_bytes)
