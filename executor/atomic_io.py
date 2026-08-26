from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_bytes(target: Path, content: bytes, *, prefix: str = ".local-chat-atomic-") -> None:
    """Replace *target* atomically, keeping the temporary file on its filesystem."""
    descriptor, raw_temp = tempfile.mkstemp(prefix=prefix, dir=target.parent)
    temporary = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def atomic_write_text(target: Path, content: str, *, encoding: str = "utf-8") -> None:
    atomic_write_bytes(target, content.encode(encoding), prefix=f".{target.name}.atomic-")
