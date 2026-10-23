"""Shared isolation for the Qt tests in this folder.

A UI test that reaches the host blocks the Qt event loop instead of failing: Docker discovery,
the staged-session listing, and the Windows credential store are all native calls that can wait
without a deadline. This file defines that isolation once, before any test module imports
``src.settings``, rather than leaving each module to remember its own environment.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")
# keyring resolves this name through its own loader; the null backend never prompts or blocks.
os.environ.setdefault("PYTHON_KEYRING_BACKEND", "keyring.backends.null.Keyring")

try:
    from src import runtime_manager
    from src.desktop.runtime import RuntimeService
except ImportError:  # pragma: no cover - Qt unavailable: the Qt tests skip themselves
    runtime_manager = None
    RuntimeService = None


@pytest.fixture(autouse=True)
def isolated_host_services(monkeypatch):
    """Keep every UI test off Docker, off stored credentials, and off the automatic runtime."""
    if RuntimeService is None:
        yield
        return
    monkeypatch.setattr(runtime_manager, "locate_docker", lambda: None)
    monkeypatch.setattr(
        runtime_manager.RuntimeManager, "staged_sessions", lambda *args, **kwargs: []
    )
    start_automatically = RuntimeService.start

    def start_manually(service):
        assert service.automatic is False, (
            "UI tests must run the manual (developer) runtime; an automatic runtime starts the "
            "Docker lifecycle and its timers inside the Qt event loop"
        )
        start_automatically(service)

    monkeypatch.setattr(RuntimeService, "start", start_manually)
    yield
