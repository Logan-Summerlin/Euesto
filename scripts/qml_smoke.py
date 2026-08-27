#!/usr/bin/env python3
"""Deterministic, offscreen QML startup smoke check."""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

try:
    from PySide6.QtCore import QUrl
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtGui import QGuiApplication
except ImportError as exc:
    print(f"unavailable: PySide6 ({exc})", file=sys.stderr)
    raise SystemExit(127) from exc

app = QGuiApplication(sys.argv)
engine = QQmlApplicationEngine()
engine.load(QUrl.fromLocalFile(str(Path(__file__).resolve().parents[1] / "qml" / "Main.qml")))
if not engine.rootObjects():
    raise SystemExit("QML engine loaded no root object")
raise SystemExit(0)
