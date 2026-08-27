pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: root
    objectName: "transcriptRoot"
    required property color backgroundColor
    required property color cardColor
    required property color userColor
    required property color textColor
    required property color mutedColor
    required property color borderColor
    required property color accentColor

    color: backgroundColor
    property bool followingTail: true
    property bool userScrolling: false
    property bool movingToTail: false
    property var activityOverrides: ({})
    property string conversationId: backend ? backend.currentConversationId : ""

    function maximumY() { return Math.max(0, viewport.contentHeight - viewport.height) }
    function updateTailState() { followingTail = maximumY() - viewport.contentY <= 24 }
    function followTail() { if (!followingTail || userScrolling) return; movingToTail = true; viewport.contentY = maximumY(); Qt.callLater(function() { movingToTail = false }) }
    function activityExpanded(key, defaultValue) { let value = activityOverrides[String(key || "")]; return value === undefined ? defaultValue : value }
    function setActivityExpanded(key, expanded) { let next = {}; for (let name in activityOverrides) next[name] = activityOverrides[name]; next[String(key || "")] = expanded; activityOverrides = next }
    function openExternalLink(destination) { let value = String(destination || ""); if (!/^(https?:|mailto:)/i.test(value)) return; linkDialog.destination = value; linkDialog.open() }

    onConversationIdChanged: { activityOverrides = ({}); followingTail = true; userScrolling = false; tailTimer.restart() }
    Timer { id: tailTimer; interval: 0; repeat: false; onTriggered: root.followTail() }

    Flickable {
        id: viewport
        objectName: "transcriptViewport"
        anchors.fill: parent
        anchors.margins: 18
        clip: true
        contentWidth: width
        contentHeight: Math.ceil(transcriptColumn.implicitHeight)
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        pixelAligned: true
        ScrollBar.vertical: ScrollBar {
            id: transcriptScrollBar
            objectName: "transcriptScrollBar"
            policy: ScrollBar.AlwaysOn
            minimumSize: 0.08
            active: true
            contentItem: Rectangle { implicitWidth: 10; radius: width / 2; color: root.mutedColor; opacity: transcriptScrollBar.pressed || transcriptScrollBar.hovered ? 0.95 : 0.65 }
            onPressedChanged: { if (pressed) { root.userScrolling = true; root.followingTail = false } else { root.userScrolling = false; root.updateTailState() } }
        }
        onMovementStarted: { if (!root.movingToTail) { root.userScrolling = true; root.followingTail = false } }
        onMovementEnded: { if (!root.movingToTail) { root.userScrolling = false; root.updateTailState() } }
        onContentHeightChanged: { if (root.followingTail && !root.userScrolling) tailTimer.restart() }

        Column {
            id: transcriptColumn
            width: Math.max(0, viewport.width - transcriptScrollBar.width - 8)
            spacing: 12
            Repeater {
                id: transcriptRepeater
                objectName: "transcriptRepeater"
                model: transcriptModel
                delegate: Rectangle {
                    objectName: "transcriptDelegate"
                    id: card
                    required property var rowData
                    readonly property var value: rowData || ({})
                    width: transcriptColumn.width
                    height: Math.ceil(content.implicitHeight + 24)
                    radius: 12
                    color: card.value.role === "user" ? root.userColor : root.cardColor
                    border.color: root.borderColor
                    border.width: 1
                    Column {
                        id: content
                        anchors.fill: parent
                        anchors.margins: 12
                        spacing: 8
                        RowLayout {
                            width: parent.width
                            height: implicitHeight
                            Label { text: card.value.role === "user" ? "You" : card.value.role === "activity" ? "Legacy activity" : "Assistant"; color: root.mutedColor; renderType: Text.NativeRendering; font.weight: Font.DemiBold }
                            Item { Layout.fillWidth: true }
                            Label { visible: card.value.streaming === true; text: "Working"; color: root.accentColor; renderType: Text.NativeRendering }
                            ToolButton { visible: card.value.role === "assistant" && card.value.streaming !== true; text: "↻"; ToolTip.visible: hovered; ToolTip.text: "Regenerate"; onClicked: if (backend) backend.regenerateMessage(card.value.messageId) }
                        }
                        Button { id: activityButton; property bool expanded: root.activityExpanded(card.value.key, card.value.activityExpanded === true); width: parent.width; height: visible ? implicitHeight : 0; visible: (card.value.activity || []).length > 0; flat: true; text: (expanded ? "▾ " : "▸ ") + (card.value.activitySummary || "Tool calls"); palette.buttonText: root.mutedColor; onClicked: root.setActivityExpanded(card.value.key, !expanded) }
                        Loader {
                            id: activityLoader
                            width: parent.width
                            height: active ? implicitHeight : 0
                            active: activityButton.visible && activityButton.expanded
                            visible: active
                            sourceComponent: Column {
                                width: activityLoader.width
                                spacing: 4
                                Repeater { model: card.value.activity || []; delegate: Rectangle { id: activityEvent; required property var modelData; readonly property var value: modelData || ({}); width: activityLoader.width; height: Math.ceil(eventTitle.implicitHeight + 14); radius: 7; color: activityEvent.value.attention === true ? Qt.rgba(0.75, 0.25, 0.2, 0.12) : "transparent"; border.color: root.borderColor; Label { id: eventTitle; anchors.fill: parent; anchors.margins: 7; text: activityEvent.value.title || "tool"; color: activityEvent.value.attention === true ? "#e57373" : root.mutedColor; renderType: Text.NativeRendering; font.weight: Font.DemiBold; elide: Text.ElideRight } } }
                            }
                        }
                        TextEdit { id: messageBody; objectName: "transcriptMessageBody"; width: parent.width; height: visible ? Math.ceil(contentHeight) : 0; visible: String(card.value.content || "").length > 0; text: card.value.html || ""; textFormat: TextEdit.RichText; renderType: Text.NativeRendering; readOnly: true; selectByMouse: true; wrapMode: TextEdit.Wrap; verticalAlignment: TextEdit.AlignTop; color: root.textColor; onLinkActivated: link => root.openExternalLink(String(link)) }
                        RowLayout {
                            width: parent.width
                            height: visible ? implicitHeight : 0
                            visible: String(card.value.metadata || "").length > 0
                            Label { Layout.fillWidth: true; text: card.value.metadata || ""; color: root.mutedColor; font.pixelSize: 11; renderType: Text.NativeRendering; elide: Text.ElideRight }
                            ToolButton { visible: card.value.role === "user" && backend !== null && !backend.generating; text: "Edit"; onClicked: if (backend) editDialog.openFor(card.value.messageId, card.value.content) }
                        }
                    }
                }
            }
        }
    }

    Label { anchors.centerIn: parent; visible: transcriptRepeater.count === 0; text: "Start a conversation"; color: root.mutedColor; renderType: Text.NativeRendering; font.pixelSize: 18 }
    Connections { target: backend; function onTranscriptChanged() { if (root.followingTail && !root.userScrolling) tailTimer.restart() } }
    Dialog { id: linkDialog; property string destination: ""; title: "Open external link?"; modal: true; anchors.centerIn: parent; width: Math.min(680, root.width - 60); standardButtons: Dialog.Open | Dialog.Cancel; onAccepted: Qt.openUrlExternally(destination); contentItem: Column { spacing: 10; padding: 16; Label { text: "Destination"; color: root.mutedColor; font.weight: Font.DemiBold }; Text { width: parent.width; text: linkDialog.destination; color: root.textColor; wrapMode: Text.Wrap; selectByMouse: true }; Label { text: "Only open this link if you recognize and trust the destination."; color: root.mutedColor; wrapMode: Text.Wrap } } }
    Dialog { id: editDialog; property int messageId: 0; title: "Edit message"; modal: true; anchors.centerIn: parent; width: Math.min(600, root.width - 60); standardButtons: Dialog.Ok | Dialog.Cancel; function openFor(id, value) { messageId = id; editor.text = value; open() }; onAccepted: if (backend) backend.editMessage(messageId, editor.text); contentItem: TextArea { id: editor; implicitHeight: 220; wrapMode: TextEdit.Wrap; selectByMouse: true } }
}
