"""Qt Quick rendering tests for ``qml/Transcript.qml`` driven through ``QQmlApplicationEngine``.

They need a Qt Quick runtime (offscreen platform, software scene graph), so they belong to
the ``slow`` tier and skip cleanly where PySide6 cannot load.
"""
from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

import pytest

try:
    from PySide6.QtCore import Property, QObject, QUrl, Signal, Slot
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuick import QQuickItem
    from PySide6.QtQuickControls2 import QQuickStyle
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
except ImportError as exc:
    pytest.skip(f"Qt Quick runtime unavailable: {exc}", allow_module_level=True)

from src.transcript_model import TranscriptListModel

pytestmark = pytest.mark.slow

ROOT = Path(__file__).resolve().parents[2]
WAIT_SECONDS = 5.0


class FakeBackend(QObject):
    transcriptChanged = Signal()
    stateChanged = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.model = TranscriptListModel(self)

    @Property(QObject, constant=True)
    def transcriptModel(self) -> QObject:
        return self.model

    @Property(str, notify=stateChanged)
    def currentConversationId(self) -> str:
        return "conversation"

    @Property(bool, notify=stateChanged)
    def generating(self) -> bool:
        return False

    @Slot(int)
    def regenerateMessage(self, _message_id: int) -> None:
        pass

    @Slot(int, str)
    def editMessage(self, _message_id: int, _value: str) -> None:
        pass


def _message(index: int, lines: int, **overrides: object) -> dict[str, object]:
    content = "\n".join(f"line {line}" for line in range(lines))
    html = "<p>" + "<br>".join(content.splitlines()) + "</p>"
    row: dict[str, object] = {
        "key": f"message-{index}",
        "messageId": index,
        "role": "user" if index % 2 else "assistant",
        "content": content,
        "html": html,
        "metadata": "model/test" if index % 2 == 0 else "",
        "activity": [],
        "activitySummary": "",
        "activityExpanded": False,
        "streaming": False,
    }
    row.update(overrides)
    return row


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    existing = QApplication.instance()
    if existing is not None:
        return existing
    QQuickStyle.setStyle("Basic")
    return QApplication([])


def _wait_until(condition: Callable[[], bool], message: str) -> None:
    """Process Qt events until an observable condition holds (bounded, never a fixed sleep)."""
    deadline = time.monotonic() + WAIT_SECONDS
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {message}")
        QTest.qWait(10)


def _create_transcript_window(backend: FakeBackend) -> tuple[QQmlApplicationEngine, QObject]:
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("backend", backend)
    engine.rootContext().setContextProperty("transcriptModel", backend.model)
    engine.addImportPath(str(ROOT / "qml"))
    engine.loadData(
        b"""
import QtQuick
import QtQuick.Controls
import "."
ApplicationWindow {
    width: 720
    height: 520
    visible: true
    Transcript {
        anchors.fill: parent
        backgroundColor: "#10141c"
        cardColor: "#181f2a"
        userColor: "#1d2c49"
        textColor: "#e7eaf0"
        mutedColor: "#9aa7ba"
        borderColor: "#2b3443"
        accentColor: "#6f93f5"
    }
}
""",
        QUrl.fromLocalFile(str(ROOT / "qml" / "TranscriptHarness.qml")),
    )
    roots = engine.rootObjects()
    assert roots, [warning.toString() for warning in engine.warnings()]
    window = roots[-1]
    assert window is not None
    return engine, window


def _destroy_transcript_window(qapp: QApplication, engine: QQmlApplicationEngine, window: QObject) -> None:
    window.close()
    engine.deleteLater()
    qapp.processEvents()
    QTest.qWait(0)
    qapp.processEvents()


def _visual_find(item: QQuickItem, name: str, out: list[QObject]) -> None:
    if item.objectName() == name:
        out.append(item)
    for child in item.childItems():
        _visual_find(child, name, out)


def _find_all(window: QObject, name: str) -> list[QObject]:
    found: list[QObject] = []
    _visual_find(window.contentItem(), name, found)
    return found


def _message_bodies(window: QObject, count: int) -> list[QObject]:
    _wait_until(lambda: len(_find_all(window, "transcriptMessageBody")) == count, f"{count} message bodies")
    return _find_all(window, "transcriptMessageBody")


def _viewport(window: QObject) -> tuple[QObject, QObject]:
    transcript = window.findChild(QObject, "transcriptRoot")
    viewport = window.findChild(QObject, "transcriptViewport")
    assert transcript is not None and viewport is not None
    return transcript, viewport


def _height(viewport: QObject) -> float:
    return float(viewport.property("contentHeight"))


