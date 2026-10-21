pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: root
    required property color backgroundColor
    required property color textColor
    required property color mutedColor
    required property color borderColor
    required property color accentColor
    signal sendRequested(string text, bool steer)
    signal stopRequested()

    color: backgroundColor
    implicitHeight: body.implicitHeight + 20
    border.color: borderColor
    border.width: 1

    function focusInput() { prompt.forceActiveFocus() }

    component ToolCheckBox: CheckBox {
        id: control
        Layout.alignment: Qt.AlignVCenter
        spacing: 8
        leftPadding: 0
        rightPadding: 0
        topPadding: 0
        bottomPadding: 0

        contentItem: Label {
            text: control.text
            color: root.textColor
            verticalAlignment: Text.AlignVCenter
            leftPadding: control.indicator.width + control.spacing
            elide: Text.ElideRight
        }

        indicator: Rectangle {
            width: 13
            height: 13
            implicitWidth: 13
            implicitHeight: 13
            radius: 2
            color: control.checked ? root.accentColor : "transparent"
            border.color: control.enabled ? root.mutedColor : root.borderColor
            border.width: 1

            Label {
                anchors.centerIn: parent
                visible: control.checked
                text: "✓"
                color: root.backgroundColor
                font.bold: true
                font.pixelSize: 9
            }
        }
    }

    ColumnLayout {
        id: body
        anchors.fill: parent
        anchors.margins: 10
        spacing: 6

        RowLayout {
            spacing: 14
            Label { text: "Tools"; color: root.mutedColor; font.pixelSize: 12 }
            ToolCheckBox {
                text: "Web Search"
                checked: backend.serverTools.web_search
                onToggled: backend.setServerTool("web_search", checked)
            }
            ToolCheckBox {
                text: "Web Fetch"
                checked: backend.serverTools.web_fetch
                onToggled: backend.setServerTool("web_fetch", checked)
            }
            ToolCheckBox {
                text: "DateTime"
                checked: backend.serverTools.datetime
                onToggled: backend.setServerTool("datetime", checked)
            }
            Item { Layout.fillWidth: true }
        }

        TextArea {
            id: prompt
            Layout.fillWidth: true
            Layout.preferredHeight: Math.min(110, Math.max(48, contentHeight + 18))
            placeholderText: backend.generating
                ? "Queue message · Ctrl+Enter steers"
                : "Message…  Enter to send · Shift+Enter for newline"
            color: root.textColor
            wrapMode: TextEdit.Wrap
            selectByMouse: true
            Keys.onPressed: event => {
                if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
                    if (event.modifiers & Qt.ShiftModifier) {
                        return
                    }
                    root.sendRequested(prompt.text, Boolean(event.modifiers & Qt.ControlModifier))
                    prompt.clear()
                    event.accepted = true
                }
            }
        }

        RowLayout {
            objectName: "runMeter"
            visible: backend.generating && backend.currentMode !== "chat"
            spacing: 8
            ProgressBar {
                Layout.preferredWidth: 90
                from: 0
                to: 1
                value: backend.runMeter.contextFraction
                visible: backend.runMeter.workingSetTokens > 0
                ToolTip.visible: hovered
                ToolTip.text: "Context against the working-set cap"
            }
            Label {
                Layout.fillWidth: true
                text: backend.runMeter.text
                color: root.mutedColor
                elide: Text.ElideRight
            }
        }

        RowLayout {
            Label {
                Layout.fillWidth: true
                text: backend.statusText
                color: root.mutedColor
                elide: Text.ElideRight
            }
            Button {
                visible: backend.generating
                text: "Stop"
                onClicked: root.stopRequested()
            }
            Button {
                text: "Steer"
                visible: backend.generating && backend.currentMode === "agent"
                enabled: prompt.text.trim().length > 0
                ToolTip.visible: hovered
                ToolTip.text: "Send guidance the agent reads before its next step (Ctrl+Enter)"
                onClicked: {
                    root.sendRequested(prompt.text, true)
                    prompt.clear()
                }
            }
            Button {
                text: backend.generating ? "Queue" : "Send"
                highlighted: true
                enabled: prompt.text.trim().length > 0
                onClicked: {
                    root.sendRequested(prompt.text, false)
                    prompt.clear()
                }
            }
        }
    }

    Connections {
        target: backend
        function onFocusComposerRequested() { prompt.forceActiveFocus() }
    }
}
