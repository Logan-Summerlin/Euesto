from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_DATA_HOME"] = tempfile.mkdtemp(prefix="local-chat-screenshot-")
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtQml import QQmlApplicationEngine  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from src.qml_backend import DesktopBridge  # noqa: E402


def main() -> int:
    app = QApplication(sys.argv)
    backend = DesktopBridge()
    conversation = backend.current_conversation()
    if conversation:
        backend.storage.update_conversation(conversation.id, title="Planning a local chatbot")
        backend.storage.add_message(
            conversation.id,
            "user",
            "Build me a fast, minimalist desktop chatbot for OpenRouter.",
        )
        backend.storage.add_message(
            conversation.id,
            "assistant",
            "Absolutely. The app can stay **native and lightweight** while streaming "
            "responses, keeping chats local, and storing the API key in Windows "
            "Credential Manager.\n\n```python\npython app.py\n```",
            input_tokens=24,
            output_tokens=38,
            cost=0.00042,
        )
        backend.selectConversation(conversation.id)
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("backend", backend)
    engine.addImportPath(str(ROOT / "qml"))
    engine.load(str(ROOT / "qml" / "Main.qml"))
    if not engine.rootObjects():
        backend.shutdown()
        return 1
    window = engine.rootObjects()[0]
    window.setWidth(1120)
    window.setHeight(760)
    backend.attachWindow(window)

    def capture() -> None:
        window.grabWindow().save(str(ROOT / "assets" / "screenshot.png"))
        backend.shutdown()

    QTimer.singleShot(800, capture)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