def test_transcript_scroll_anchor_survives_a_tail_update(qapp: QApplication) -> None:
    backend = FakeBackend()
    rows = [_message(index, (1, 4, 16, 3)[index % 4]) for index in range(1, 41)]
    backend.model.replace(rows, reset=True)
    engine, window = _create_transcript_window(backend)
    try:
        transcript, viewport = _viewport(window)
        _message_bodies(window, len(rows))
        _wait_until(lambda: _height(viewport) > float(viewport.property("height")), "an overflowing transcript")
        initial_height = _height(viewport)
        transcript.setProperty("followingTail", False)
        viewport.setProperty("contentY", initial_height / 2)
        anchored_y = float(viewport.property("contentY"))

        changed = list(rows)
        changed[-1] = _message(40, 80)
        backend.model.replace(changed)
        backend.transcriptChanged.emit()
        _wait_until(lambda: _height(viewport) > initial_height, "the tail row to grow")
        QTest.qWait(20)
        assert float(viewport.property("contentY")) == pytest.approx(anchored_y, abs=1.0)

        stable_height = _height(viewport)
        viewport.setProperty("contentY", 0)
        viewport.setProperty("contentY", stable_height - float(viewport.property("height")))
        qapp.processEvents()
        assert _height(viewport) == pytest.approx(stable_height, abs=0.5)

        transcript.setProperty("followingTail", True)
        backend.model.replace(changed + [_message(41, 24)])
        backend.transcriptChanged.emit()

        def at_tail() -> bool:
            expected = max(0.0, _height(viewport) - float(viewport.property("height")))
            return _height(viewport) > stable_height and abs(float(viewport.property("contentY")) - expected) <= 1.0

        _wait_until(at_tail, "the view to follow the new tail")
    finally:
        _destroy_transcript_window(qapp, engine, window)


def test_transcript_renders_long_content_after_an_existing_row_update(qapp: QApplication) -> None:
    backend = FakeBackend()
    rows = [_message(1, 1), _message(2, 1)]
    backend.model.replace(rows, reset=True)
    engine, window = _create_transcript_window(backend)
    try:
        bodies = _message_bodies(window, 2)
        _wait_until(lambda: float(bodies[-1].property("contentHeight")) > 0, "the initial body to lay out")
        before = float(bodies[-1].property("contentHeight"))
        updated = list(rows)
        updated[-1] = _message(2, 180)
        backend.model.replace(updated)
        _wait_until(lambda: float(_message_bodies(window, 2)[-1].property("contentHeight")) > before, "the updated body to grow")
        after = _message_bodies(window, 2)[-1]
        content_height = float(after.property("contentHeight"))
        assert float(after.property("height")) == pytest.approx(content_height, abs=1.0)
        assert "line 0" in str(after.property("text"))
        assert "line 179" in str(after.property("text"))
    finally:
        _destroy_transcript_window(qapp, engine, window)


def test_transcript_keeps_all_rows_after_repeated_scroll_and_height_updates(qapp: QApplication) -> None:
    backend = FakeBackend()
    rows = [_message(index, (2, 5, 18, 3)[index % 4]) for index in range(1, 61)]
    backend.model.replace(rows, reset=True)
    engine, window = _create_transcript_window(backend)
    try:
        transcript, viewport = _viewport(window)
        _message_bodies(window, len(rows))
        viewport_height = float(viewport.property("height"))
        _wait_until(lambda: _height(viewport) > viewport_height, "an overflowing transcript")
        initial_height = _height(viewport)
        transcript.setProperty("followingTail", False)
        for fraction in (0.0, 0.23, 0.61, 1.0, 0.38, 0.0, 1.0):
            viewport.setProperty("contentY", max(0.0, _height(viewport) - viewport_height) * fraction)
            qapp.processEvents()
        changed = list(rows)
        changed[10] = _message(11, 45)
        changed[48] = _message(49, 32)
        backend.model.replace(changed)
        _wait_until(lambda: _height(viewport) > initial_height, "the updated rows to grow")
        for fraction in (1.0, 0.0, 0.5, 0.17, 0.83, 0.0, 1.0):
            viewport.setProperty("contentY", max(0.0, _height(viewport) - viewport_height) * fraction)
            qapp.processEvents()
        bodies = _message_bodies(window, len(changed))
        assert all(float(body.property("height")) > 0 for body in bodies)
        assert len(_find_all(window, "transcriptDelegate")) == len(changed)
    finally:
        _destroy_transcript_window(qapp, engine, window)


def test_transcript_empty_state_and_streaming_rows(qapp: QApplication) -> None:
    backend = FakeBackend()
    engine, window = _create_transcript_window(backend)
    try:
        repeater = window.findChild(QObject, "transcriptRepeater")
        assert repeater is not None and int(repeater.property("count")) == 0
        backend.model.replace([_message(1, 2), _message(2, 1, streaming=True, content="partial", html="<p>partial</p>")])
        _wait_until(lambda: int(repeater.property("count")) == 2, "two delegates")
        bodies = _message_bodies(window, 2)
        assert "partial" in str(bodies[-1].property("text"))
        backend.model.replace([_message(1, 2), _message(2, 3)])
        _wait_until(lambda: "line 2" in str(_message_bodies(window, 2)[-1].property("text")), "the completed row to replace the streaming row")
        backend.model.replace([], reset=True)
        _wait_until(lambda: int(repeater.property("count")) == 0, "the transcript to clear")
    finally:
        _destroy_transcript_window(qapp, engine, window)
