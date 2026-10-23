from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# How long a resource that a test stopped using may take to finish. A QThread that has just
# returned from run() (or a process that has just been terminated) is not a leak yet.
SETTLE_SECONDS = 2.0


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config) -> None:
    # Windows keeps the default temporary directory under %LOCALAPPDATA%, and the workspace
    # broker refuses any workspace below an AppData directory, so tmp_path workspaces would
    # all be rejected. Keep test directories in the (git-ignored) repository instead.
    if sys.platform != "win32" or config.option.basetemp is not None:
        return
    if any(part.casefold() == "appdata" for part in Path(tempfile.gettempdir()).parts):
        config.option.basetemp = str(ROOT / ".pytest-tmp")


# A node id is repeated in every `-v` progress line, in each failure header, and by
# `validate.py collect`, so it must stay short: a parametrization over a captured transcript or
# a request body writes the whole payload into the tier's log instead of naming the test
# (docs/TESTING.md, "Test ids").
MAX_NODE_ID_CHARS = 200


def unbounded_test_ids(items) -> list[str]:
    """Collected node ids that would flood a tier's log with their own parameters."""
    return [item.nodeid for item in items if len(item.nodeid) > MAX_NODE_ID_CHARS]


def pytest_collection_modifyitems(config, items) -> None:
    unbounded = unbounded_test_ids(items)
    if unbounded:
        offending = "; ".join(
            f"{nodeid[:64]}… ({len(nodeid)} characters)" for nodeid in unbounded[:5]
        )
        raise pytest.UsageError(
            f"{len(unbounded)} collected test id(s) exceed {MAX_NODE_ID_CHARS} characters; "
            "give the parametrization explicit ids= (docs/TESTING.md, 'Test ids'): "
            + offending
        )
    if sys.platform != "win32":
        return
    skip = pytest.mark.skip(
        reason="needs the executor's Linux container (/bin/bash, POSIX modes or ctime)"
    )
    for item in items:
        if "posix" in item.keywords:
            item.add_marker(skip)


def _reap(process: subprocess.Popen) -> None:
    """Terminate, then kill, a leaked child so the interpreter can still exit."""
    try:
        process.terminate()
        process.wait(timeout=SETTLE_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
            process.wait(timeout=SETTLE_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            pass


@pytest.fixture(autouse=True)
def no_leaked_child_processes(monkeypatch: pytest.MonkeyPatch):
    """A test must not leave a child process running (docs/TESTING.md, "Leak policy").

    A leaked child outlives pytest, keeps the step's output open, and turns "the tests
    finished" into "the step never ends". Tracking what this test actually started is precise
    and cheap; the guard reaps the leak so the run can finish, and then fails the test that
    caused it.
    """
    started: list[subprocess.Popen] = []
    original_init = subprocess.Popen.__init__

    def tracking_init(self, *args, **kwargs) -> None:
        original_init(self, *args, **kwargs)
        started.append(self)

    monkeypatch.setattr(subprocess.Popen, "__init__", tracking_init)
    yield
    leaked = [process for process in started if process.poll() is None]
    for process in leaked:
        _reap(process)
    assert not leaked, (
        "the test left a child process running; end it in the test or its fixture: "
        + ", ".join(str(process.args) for process in leaked)
    )


def _qt_thread_classes() -> list[type]:
    """Every QThread subclass the application defines, so leaks can be tracked per start()."""
    core = sys.modules.get("PySide6.QtCore")
    if core is None or not hasattr(core, "QThread"):
        return []
    classes: list[type] = []
    pending = [core.QThread]
    while pending:
        current = pending.pop()
        for subclass in current.__subclasses__():
            classes.append(subclass)
            pending.append(subclass)
    return classes


@pytest.fixture(autouse=True)
def no_leaked_qt_threads(monkeypatch: pytest.MonkeyPatch):
    """A test must not leave a QThread running.

    A live QThread is joined during interpreter finalisation, after every test has reported
    "passed", so it presents as a hang rather than a failure. Patching ``start`` on the
    application's own (Python) subclasses records exactly the threads a test launched.
    """
    classes = _qt_thread_classes()
    if not classes:
        yield
        return
    started: list[object] = []
    for thread_class in classes:
        original_start = thread_class.start

        def tracking_start(thread, *args, _original=original_start, **kwargs):
            started.append(thread)
            return _original(thread, *args, **kwargs)

        monkeypatch.setattr(thread_class, "start", tracking_start)
    yield
    leaked: list[str] = []
    for thread in started:
        deadline = time.monotonic() + SETTLE_SECONDS
        while not thread.isFinished() and time.monotonic() < deadline:
            thread.wait(50)
        if not thread.isFinished():
            leaked.append(thread.objectName() or type(thread).__name__)
    assert not leaked, (
        "the test left a QThread running; wait for it (or stop the service that owns it): "
        + ", ".join(leaked)
    )
