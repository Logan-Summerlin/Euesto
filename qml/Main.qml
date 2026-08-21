pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs

ApplicationWindow {
    id: window
    visible: true
    width: 1180
    height: 780
    minimumWidth: 820
    minimumHeight: 580
    title: backend.currentTitle.length ? backend.currentTitle + " — Local OpenRouter Chat" : "Local OpenRouter Chat"

    readonly property bool dark: backend.theme !== "light"
    readonly property color pageColor: dark ? "#10141c" : "#f7f8fb"
    readonly property color panelColor: dark ? "#151b25" : "#ffffff"
    readonly property color sidebarColor: dark ? "#0c1118" : "#eef1f6"
    readonly property color cardColor: dark ? "#181f2a" : "#ffffff"
    readonly property color userColor: dark ? "#1d2c49" : "#edf3ff"
    readonly property color textColor: dark ? "#e7eaf0" : "#172033"
    readonly property color mutedColor: dark ? "#9aa7ba" : "#68748a"
    readonly property color borderColor: dark ? "#2b3443" : "#dce1ea"
    readonly property color accentColor: dark ? "#6f93f5" : "#305fd6"
    readonly property color selectionColor: dark ? "#26375f" : "#dce5ff"

    color: pageColor
    palette.window: pageColor
    palette.windowText: textColor
    palette.base: panelColor
    palette.alternateBase: cardColor
    palette.text: textColor
    palette.button: cardColor
    palette.buttonText: textColor
    palette.highlight: accentColor
    palette.highlightedText: "#ffffff"
    palette.placeholderText: mutedColor
    Component.onCompleted: {
        backend.attachWindow(window)
        if (backend.runtimeBusy)
            runtimeDialog.open()
    }
    onClosing: close => {
        close.accepted = false
        backend.shutdown()
    }

    Shortcut { sequence: "Ctrl+N"; onActivated: backend.newConversation() }
    Shortcut { sequence: "Ctrl+L"; onActivated: composer.focusInput() }
    Shortcut { sequence: "Ctrl+F"; onActivated: sidebar.focusSearch() }
    Shortcut { sequence: "Escape"; onActivated: backend.stopGeneration() }
    Shortcut { sequence: "Alt+Left"; onActivated: navigateActive(-1) }
    Shortcut { sequence: "Alt+Right"; onActivated: navigateActive(1) }
    Shortcut { sequence: "Ctrl+Comma"; onActivated: settingsDialog.openAndLoad() }

    function navigateActive(direction) {
        if (backend.transcript.length > 0) {
            let value = backend.transcript[backend.transcript.length - 1]
            if (value.messageId)
                backend.navigateBranch(value.messageId, direction)
        }
    }

    RowLayout {
        anchors.fill: parent
        spacing: 0

        Sidebar {
            id: sidebar
            Layout.fillHeight: true
            Layout.preferredWidth: 250
            backgroundColor: window.sidebarColor
            textColor: window.textColor
            mutedColor: window.mutedColor
            accentColor: window.accentColor
            selectionColor: window.selectionColor
            onNewRequested: backend.newConversation()
            onSelected: conversationId => backend.selectConversation(conversationId)
            onSearchChanged: query => backend.setConversationSearch(query)
            onArchivedRequested: backend.toggleArchived()
        }

        ColumnLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 0

            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 58
                color: window.panelColor
                border.color: window.borderColor

                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: 14
                    anchors.rightMargin: 14
                    spacing: 8

                    ComboBox {
                        id: modeBox
                        model: ["chat", "plan", "agent"]
                        currentIndex: Math.max(0, model.indexOf(backend.currentMode))
                        displayText: currentText.charAt(0).toUpperCase() + currentText.slice(1)
                        onActivated: backend.selectMode(currentText)
                        enabled: !backend.generating && !backend.stagingBusy
                            && (currentText === "chat" || backend.workspaceReady)
                        ToolTip.visible: hovered
                        ToolTip.text: backend.runtimeBusy
                            ? backend.runtimeDetail
                            : currentText === "chat"
                              ? "No local workspace access"
                              : currentText === "plan"
                                ? "Read-only isolated workspace inspection"
                              : "Approval-controlled staging and reviewed publication"
                    }

                    Switch {
                        text: "Auto"
                        visible: backend.currentMode === "agent"
                        checked: backend.autoModeEnabled
                        enabled: backend.autoModeAvailable && backend.workspaceReady
                            && !backend.generating && !backend.stagingBusy
                        onClicked: backend.requestAutoMode(checked)
                        ToolTip.visible: hovered
                        ToolTip.text: "Automatically authorize valid tools and publish successful staged changes"
                    }

                    Button {
                        text: backend.workspacePath.length
                            ? backend.workspacePath.split(/[\\/]/).pop()
                            : "Workspace"
                        onClicked: workspaceDialog.open()
                        enabled: !backend.generating && !backend.stagingBusy
                        ToolTip.visible: hovered
                        ToolTip.text: backend.workspacePath || "Select one project workspace"
                    }

                    Button {
                        Layout.maximumWidth: 250
                        text: {
                            for (let i = 0; i < backend.models.length; ++i)
                                if (backend.models[i].id === backend.currentModel)
                                    return backend.models[i].label
                            return backend.currentModel
                        }
                        onClicked: modelDialog.openAndRefresh()
                        enabled: !backend.generating
                    }

                    ComboBox {
                        model: ["default", "minimal", "low", "medium", "high"]
                        currentIndex: Math.max(0, model.indexOf(backend.reasoningEffort))
                        displayText: "Think: " + currentText.charAt(0).toUpperCase() + currentText.slice(1)
                        onActivated: backend.setReasoningEffort(currentText)
                        enabled: !backend.generating
                    }

                    Item { Layout.fillWidth: true }

                    Label {
                        text: backend.gatewayText
                        color: window.mutedColor
                        ToolTip.visible: statusMouse.containsMouse
                        ToolTip.text: backend.gatewayDetail
                        MouseArea {
                            id: statusMouse
                            anchors.fill: parent
                            hoverEnabled: true
                            onClicked: backend.showGatewayStatus()
                        }
                    }

                    Button {
                        text: "Settings"
                        onClicked: settingsDialog.openAndLoad()
                    }

                    ToolButton {
                        text: "⋯"
                        onClicked: actionsMenu.open()
                        Menu {
                            id: actionsMenu
                            MenuItem { text: "Rename"; onTriggered: renameDialog.open() }
                            MenuItem { text: "Pin / unpin"; onTriggered: backend.togglePin() }
                            MenuItem { text: "Archive / restore"; onTriggered: backend.toggleArchive() }
                            MenuItem { text: "Fork conversation"; onTriggered: backend.forkConversation() }
                            MenuSeparator {}
                            MenuItem { text: "Import…"; onTriggered: importDialog.open() }
                            MenuItem { text: "Export JSON…"; onTriggered: { exportFormat = "json"; exportDialog.open() } }
                            MenuItem { text: "Export Markdown…"; onTriggered: { exportFormat = "markdown"; exportDialog.open() } }
                            MenuSeparator {}
                            MenuItem { text: "Inspect context"; onTriggered: backend.inspectContext() }
                            MenuItem { text: "Compact context"; onTriggered: backend.compactContext() }
                            MenuItem { text: "Usage"; onTriggered: backend.showUsage() }
                            MenuItem { text: "Delete"; onTriggered: backend.requestDeleteConversation() }
                        }
                    }
                }
            }

            Transcript {
                Layout.fillWidth: true
                Layout.fillHeight: true
                backgroundColor: window.pageColor
                cardColor: window.cardColor
                userColor: window.userColor
                textColor: window.textColor
                mutedColor: window.mutedColor
                borderColor: window.borderColor
                accentColor: window.accentColor
            }

            Composer {
                id: composer
                Layout.fillWidth: true
                backgroundColor: window.panelColor
                textColor: window.textColor
                mutedColor: window.mutedColor
                borderColor: window.borderColor
                accentColor: window.accentColor
                onSendRequested: (text, steer) => backend.sendMessage(text, steer)
                onStopRequested: backend.stopGeneration()
            }
        }
    }

    property string exportFormat: "json"

    FolderDialog {
        id: workspaceDialog
        title: "Select one project workspace"
        onAccepted: backend.selectWorkspace(selectedFolder)
    }
    FileDialog {
        id: importDialog
        title: "Import conversation"
        nameFilters: ["Chat exports (*.json *.md)", "All files (*)"]
        fileMode: FileDialog.OpenFile
        onAccepted: backend.importConversation(selectedFile)
    }
    FileDialog {
        id: exportDialog
        title: "Export conversation"
        fileMode: FileDialog.SaveFile
        nameFilters: exportFormat === "markdown"
            ? ["Readable Markdown (*.md)"] : ["Complete JSON (*.json)"]
        onAccepted: backend.exportConversation(selectedFile, exportFormat)
    }

    Dialog {
        id: renameDialog
        title: "Rename conversation"
        modal: true
        anchors.centerIn: parent
        standardButtons: Dialog.Ok | Dialog.Cancel
        onOpened: renameField.text = backend.currentTitle
        onAccepted: backend.renameConversation(renameField.text)
        contentItem: TextField {
            id: renameField
            implicitWidth: 420
            placeholderText: "Conversation name"
        }
    }

    Dialog {
        id: modelDialog
        title: "Choose a model"
        modal: true
        anchors.centerIn: parent
        width: Math.min(900, window.width - 60)
        height: Math.min(650, window.height - 60)
        property var visibleModels: backend.models
        function refresh() {
            visibleModels = backend.filteredModels(
                modelSearch.text, textOnly.checked,
                Number(priceLimit.currentValue),
                Number(rankLimit.currentValue),
                Number(yearFilter.text || 0))
        }
        function openAndRefresh() { refresh(); open() }

        contentItem: ColumnLayout {
            spacing: 8
            RowLayout {
                TextField {
                    id: modelSearch
                    Layout.fillWidth: true
                    placeholderText: "Search models…"
                    onTextEdited: modelDialog.refresh()
                }
                CheckBox {
                    id: textOnly
                    text: "Text"
                    checked: true
                    onToggled: modelDialog.refresh()
                }
                ComboBox {
                    id: priceLimit
                    textRole: "text"
                    valueRole: "value"
                    model: [
                        { text: "Any price", value: -1 },
                        { text: "Free", value: 0 },
                        { text: "≤ $0.50/M", value: 0.5 },
                        { text: "≤ $1/M", value: 1 },
                        { text: "≤ $5/M", value: 5 }
                    ]
                    onActivated: modelDialog.refresh()
                }
                ComboBox {
                    id: rankLimit
                    textRole: "text"
                    valueRole: "value"
                    model: [
                        { text: "Any rank", value: 0 },
                        { text: "AA top 10", value: 10 },
                        { text: "AA top 25", value: 25 },
                        { text: "AA top 50", value: 50 },
                        { text: "AA top 100", value: 100 }
                    ]
                    onActivated: modelDialog.refresh()
                }
                TextField {
                    id: yearFilter
                    implicitWidth: 80
                    placeholderText: "Year"
                    inputMethodHints: Qt.ImhDigitsOnly
                    onTextEdited: modelDialog.refresh()
                }
                TextField {
                    id: customModel
                    Layout.preferredWidth: 170
                    placeholderText: "Custom model ID"
                }
                Button {
                    text: "Use ID"
                    enabled: customModel.text.trim().length > 0
                    onClicked: {
                        backend.selectModel(customModel.text)
                        modelDialog.close()
                    }
                }
                Button { text: "Refresh"; onClicked: backend.refreshCatalog() }
            }
            ListView {
                id: modelsList
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                spacing: 5
                model: modelDialog.visibleModels
                ScrollBar.vertical: ScrollBar {}
                delegate: ItemDelegate {
                    required property var modelData
                    width: modelsList.width
                    height: details.implicitHeight + 18
                    contentItem: ColumnLayout {
                        id: details
                        RowLayout {
                            Layout.fillWidth: true
                            Label {
                                text: (modelData.favorite ? "★ " : "") + modelData.label
                                font.weight: Font.DemiBold
                                color: window.textColor
                            }
                            Item { Layout.fillWidth: true }
                            Label {
                                text: modelData.price === null || modelData.price === undefined
                                    ? "" : "$" + Number(modelData.price).toFixed(3) + "/M avg"
                                color: window.mutedColor
                            }
                        }
                        Label {
                            Layout.fillWidth: true
                            text: modelData.id + " · " + Number(modelData.contextLength).toLocaleString() + " context"
                            color: window.mutedColor
                            elide: Text.ElideRight
                        }
                        Label {
                            Layout.fillWidth: true
                            visible: modelData.description.length > 0
                            text: modelData.description
                            color: window.mutedColor
                            maximumLineCount: 2
                            elide: Text.ElideRight
                            wrapMode: Text.Wrap
                        }
                    }
                    onClicked: {
                        backend.selectModel(modelData.id)
                        modelDialog.close()
                    }
                    onPressAndHold: backend.toggleFavoriteModel(modelData.id)
                }
            }
            Label {
                text: "Press and hold a model to favorite it."
                color: window.mutedColor
            }
        }
    }

    Dialog {
        id: settingsDialog
        title: "Settings"
        modal: true
        anchors.centerIn: parent
        width: Math.min(880, window.width - 50)
        height: Math.min(700, window.height - 50)
        standardButtons: Dialog.Close
        function optionalText(value) {
            return value === null || value === undefined ? "" : String(value)
        }
        function openAndLoad() {
            gatewayUrl.text = backend.gatewaySettings.url
            gatewayToken.clear()
            apiKey.clear()
            systemPrompt.text = backend.systemPrompt
            investigationModel.currentIndex = Math.max(0, investigationModel.model.indexOf(backend.investigationModel))
            let options = backend.modelOptions
            maxTokens.text = optionalText(options.max_tokens)
            temperature.text = optionalText(options.temperature)
            topP.text = optionalText(options.top_p)
            stopSequences.text = (options.stop || []).join("\n")
            privacyDeny.checked = options.data_collection !== "allow"
            zdr.checked = options.zero_data_retention === true
            let config = backend.workspaceConfiguration()
            workspaceInstructions.text = config.instructions || ""
            customTools.text = JSON.stringify(config.custom_tools || [], null, 2)
            backend.refreshSkills()
            let names = []
            for (let i = 0; i < backend.skills.length; ++i)
                if (backend.skills[i].active) names.push(backend.skills[i].name)
            activeSkills.text = names.join(", ")
            backend.loadPermissionRules()
            open()
        }

        contentItem: ColumnLayout {
            TabBar {
                id: tabs
                Layout.fillWidth: true
                TabButton { text: "Connection" }
                TabButton { text: "Conversation" }
                TabButton { text: "Agent" }
                TabButton { text: "Extensions" }
            }
            StackLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                currentIndex: tabs.currentIndex

                ScrollView {
                    contentWidth: availableWidth
                        ColumnLayout {
                            width: parent.width
                            spacing: 10
                            Label { text: "Local gateway"; font.pixelSize: 18; font.weight: Font.DemiBold }
                        Label {
                            Layout.fillWidth: true
                            text: "The app creates the local gateway and executor credentials automatically. Manual gateway settings are only needed for a developer-managed gateway."
                            color: window.mutedColor
                            wrapMode: Text.Wrap
                        }
                        TextField { id: gatewayUrl; Layout.fillWidth: true; placeholderText: "http://127.0.0.1:8765" }
                        TextField { id: gatewayToken; Layout.fillWidth: true; echoMode: TextInput.Password; placeholderText: backend.gatewaySettings.hasToken ? "Token already stored" : "Gateway token" }
                        Button { text: "Save gateway"; onClicked: backend.saveGateway(gatewayUrl.text, gatewayToken.text) }
                        Label { text: "OpenRouter API key"; font.pixelSize: 18; font.weight: Font.DemiBold }
                        TextField { id: apiKey; Layout.fillWidth: true; echoMode: TextInput.Password; placeholderText: backend.gatewaySettings.hasApiKey ? "Key already stored" : "sk-or-v1-…" }
                        Button { text: "Store API key"; onClicked: backend.saveApiKey(apiKey.text) }
                        Label { text: "Investigation model (required for repository investigation)"; font.pixelSize: 18; font.weight: Font.DemiBold }
                        ComboBox { id: investigationModel; Layout.fillWidth: true; model: backend.models.map(item => item.id); currentIndex: Math.max(0, model.indexOf(backend.investigationModel)) }
                        Button { text: "Save investigation model"; onClicked: backend.saveInvestigationModel(currentText) }
                        Label { text: "Provider privacy"; font.pixelSize: 18; font.weight: Font.DemiBold }
                        CheckBox {
                            id: privacyDeny
                            text: "Deny providers that collect or train on prompts"
                            checked: true
                        }
                        CheckBox {
                            id: zdr
                            text: "Require zero-data-retention endpoints"
                            ToolTip.visible: hovered
                            ToolTip.text: "Hosted web tools may have separate retention policies."
                        }
                        Button {
                            text: "Save privacy controls"
                            onClicked: backend.saveModelOptions({
                                max_tokens: maxTokens.text,
                                temperature: temperature.text,
                                top_p: topP.text,
                                reasoning_effort: backend.reasoningEffort,
                                stop: stopSequences.text.split("\n").filter(value => value.length > 0),
                                data_collection: privacyDeny.checked ? "deny" : "allow",
                                zero_data_retention: zdr.checked
                            })
                        }
                        Item { Layout.fillHeight: true }
                    }
                }

                ScrollView {
                    contentWidth: availableWidth
                    ColumnLayout {
                        width: parent.width
                        spacing: 10
                        Label { text: "System prompt"; font.pixelSize: 18; font.weight: Font.DemiBold }
                        TextArea {
                            id: systemPrompt
                            Layout.fillWidth: true
                            Layout.preferredHeight: 230
                            wrapMode: TextEdit.Wrap
                        }
                        Button { text: "Save system prompt"; onClicked: backend.saveSystemPrompt(systemPrompt.text) }
                        Label { text: "Prompt presets"; font.pixelSize: 18; font.weight: Font.DemiBold }
                        RowLayout {
                            ComboBox {
                                id: presetBox
                                Layout.fillWidth: true
                                model: backend.presets
                                textRole: "name"
                                valueRole: "id"
                                onActivated: {
                                    for (let i = 0; i < backend.presets.length; ++i) {
                                        if (backend.presets[i].id === currentValue) {
                                            presetName.text = backend.presets[i].name
                                            presetContent.text = backend.presets[i].content
                                            break
                                        }
                                    }
                                }
                            }
                            Button { text: "Apply"; enabled: presetBox.currentIndex >= 0; onClicked: backend.applyPromptPreset(presetBox.currentValue) }
                            Button { text: "Delete"; enabled: presetBox.currentIndex >= 0; onClicked: backend.deletePromptPreset(presetBox.currentValue) }
                        }
                        TextField { id: presetName; Layout.fillWidth: true; placeholderText: "Preset name" }
                        TextArea { id: presetContent; Layout.fillWidth: true; Layout.preferredHeight: 100; placeholderText: "Preset system prompt" }
                        Button { text: "Save as new preset"; onClicked: backend.savePromptPreset("", presetName.text, presetContent.text) }
                        Label { text: "Model controls"; font.pixelSize: 18; font.weight: Font.DemiBold }
                        TextField { id: maxTokens; Layout.fillWidth: true; placeholderText: "Maximum output tokens (provider default when empty)" }
                        TextField { id: temperature; Layout.fillWidth: true; placeholderText: "Temperature" }
                        TextField { id: topP; Layout.fillWidth: true; placeholderText: "Top-p" }
                        TextArea {
                            id: stopSequences
                            Layout.fillWidth: true
                            Layout.preferredHeight: 70
                            placeholderText: "Stop sequences, one per line"
                        }
                        Button {
                            text: "Save model controls"
                            onClicked: backend.saveModelOptions({
                                max_tokens: maxTokens.text,
                                temperature: temperature.text,
                                top_p: topP.text,
                                reasoning_effort: backend.reasoningEffort,
                                stop: stopSequences.text.split("\n").filter(value => value.length > 0),
                                data_collection: privacyDeny.checked ? "deny" : "allow",
                                zero_data_retention: zdr.checked
                            })
                        }
                        RowLayout {
                            Button { text: backend.theme === "dark" ? "Use light theme" : "Use dark theme"; onClicked: backend.setTheme(backend.theme === "dark" ? "light" : "dark") }
                            Button { text: "Inspect context"; onClicked: backend.inspectContext() }
                            Button { text: "Usage"; onClicked: backend.showUsage() }
                        }
                        Item { Layout.fillHeight: true }
                    }
                }

                ScrollView {
                    contentWidth: availableWidth
                    ColumnLayout {
                        width: parent.width
                        spacing: 10
                        Label { text: "Workspace instructions"; font.pixelSize: 18; font.weight: Font.DemiBold }
                        Label { text: backend.workspacePath || "No workspace selected"; color: window.mutedColor }
                        TextArea {
                            id: workspaceInstructions
                            Layout.fillWidth: true
                            Layout.preferredHeight: 180
                            wrapMode: TextEdit.Wrap
                            placeholderText: "User-authored instructions; cannot grant permissions"
                        }
                        Label { text: "Declared custom capabilities (discovery only)"; font.weight: Font.DemiBold }
                        TextArea {
                            id: customTools
                            Layout.fillWidth: true
                            Layout.preferredHeight: 160
                            font.family: "Cascadia Code"
                            placeholderText: "[]"
                        }
                        RowLayout {
                            Button {
                                text: backend.stagingBusy ? "Reviewing…" : "Review staged changes"
                                enabled: backend.workspaceReady && !backend.generating && !backend.stagingBusy
                                onClicked: backend.reviewStaging()
                            }
                            Button { text: "Save workspace configuration"; onClicked: backend.saveWorkspaceConfiguration(workspaceInstructions.text, customTools.text) }
                            Button {
                                text: backend.stagingBusy ? "Reseeding…" : "Discard staged changes"
                                enabled: backend.workspaceReady && !backend.generating && !backend.stagingBusy
                                onClicked: backend.requestDiscardStaging()
                            }
                        }
                        RowLayout {
                            TextField { id: resumeId; Layout.fillWidth: true; placeholderText: "Interrupted run ID" }
                            Button { text: "Resume"; onClicked: backend.resumeRun(resumeId.text) }
                            Button { text: "Pause active agent"; onClicked: backend.pauseAgent() }
                        }
                        Label { text: "Saved permissions"; font.pixelSize: 18; font.weight: Font.DemiBold }
                        Repeater {
                            model: backend.permissions
                            delegate: RowLayout {
                                required property var modelData
                                Layout.fillWidth: true
                                CheckBox {
                                    checked: modelData.enabled
                                    onToggled: backend.setPermissionEnabled(modelData.rule_id, checked)
                                }
                                Label {
                                    Layout.fillWidth: true
                                    text: modelData.tool + " · " + (modelData.path_prefix || modelData.executable || "exact scope")
                                    elide: Text.ElideRight
                                }
                                Button { text: "Delete"; onClicked: backend.deletePermission(modelData.rule_id) }
                            }
                        }
                        Item { Layout.fillHeight: true }
                    }
                }

                ScrollView {
                    contentWidth: availableWidth
                    ColumnLayout {
                        width: parent.width
                        spacing: 10
                        Label { text: "Skills"; font.pixelSize: 18; font.weight: Font.DemiBold }
                        TextField {
                            id: activeSkills
                            Layout.fillWidth: true
                            placeholderText: "Comma-separated active skill names"
                            Component.onCompleted: {
                                let names = []
                                for (let i = 0; i < backend.skills.length; ++i)
                                    if (backend.skills[i].active) names.push(backend.skills[i].name)
                                text = names.join(", ")
                            }
                        }
                        Button { text: "Save active skills"; onClicked: backend.saveActiveSkills(activeSkills.text) }
                        Repeater {
                            model: backend.skills
                            delegate: Label {
                                required property var modelData
                                Layout.fillWidth: true
                                text: (modelData.active ? "✓ " : "") + modelData.name + " — " + modelData.description
                                wrapMode: Text.Wrap
                            }
                        }
                        Label { text: "Prompt commands"; font.pixelSize: 18; font.weight: Font.DemiBold }
                        RowLayout {
                            TextField { id: commandName; placeholderText: "name" }
                            TextField { id: commandDescription; Layout.fillWidth: true; placeholderText: "description" }
                        }
                        TextArea { id: commandTemplate; Layout.fillWidth: true; Layout.preferredHeight: 100; placeholderText: "Template using {{args}}" }
                        Button { text: "Save command"; onClicked: backend.savePromptCommand(commandName.text, commandDescription.text, commandTemplate.text) }
                        Repeater {
                            model: backend.commands
                            delegate: RowLayout {
                                required property var modelData
                                Layout.fillWidth: true
                                Label { Layout.fillWidth: true; text: "/" + modelData.name + " — " + modelData.description }
                                Button {
                                    visible: !modelData.builtin
                                    text: "Delete"
                                    onClicked: backend.deletePromptCommand(modelData.name)
                                }
                            }
                        }
                        Item { Layout.fillHeight: true }
                    }
                }
            }
        }
    }

    Dialog {
        id: runtimeDialog
        title: backend.runtimeState === "failed" ? "Local runtime setup needs attention" : "Preparing local runtime"
        modal: false
        anchors.centerIn: parent
        width: Math.min(620, window.width - 50)
        closePolicy: backend.runtimeBusy ? Popup.NoAutoClose : Popup.CloseOnEscape

        contentItem: ColumnLayout {
            spacing: 10
            Label {
                Layout.fillWidth: true
                text: backend.workspacePath.length
                    ? "Setting up the isolated runtime for:\n" + backend.workspacePath
                    : "Starting the local gateway. You can select a workspace when it is ready."
                wrapMode: Text.Wrap
            }
            Label {
                Layout.fillWidth: true
                text: backend.gatewayText
                font.weight: Font.DemiBold
            }
            Label {
                Layout.fillWidth: true
                text: backend.runtimeDetail
                color: window.mutedColor
                wrapMode: Text.Wrap
            }
            BusyIndicator {
                Layout.alignment: Qt.AlignHCenter
                running: backend.runtimeBusy
                visible: running
            }
            RowLayout {
                Layout.alignment: Qt.AlignRight
                Button {
                    text: "Retry"
                    visible: backend.runtimeState === "failed"
                    onClicked: backend.retryRuntime()
                }
                Button {
                    text: "Close"
                    visible: !backend.runtimeBusy
                    onClicked: runtimeDialog.close()
                }
            }
        }
    }

    Dialog {
        id: approvalDialog
        property string approvalKey: ""
        property bool allowRule: false
        title: "Approval"
        modal: true
        anchors.centerIn: parent
        width: Math.min(760, window.width - 50)
        height: Math.min(620, window.height - 50)
        closePolicy: Popup.NoAutoClose

        contentItem: ColumnLayout {
            Label {
                id: approvalSummary
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                font.weight: Font.DemiBold
            }
            Label { text: "Full request details"; color: window.mutedColor }
            ScrollView {
                Layout.fillWidth: true
                Layout.fillHeight: true
                TextArea {
                    id: approvalDetails
                    readOnly: true
                    wrapMode: TextEdit.Wrap
                    font.family: "Cascadia Code"
                }
            }
            RowLayout {
                Layout.alignment: Qt.AlignRight
                Button {
                    text: "Deny"
                    onClicked: { backend.resolveApproval(approvalDialog.approvalKey, "deny"); approvalDialog.close() }
                }
                Button {
                    visible: approvalDialog.allowRule
                    text: "Allow for run"
                    onClicked: { backend.resolveApproval(approvalDialog.approvalKey, "allow_run"); approvalDialog.close() }
                }
                Button {
                    visible: approvalDialog.allowRule
                    text: "Save exact rule"
                    onClicked: { backend.resolveApproval(approvalDialog.approvalKey, "allow_rule"); approvalDialog.close() }
                }
                Button {
                    text: "Allow once"
                    highlighted: true
                    onClicked: { backend.resolveApproval(approvalDialog.approvalKey, "allow_once"); approvalDialog.close() }
                }
            }
        }
    }

    Dialog {
        id: noticeDialog
        modal: true
        anchors.centerIn: parent
        width: Math.min(650, window.width - 50)
        standardButtons: Dialog.Ok
        contentItem: ScrollView {
            implicitHeight: Math.min(400, noticeText.contentHeight + 30)
            TextArea {
                id: noticeText
                readOnly: true
                wrapMode: TextEdit.Wrap
            }
        }
    }

    Dialog {
        id: confirmationDialog
        property string token: ""
        modal: true
        anchors.centerIn: parent
        width: Math.min(600, window.width - 50)
        standardButtons: Dialog.Yes | Dialog.No
        onAccepted: backend.resolveConfirmation(token, true)
        onRejected: backend.resolveConfirmation(token, false)
        contentItem: Label {
            id: confirmationText
            wrapMode: Text.Wrap
        }
    }

    Connections {
        target: backend
        function onInfoRequested(title, message) {
            noticeDialog.title = title
            noticeText.text = message
            noticeDialog.open()
        }
        function onErrorRequested(title, message) {
            noticeDialog.title = title
            noticeText.text = message
            noticeDialog.open()
        }
        function onConfirmRequested(token, title, message) {
            confirmationDialog.token = token
            confirmationDialog.title = title
            confirmationText.text = message
            confirmationDialog.open()
        }
        function onApprovalRequested(value) {
            approvalDialog.approvalKey = value.key
            approvalDialog.allowRule = value.allowRule
            approvalDialog.title = value.title
            approvalSummary.text = value.summary
            approvalDetails.text = value.details
            approvalDialog.open()
        }
        function onFileExported(name) {
            noticeDialog.title = "Export complete"
            noticeText.text = "Exported " + name
            noticeDialog.open()
        }
        function onFileImported(name) {
            noticeDialog.title = "Import complete"
            noticeText.text = "Imported " + name
            noticeDialog.open()
        }
        function onRuntimeSetupStarted() {
            runtimeDialog.open()
        }
        function onRuntimeSetupFinished(success) {
            if (success)
                runtimeDialog.close()
        }
    }
}
