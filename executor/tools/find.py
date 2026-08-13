from pathlib import Path
from .list_files import list_files

def find(root: Path, arguments: dict) -> tuple[str, dict]:
    return list_files(root, arguments)
