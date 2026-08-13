from pathlib import Path
from .run_command import CommandRunner

_runner = CommandRunner()

async def bash(request_id: str, root: Path, arguments: dict, *, max_seconds: int, max_output: int, max_checkpoint_files: int = 300000, max_checkpoint_bytes: int = 2000000000) -> tuple[str, dict]:
    return await _runner.run(request_id, root, arguments, max_seconds=max_seconds, max_output=max_output, max_checkpoint_files=max_checkpoint_files, max_checkpoint_bytes=max_checkpoint_bytes)

async def cancel(request_id: str) -> bool:
    return await _runner.cancel(request_id)

def events(request_id: str, after: int = 0) -> dict[str, object]:
    return _runner.events(request_id, after)
