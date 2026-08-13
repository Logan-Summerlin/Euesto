from pathlib import Path
from .apply_patch import apply_patch

def edit(root: Path, arguments: dict, *, max_bytes: int, max_checkpoint_files: int = 300000, max_checkpoint_bytes: int = 2000000000) -> tuple[str, dict]:
    edits = arguments.get("edits")
    if isinstance(edits, dict): edits = [edits]
    if not isinstance(edits, list) or not edits: raise ValueError("edit requires edits")
    normalized = {**arguments, "edits": [{**item, "mode": item.get("mode", "replace_exact")} for item in edits]}
    if any(item.get("mode") != "replace_exact" for item in normalized["edits"]): raise ValueError("edit only supports exact replacements")
    return apply_patch(root, normalized, max_bytes=max_bytes, max_checkpoint_files=max_checkpoint_files, max_checkpoint_bytes=max_checkpoint_bytes)
