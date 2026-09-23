from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config) -> None:
    # Windows keeps the default temporary directory under %LOCALAPPDATA%, and the workspace
    # broker refuses any workspace below an AppData directory, so tmp_path workspaces would
    # all be rejected. Keep test directories in the (git-ignored) repository instead.
    if sys.platform != "win32" or config.option.basetemp is not None:
        return
    if any(part.casefold() == "appdata" for part in Path(tempfile.gettempdir()).parts):
        config.option.basetemp = str(ROOT / ".pytest-tmp")
