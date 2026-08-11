pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: root
    required property color backgroundColor
    required property color textColor
    required property color mutedColor
    required property color accentColor
    required property color selectionColor
    signal newRequested()
    signal selected(string conversationId)
    signal searchChanged(string query)
    signal archivedRequested()
    function focusSearch() { search.forceActiveFocus() }

    color: backgroundColor
    implicitWidth: 250

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 12
        spacing: 10

        Label {
            text: "OpenRouter Chat"
            color: root.textColor
            font.pixelSize: 20
            font.weight: Font.DemiBold
        }
        Button {
            Layout.fillWidth: true
            text: "＋  New chat"
            highlighted: true
            onClicked: root.newRequested()
        }
        TextField {
            id: search
            Layout.fillWidth: true
            placeholderText: "Search conversations…"
            onTextEdited: root.searchChanged(text)
        }
        ListView {
            id: list
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            spacing: 4
            model: backend.conversations
            ScrollBar.vertical: ScrollBar {}

            delegate: ItemDelegate {
                id: conversationDelegate
                required property var modelData
                width: list.width
                height: 48
                highlighted: modelData.id === backend.currentConversationId
                background: Rectangle {
                    radius: 8
                    color: conversationDelegate.highlighted ? root.selectionColor : "transparent"
                }
                contentItem: RowLayout {
                    spacing: 7
                    Label {
                        text: modelData.pinned ? "★" : ""
                        color: root.accentColor
                    }
                    Label {
                        Layout.fillWidth: true
                        text: modelData.title
                        color: root.textColor
                        elide: Text.ElideRight
                        font.weight: conversationDelegate.highlighted ? Font.DemiBold : Font.Normal
                    }
                }
                onClicked: root.selected(modelData.id)
                ToolTip.visible: hovered
                ToolTip.text: modelData.title
            }
        }
        Button {
            Layout.fillWidth: true
            flat: true
            text: backend.archivedView ? "Show active chats" : "Show archived"
            onClicked: root.archivedRequested()
        }
    }
}
