from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

# Read once at import, while the executor is still single-threaded: os.umask can only be
# read by setting it, and a transient 0 must never leak into a concurrently spawned command.
_UMASK = os.umask(0o022)
os.umask(_UMASK)


def replacement_mode(target: Path) -> int:
    """Permission bits a replacement of *target* must carry.

    Temporary files are created ``0600``; a replaced file keeps its own mode and a new file
    gets the ordinary default (``0666`` minus the umask), so a rewrite never reports or
    publishes a spurious permission change.
    """
    try:
        return stat.S_IMODE(target.stat().st_mode)
    except FileNotFoundError:
        return 0o666 & ~_UMASK


def atomic_write_bytes(target: Path, content: bytes, *, prefix: str = ".local-chat-atomic-") -> None:
    """Replace *target* atomically, keeping the temporary file on its filesystem."""
    mode = replacement_mode(target)
    descriptor, raw_temp = tempfile.mkstemp(prefix=prefix, dir=target.parent)
    temporary = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def atomic_write_text(target: Path, content: str, *, encoding: str = "utf-8") -> None:
    atomic_write_bytes(target, content.encode(encoding), prefix=f".{target.name}.atomic-")
