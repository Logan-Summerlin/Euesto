from pathlib import Path
from .apply_patch import apply_patch

def write(root: Path, arguments: dict, *, max_bytes: int, max_checkpoint_files: int = 300000, max_checkpoint_bytes: int = 2000000000) -> tuple[str, dict]:
    edits = arguments.get("edits")
    if isinstance(edits, dict): edits = [edits]
    if not isinstance(edits, list) or not edits: raise ValueError("write requires edits")
    if any(not isinstance(item, dict) or item.get("mode", "replace_file") != "replace_file" for item in edits): raise ValueError("write only supports whole-file writes")
    normalized = {**arguments, "edits": [{**item, "mode": "replace_file"} for item in edits]}
    return apply_patch(root, normalized, max_bytes=max_bytes, max_checkpoint_files=max_checkpoint_files, max_checkpoint_bytes=max_checkpoint_bytes)
