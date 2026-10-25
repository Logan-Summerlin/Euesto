#!/usr/bin/env python3
"""Deterministic, offscreen QML startup smoke check against a real backend.

The engine loads ``qml/Main.qml`` with the real ``DesktopBridge`` (a temporary database, the
null keyring backend, and no Docker work) instead of leaving ``backend`` and ``transcriptModel``
undefined. Every ``backend.<name>`` binding therefore resolves, so a missing property is a QML
``ReferenceError`` that fails the check rather than a warning nobody reads.

The check is bounded three ways: ``faulthandler`` dumps every thread's stack after 60 s and
exits, a 30 s quit timer ends any event loop that starts, and ``validate.py qml-smoke`` kills
the whole process tree after 300 s. A successful load leaves through ``os._exit`` because a
wedged Qt teardown must not hold the step open.
"""

from __future__ import annotations

import faulthandler
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")
# The bridge reads the stored gateway token while it is being constructed. Never let a startup
# check reach the Windows credential store (or prompt for it).
os.environ.setdefault("PYTHON_KEYRING_BACKEND", "keyring.backends.null.Keyring")

ROOT = Path(__file__).resolve().parents[1]
# Running ``python scripts/qml_smoke.py`` puts ``scripts/`` on ``sys.path``, not the repository
# root, so ``src`` would not import outside the validator (which sets ``PYTHONPATH``).
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
LOAD_TIMEOUT_SECONDS = 60
QUIT_TIMEOUT_MS = 30_000
MAX_MESSAGES = 200
# A binding that names a property the bridge does not expose is exactly the failure this check
# exists to catch; other Qt messages are reported but do not fail the load.
FAILING_MESSAGES = ("ReferenceError", "TypeError")


def _message_handler(_mode, _context, message) -> None:
    if len(messages) < MAX_MESSAGES:
        messages.append(str(message))
    print(f"qt: {message}", file=sys.stderr, flush=True)


messages: list[str] = []


def main() -> int:
    faulthandler.dump_traceback_later(LOAD_TIMEOUT_SECONDS, exit=True)
    try:
        from PySide6.QtCore import QTimer, QUrl, qInstallMessageHandler
        from PySide6.QtQml import QQmlApplicationEngine
        from PySide6.QtQuickControls2 import QQuickStyle
        from PySide6.QtWidgets import QApplication
    except ImportError as exc:
        print(f"unavailable: PySide6 ({exc})", file=sys.stderr)
        return 127

    from src.qml_backend import DesktopBridge
    from src.storage import Storage

    qInstallMessageHandler(_message_handler)
    # The style is set once, before the application exists, exactly as ``app.py`` does it.
    QQuickStyle.setStyle("Fusion")
    app = QApplication([])
    storage = Storage(Path(tempfile.mkdtemp(prefix="euesto-qml-smoke-")) / "smoke.sqlite3")
    backend = DesktopBridge(storage)
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("backend", backend)
    engine.rootContext().setContextProperty("transcriptModel", backend.transcriptModel)
    engine.addImportPath(str(ROOT / "qml"))
    QTimer.singleShot(QUIT_TIMEOUT_MS, app.quit)
    engine.load(QUrl.fromLocalFile(str(ROOT / "qml" / "Main.qml")))

    if not engine.rootObjects():
        print("QML engine loaded no root object", file=sys.stderr)
        return 1
    failures = [text for text in messages if any(mark in text for mark in FAILING_MESSAGES)]
    if failures:
        for text in failures:
            print(f"QML binding failure: {text}", file=sys.stderr)
        return 1
    print("qml smoke: Main.qml loaded with the real backend")
    return 0


if __name__ == "__main__":
    try:
        status = main()
    finally:
        faulthandler.cancel_dump_traceback_later()
    # Flush before ``os._exit``: it does not run the interpreter's shutdown, which is the point.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(status)
