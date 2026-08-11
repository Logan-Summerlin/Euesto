from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

import pytest

try:
    from PySide6.QtCore import Property, QObject, QUrl, Signal, Slot
    from PySide6.QtQml import QQmlApplicationEngine, QQmlComponent
    from PySide6.QtQuickControls2 import QQuickStyle
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
except ImportError as exc:
    pytest.skip(f"Qt Quick runtime unavailable: {exc}", allow_module_level=True)

from src.transcript_model import TranscriptListModel

ROOT = Path(__file__).resolve().parents[1]


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


def _message(index: int, lines: int) -> dict[str, object]:
    content = "\n".join(f"line {line}" for line in range(lines))
    html = "<p>" + "<br>".join(content.splitlines()) + "</p>"
    return {
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


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    existing = QApplication.instance()
    if existing is not None:
        return existing
    QQuickStyle.setStyle("Basic")
    return QApplication([])


def test_transcript_scroll_anchor_survives_a_tail_update(qapp: QApplication) -> None:
    backend = FakeBackend()
    rows = [_message(index, (1, 4, 16, 3)[index % 4]) for index in range(1, 41)]
    backend.model.replace(rows, reset=True)

    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("backend", backend)
    engine.addImportPath(str(ROOT / "qml"))
    component = QQmlComponent(engine)
    component.setData(
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
    window = component.create()
    assert window is not None, [error.toString() for error in component.errors()]
    QTest.qWait(100)

    transcript = window.findChild(QObject, "transcriptRoot")
    viewport = window.findChild(QObject, "transcriptViewport")
    assert transcript is not None
    assert viewport is not None
    initial_height = float(viewport.property("contentHeight"))
    assert initial_height > float(viewport.property("height"))

    transcript.setProperty("followingTail", False)
    midpoint = initial_height / 2
    viewport.setProperty("contentY", midpoint)
    QTest.qWait(20)
    anchored_y = float(viewport.property("contentY"))

    changed = list(rows)
    changed[-1] = _message(40, 80)
    backend.model.replace(changed)
    backend.transcriptChanged.emit()
    QTest.qWait(100)

    assert float(viewport.property("contentHeight")) > initial_height
    assert float(viewport.property("contentY")) == pytest.approx(anchored_y, abs=1.0)

    stable_height = float(viewport.property("contentHeight"))
    viewport.setProperty("contentY", 0)
    QTest.qWait(20)
    viewport.setProperty(
        "contentY", stable_height - float(viewport.property("height"))
    )
    QTest.qWait(20)
    assert float(viewport.property("contentHeight")) == pytest.approx(
        stable_height, abs=0.5
    )

    transcript.setProperty("followingTail", True)
    rows_at_tail = changed + [_message(41, 24)]
    backend.model.replace(rows_at_tail)
    backend.transcriptChanged.emit()
    QTest.qWait(100)
    expected_tail = max(
        0.0,
        float(viewport.property("contentHeight")) - float(viewport.property("height")),
    )
    assert float(viewport.property("contentY")) == pytest.approx(expected_tail, abs=1.0)

    window.close()
    window.deleteLater()
    engine.deleteLater()


def test_transcript_renders_long_content_after_an_existing_row_update(
    qapp: QApplication,
) -> None:
    backend = FakeBackend()
    rows = [_message(1, 1), _message(2, 1)]
    backend.model.replace(rows, reset=True)

    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("backend", backend)
    engine.addImportPath(str(ROOT / "qml"))
    component = QQmlComponent(engine)
    component.setData(
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
    window = component.create()
    assert window is not None, [error.toString() for error in component.errors()]
    QTest.qWait(100)

    bodies = window.findChildren(QObject, "transcriptMessageBody")
    assert len(bodies) == 2
    before = float(bodies[-1].property("contentHeight"))

    updated = list(rows)
    updated[-1] = _message(2, 180)
    backend.model.replace(updated)
    QTest.qWait(150)

    bodies = window.findChildren(QObject, "transcriptMessageBody")
    after = bodies[-1]
    content_height = float(after.property("contentHeight"))
    assert content_height > before
    assert float(after.property("height")) == pytest.approx(content_height, abs=1.0)
    assert str(after.property("text")).startswith("<p>line 0")

    window.close()
    window.deleteLater()
    engine.deleteLater()


def test_transcript_keeps_all_rows_after_repeated_scroll_and_height_updates(
    qapp: QApplication,
) -> None:
    backend = FakeBackend()
    rows = [_message(index, (2, 5, 18, 3)[index % 4]) for index in range(1, 61)]
    backend.model.replace(rows, reset=True)

    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("backend", backend)
    engine.addImportPath(str(ROOT / "qml"))
    component = QQmlComponent(engine)
    component.setData(
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
    window = component.create()
    assert window is not None, [error.toString() for error in component.errors()]
    QTest.qWait(120)

    transcript = window.findChild(QObject, "transcriptRoot")
    viewport = window.findChild(QObject, "transcriptViewport")
    assert transcript is not None and viewport is not None
    initial_height = float(viewport.property("contentHeight"))
    viewport_height = float(viewport.property("height"))
    assert initial_height > viewport_height

    transcript.setProperty("followingTail", False)
    for fraction in (0.0, 0.23, 0.61, 1.0, 0.38, 0.0, 1.0):
        maximum = max(0.0, float(viewport.property("contentHeight")) - viewport_height)
        viewport.setProperty("contentY", maximum * fraction)
        QTest.qWait(15)

    changed = list(rows)
    changed[10] = _message(11, 45)
    changed[48] = _message(49, 32)
    backend.model.replace(changed)
    QTest.qWait(120)

    for fraction in (1.0, 0.0, 0.5, 0.17, 0.83, 0.0, 1.0):
        maximum = max(0.0, float(viewport.property("contentHeight")) - viewport_height)
        viewport.setProperty("contentY", maximum * fraction)
        QTest.qWait(15)

    bodies = window.findChildren(QObject, "transcriptMessageBody")
    assert len(bodies) == len(changed)
    assert all(float(body.property("height")) > 0 for body in bodies)
    assert float(viewport.property("contentHeight")) > initial_height

    window.close()
    window.deleteLater()
    engine.deleteLater()
