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
        // Staged work is never lost silently: publish, discard, or keep it first (plan 0.6).
        backend.requestClose()
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

                    Switch {
                        text: "Sandbox"
                        visible: backend.currentMode === "agent"
                        checked: backend.sandboxEnabled || backend.autoModeEnabled
                        enabled: backend.sandboxAvailable && backend.workspaceReady
                            && !backend.autoModeEnabled
                            && !backend.generating && !backend.stagingBusy
                        onClicked: backend.requestSandbox(checked)
                        ToolTip.visible: hovered
                        ToolTip.text: "Run edits and Bash in the network-less staged copy without prompts; publication still asks"
                    }

                    Switch {
                        text: "Registries"
                        objectName: "registrySwitch"
                        visible: backend.currentMode === "agent" && backend.registriesAllowed
                        checked: backend.networkMode === "registries"
                        enabled: backend.workspaceReady && !backend.generating && !backend.stagingBusy
                        onClicked: backend.requestRegistryAccess(checked)
                        ToolTip.visible: hovered
                        ToolTip.text: "Let Bash reach the package registries the workspace policy names; restarts the runtime"
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
                        id: reasoningCombo
                        model: backend.reasoningLevels
                        textRole: "text"
                        valueRole: "value"
                        currentIndex: Math.max(0, indexOfValue(backend.reasoningEffort))
                        displayText: "Think: " + currentText
                        onActivated: backend.setReasoningEffort(currentValue)
                        // A model without reasoning keeps its stored choice, greyed out.
                        enabled: !backend.generating && backend.reasoningSupported
                        opacity: backend.reasoningSupported ? 1 : 0.5
                        hoverEnabled: true
                        ToolTip.visible: hovered
                        ToolTip.text: {
                            const level = backend.reasoningLevels[currentIndex]
                            if (!backend.reasoningSupported)
                                return "This model does not take a reasoning setting."
                            return level && level.wire
                                ? "Sends reasoning.effort: " + level.wire
                                : "Sends no reasoning setting; the provider's default applies."
                        }
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

            Rectangle {
                objectName: "agentPlan"
                Layout.fillWidth: true
                visible: backend.agentPlan.steps.length > 0 || backend.budgetContinuationAvailable
                implicitHeight: planColumn.implicitHeight + 16
                color: window.panelColor
                border.color: window.borderColor

                ColumnLayout {
                    id: planColumn
                    anchors.fill: parent
                    anchors.margins: 8
                    spacing: 2
                    Label {
                        visible: backend.agentPlan.steps.length > 0
                        text: "Plan" + (backend.agentPlan.note ? " — " + backend.agentPlan.note : "")
                        font.weight: Font.DemiBold
                        color: window.textColor
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                    }
                    Repeater {
                        model: backend.agentPlan.steps
                        delegate: Label {
                            required property var modelData
                            Layout.fillWidth: true
                            text: ({ done: "☑", in_progress: "▶", blocked: "⚠" }[modelData.status] || "☐")
                                + " " + (modelData.title || modelData.id)
                            color: modelData.status === "done" ? window.mutedColor : window.textColor
                            font.strikeout: modelData.status === "done"
                            elide: Text.ElideRight
                        }
                    }
                    RowLayout {
                        visible: backend.budgetContinuationAvailable
                        Label {
                            Layout.fillWidth: true
                            text: "The run paused at its budget. Its plan and staged work are kept."
                            color: window.mutedColor
                            wrapMode: Text.Wrap
                        }
                        Button {
                            text: "Continue with another budget"
                            highlighted: true
                            enabled: !backend.generating && backend.workspaceReady
                            onClicked: backend.continueWithBudget()
                        }
                    }
                }
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
                                    ? "" : "$" + Number(modelData.price).toFixed(3) + "/M est."
                                color: window.mutedColor
                                HoverHandler { id: priceHover }
                                ToolTip.visible: priceHover.hovered
                                ToolTip.text: "Estimated at 95% input, 5% output, 95% cache hits"
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
                            text: modelData.priceText || ""
                            visible: text.length > 0
                            color: window.mutedColor
                            elide: Text.ElideRight
                        }
                        Label {
                            Layout.fillWidth: true
                            visible: (modelData.warnings || []).length > 0
                            text: "⚠ " + (modelData.warnings || []).join(" · ")
                            color: window.dark ? "#f0b45b" : "#a35b00"
                            wrapMode: Text.Wrap
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
                Layout.fillWidth: true
                text: backend.currentMode === "chat"
                    ? "Press and hold a model to favorite it."
                    : "Plan and Agent list only models that call tools. Press and hold a model to favorite it."
                color: window.mutedColor
                wrapMode: Text.Wrap
            }
        }
    }

    Dialog {
        id: settingsDialog
        objectName: "settingsDialog"
        title: "Settings"
        modal: true
        anchors.centerIn: parent
        width: Math.min(880, window.width - 50)
        height: Math.min(700, window.height - 50)
        standardButtons: Dialog.Close
        function optionalText(value) {
            return value === null || value === undefined ? "" : String(value)
        }
        function activeSkillNames() {
            return backend.skills.filter(skill => skill.active).map(skill => skill.name).join(", ")
        }
        function saveModelOptions() {
            backend.saveModelOptions({
                max_tokens: maxTokens.text,
                temperature: temperature.text,
                top_p: topP.text,
                reasoning_effort: backend.reasoningEffort,
                stop: stopSequences.text.split("\n").filter(value => value.length > 0),
                data_collection: privacyDeny.checked ? "deny" : "allow",
                zero_data_retention: zdr.checked
            })
        }
        function openAndLoad() {
            gatewayUrl.text = backend.gatewaySettings.url
            gatewayToken.clear()
            apiKey.clear()
            systemPrompt.text = backend.systemPrompt
            investigationModel.select(backend.investigationModel)
            let options = backend.modelOptions
            maxTokens.text = optionalText(options.max_tokens)
            temperature.text = optionalText(options.temperature)
            topP.text = optionalText(options.top_p)
            stopSequences.text = (options.stop || []).join("\n")
            privacyDeny.checked = options.data_collection !== "allow"
            zdr.checked = options.zero_data_retention === true
            let threshold = backend.contextThreshold
            compactionAuto.checked = threshold.automatic === true
            compactionShare.value = threshold.share > 0 ? threshold.share : 50
            let runner = backend.runtimeSettings
            runnerTarget.currentIndex = Math.max(0, runnerTarget.model.indexOf(runner.runnerTarget))
            runnerCpus.text = runner.cpus ? String(runner.cpus) : ""
            runnerMemory.text = runner.memoryGb ? String(runner.memoryGb) : ""
            runnerPids.text = String(runner.pids)
            gitDepth.text = String(runner.gitHistoryDepth)
            retentionDays.text = String(runner.retentionDays)
            backend.refreshStagedSessions()
            let config = backend.workspaceConfiguration()
            workspaceInstructions.text = config.instructions || ""
            customTools.text = JSON.stringify(config.custom_tools || [], null, 2)
            webTools.checked = config.web_tools === true
            unredactedPaths.text = (config.unredacted_paths || []).join("\n")
            backend.refreshPolicy()
            backend.refreshSkills()
            activeSkills.text = activeSkillNames()
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
                TabButton { text: "Policy" }
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
                        Label { text: "Investigation model (used by repository investigation)"; font.pixelSize: 18; font.weight: Font.DemiBold }
                        Label {
                            Layout.fillWidth: true
                            text: "Pick a model or type any OpenRouter model ID, then save."
                            color: window.mutedColor
                            wrapMode: Text.Wrap
                        }
                        ComboBox {
                            id: investigationModel
                            objectName: "investigationModelCombo"
                            Layout.fillWidth: true
                            editable: true
                            model: backend.models.map(item => item.id)
                            // The chosen ID lives here, not in currentIndex: a model-list reload
                            // (catalog refresh, favorites, conversation switch) resets the index.
                            property string selectedId: backend.investigationModel
                            function select(id) {
                                selectedId = id
                                currentIndex = model.indexOf(id)
                                editText = id
                            }
                            onActivated: selectedId = currentText
                            onAccepted: selectedId = editText.trim()
                            onModelChanged: select(selectedId)
                        }
                        Button {
                            objectName: "saveInvestigationModelButton"
                            text: "Save investigation model"
                            onClicked: {
                                investigationModel.select(investigationModel.editText.trim())
                                backend.saveInvestigationModel(investigationModel.selectedId)
                            }
                        }
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
                            onClicked: settingsDialog.saveModelOptions()
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
                            onClicked: settingsDialog.saveModelOptions()
                        }
                        Label { text: "Context compaction"; font.pixelSize: 16; font.weight: Font.DemiBold }
                        RowLayout {
                            Layout.fillWidth: true
                            Label { text: "Compact context at" }
                            SpinBox {
                                id: compactionShare
                                objectName: "compactionShare"
                                from: 10
                                to: 60
                                stepSize: 5
                                editable: true
                                enabled: !compactionAuto.checked
                                textFromValue: function(value) { return value + "%" }
                                valueFromText: function(text) { return parseInt(text) || 10 }
                                ToolTip.visible: hovered
                                ToolTip.text: "Share of the model's context window at which compaction starts"
                            }
                            CheckBox {
                                id: compactionAuto
                                objectName: "compactionAutomatic"
                                text: "Automatic"
                            }
                            Button {
                                text: "Save context threshold"
                                onClicked: backend.saveContextCompactionShare(compactionAuto.checked ? 0 : compactionShare.value)
                            }
                        }
                        Label {
                            objectName: "compactionHelp"
                            Layout.fillWidth: true
                            wrapMode: Text.Wrap
                            color: window.mutedColor
                            text: backend.contextThreshold.automatic
                                ? "Automatic: compaction starts at the profile's working-set cap, never above 75% of the model's window."
                                : "About " + String(Math.round(backend.contextThreshold.resolvedTokens)).replace(/\B(?=(\d{3})+(?!\d))/g, ",")
                                    + " tokens of a " + String(Math.round(backend.contextThreshold.windowTokens)).replace(/\B(?=(\d{3})+(?!\d))/g, ",")
                                    + "-token window for the current model. Runs never exceed 75% of the window."
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
                        Label {
                            objectName: "workspaceInstructionsHelp"
                            Layout.fillWidth: true
                            wrapMode: Text.Wrap
                            color: window.mutedColor
                            text: "Sent to the model with every Plan and Agent run in this workspace, marked as "
                                + "user-authored configuration (up to 32,000 characters). Unlike AGENTS.md in the "
                                + "project, they are trusted because you wrote them. They guide the model; they "
                                + "cannot grant tools, permissions, or network access."
                        }
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
                                text: backend.stagingBusy ? "Reviewing…" : "Staging summary"
                                enabled: backend.workspaceReady && !backend.generating && !backend.stagingBusy
                                onClicked: backend.reviewStaging()
                                ToolTip.visible: hovered
                                ToolTip.text: "Summarize staged work and list files changed on both sides"
                            }
                            Button { text: "Save workspace configuration"; onClicked: backend.saveWorkspaceConfiguration(workspaceInstructions.text, customTools.text) }
                            Button {
                                text: backend.stagingBusy ? "Reseeding…" : "Discard staged changes"
                                enabled: backend.workspaceReady && !backend.generating && !backend.stagingBusy
                                onClicked: backend.requestDiscardStaging()
                            }
                        }
                        RowLayout {
                            Button {
                                objectName: "openReviewButton"
                                text: "Review and select…"
                                enabled: backend.workspaceReady && !backend.generating && !backend.stagingBusy
                                onClicked: backend.openReview()
                                ToolTip.visible: hovered
                                ToolTip.text: "Review each staged file with its diff, run checks, and publish a selection"
                            }
                            Button {
                                text: "Publish staged changes"
                                enabled: backend.workspaceReady && !backend.generating && !backend.stagingBusy
                                onClicked: backend.publishStaged()
                                ToolTip.visible: hovered
                                ToolTip.text: "Review and publish staged work, including work kept from an earlier session"
                            }
                            Button {
                                text: "Bring in host changes"
                                enabled: backend.workspaceReady && !backend.generating && !backend.stagingBusy
                                onClicked: backend.rebaseStaging()
                                ToolTip.visible: hovered
                                ToolTip.text: "Update staging with files changed on the host since it was seeded"
                            }
                        }
                        Label {
                            visible: backend.stagingConflicts.length > 0
                            Layout.fillWidth: true
                            wrapMode: Text.Wrap
                            font.weight: Font.DemiBold
                            text: "Changed on the host and in staging (held back from publication):"
                        }
                        Repeater {
                            objectName: "stagingConflicts"
                            model: backend.stagingConflicts
                            delegate: RowLayout {
                                required property var modelData
                                Layout.fillWidth: true
                                Label { Layout.fillWidth: true; text: modelData.path; elide: Text.ElideMiddle }
                                Button {
                                    text: "Keep staged"
                                    enabled: !backend.generating && !backend.stagingBusy
                                    onClicked: backend.resolveConflict(modelData.path, "keep_staged")
                                }
                                Button {
                                    text: "Take host"
                                    enabled: !backend.generating && !backend.stagingBusy
                                    onClicked: backend.resolveConflict(modelData.path, "take_host")
                                }
                            }
                        }
                        Label { text: "Runner"; font.pixelSize: 18; font.weight: Font.DemiBold }
                        Label {
                            Layout.fillWidth: true
                            wrapMode: Text.Wrap
                            color: window.mutedColor
                            text: "Commands run in a separate, network-disabled runner container. Empty CPU or "
                                + "memory fields use a share of Docker's allocation. Changes apply when the local "
                                + "runtime next starts."
                        }
                        GridLayout {
                            objectName: "runnerSettings"
                            columns: 2
                            Layout.fillWidth: true
                            Label { text: "Image" }
                            ComboBox {
                                id: runnerTarget
                                Layout.fillWidth: true
                                model: ["auto", "base", "python", "node", "polyglot"]
                                displayText: currentText === "auto" && backend.runtimeSettings.detectedTarget
                                    ? "auto (" + backend.runtimeSettings.detectedTarget + ")" : currentText
                            }
                            Label { text: "CPUs (2–8)" }
                            TextField { id: runnerCpus; Layout.fillWidth: true; placeholderText: "From Docker"; inputMethodHints: Qt.ImhFormattedNumbersOnly }
                            Label { text: "Memory, GB (2–16)" }
                            TextField { id: runnerMemory; Layout.fillWidth: true; placeholderText: "From Docker"; inputMethodHints: Qt.ImhDigitsOnly }
                            Label { text: "Process limit (256–4,096)" }
                            TextField { id: runnerPids; Layout.fillWidth: true; inputMethodHints: Qt.ImhDigitsOnly }
                            Label { text: "Git history depth (0 = full)" }
                            TextField { id: gitDepth; Layout.fillWidth: true; inputMethodHints: Qt.ImhDigitsOnly }
                            Label { text: "Offer removal after, days" }
                            TextField { id: retentionDays; Layout.fillWidth: true; inputMethodHints: Qt.ImhDigitsOnly }
                        }
                        Button {
                            text: "Save runner settings"
                            onClicked: backend.saveRuntimeSettings({
                                runnerTarget: runnerTarget.currentText,
                                cpus: Number(runnerCpus.text || 0),
                                memoryGb: Number(runnerMemory.text || 0),
                                pids: runnerPids.text,
                                gitHistoryDepth: gitDepth.text,
                                retentionDays: retentionDays.text
                            })
                        }
                        RowLayout {
                            Label { text: "Staged sessions"; font.pixelSize: 18; font.weight: Font.DemiBold }
                            Item { Layout.fillWidth: true }
                            Button { text: "Refresh"; onClicked: backend.refreshStagedSessions() }
                        }
                        Label {
                            Layout.fillWidth: true
                            wrapMode: Text.Wrap
                            color: window.mutedColor
                            text: backend.stagedSessions.length
                                ? "Staged work is kept per workspace until you publish or discard it."
                                : "No staged sessions are stored."
                        }
                        Repeater {
                            objectName: "stagedSessions"
                            model: backend.stagedSessions
                            delegate: RowLayout {
                                required property var modelData
                                Layout.fillWidth: true
                                Label {
                                    Layout.fillWidth: true
                                    elide: Text.ElideMiddle
                                    text: (modelData.stale ? "⚠ unused · " : "")
                                        + (modelData.workspace || modelData.key) + " · " + modelData.runner
                                        + " · " + (Number(modelData.size) / 1048576).toFixed(1) + " MB · "
                                        + (modelData.age_days === null || modelData.age_days === undefined
                                            ? "age unknown" : Math.floor(modelData.age_days) + " days")
                                }
                                Button { text: "Remove"; onClicked: backend.removeStagedSession(modelData.key) }
                            }
                        }
                        RowLayout {
                            TextField { id: resumeId; Layout.fillWidth: true; placeholderText: "Interrupted run ID" }
                            Button { text: "Resume"; onClicked: backend.resumeRun(resumeId.text) }
                            Button { text: "Pause active agent"; onClicked: backend.pauseAgent() }
                        }
                        Label { text: "Documentation tools"; font.pixelSize: 18; font.weight: Font.DemiBold }
                        CheckBox {
                            id: webTools
                            objectName: "webToolsToggle"
                            text: "Let Agent sessions search the web and fetch documentation pages"
                            onToggled: backend.setWebTools(checked)
                            ToolTip.visible: hovered
                            ToolTip.text: "Off by default. Results are untrusted; pages off the documentation allowlist ask first."
                        }
                        Label { text: "Redaction exceptions"; font.pixelSize: 18; font.weight: Font.DemiBold }
                        Label {
                            Layout.fillWidth: true
                            wrapMode: Text.Wrap
                            color: window.mutedColor
                            text: "Secrets in tool results are replaced before they reach the model. List "
                                + "workspace-relative files (one per line, up to 50) whose contents the model "
                                + "may see unredacted."
                        }
                        TextArea {
                            id: unredactedPaths
                            objectName: "unredactedPaths"
                            Layout.fillWidth: true
                            Layout.preferredHeight: 70
                            placeholderText: "config/example.env"
                        }
                        Button { text: "Save redaction exceptions"; onClicked: backend.saveUnredactedPaths(unredactedPaths.text) }
                        Label { text: "Saved permissions"; font.pixelSize: 18; font.weight: Font.DemiBold }
                        GridLayout {
                            objectName: "ruleForm"
                            columns: 5
                            Layout.fillWidth: true
                            ComboBox {
                                id: ruleTool
                                model: ["bash", "write", "edit", "apply_patch", "read", "grep", "find", "ls", "status"]
                            }
                            ComboBox {
                                id: ruleDecision
                                model: ["allow_rule", "ask", "deny"]
                                displayText: ({ allow_rule: "Allow", ask: "Ask", deny: "Deny" })[currentText]
                            }
                            TextField {
                                id: rulePath
                                Layout.fillWidth: true
                                placeholderText: "Path prefix (optional)"
                            }
                            TextField {
                                id: ruleCommand
                                Layout.fillWidth: true
                                visible: ruleTool.currentText === "bash"
                                placeholderText: "Command prefix, e.g. pytest"
                            }
                            Button {
                                text: "Add rule"
                                enabled: backend.workspaceReady
                                onClicked: {
                                    backend.addPermissionRule({
                                        tool: ruleTool.currentText,
                                        mode: "agent",
                                        decision: ruleDecision.currentText,
                                        path_prefix: rulePath.text,
                                        command: ruleTool.currentText === "bash" ? ruleCommand.text : ""
                                    })
                                    rulePath.clear()
                                    ruleCommand.clear()
                                }
                            }
                        }
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
                                    text: ({ allow_rule: "Allow", ask: "Ask", deny: "Deny" }[modelData.decision] || modelData.decision)
                                        + " · " + modelData.tool + " · "
                                        + (modelData.executable
                                            ? [modelData.executable].concat(modelData.argument_prefix || []).join(" ")
                                            : (modelData.path_prefix || "exact scope"))
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
                        id: policyPane
                        objectName: "policySettings"
                        width: parent.width
                        spacing: 10
                        readonly property var policy: backend.workspacePolicy
                        Label { text: "Workspace policy"; font.pixelSize: 18; font.weight: Font.DemiBold }
                        Label {
                            Layout.fillWidth: true
                            wrapMode: Text.Wrap
                            color: window.mutedColor
                            text: "Read from " + (policyPane.policy.path || ".euesto/policy.toml") + " in the host "
                                + "workspace (never from staging). It takes effect only after you confirm it, "
                                + "and any change needs confirming again."
                        }
                        Label {
                            Layout.fillWidth: true
                            wrapMode: Text.Wrap
                            font.weight: Font.DemiBold
                            text: ({
                                absent: "No policy file; defaults apply.",
                                confirmed: "Confirmed and in effect.",
                                unconfirmed: "Not confirmed yet; defaults apply until you confirm it.",
                                changed: "Changed since you confirmed it; the confirmed version stays in effect.",
                                invalid: "Invalid; defaults apply. Fix the file and refresh.",
                                removed: "Removed from the workspace; defaults apply."
                            })[policyPane.policy.status] || ""
                        }
                        Repeater {
                            model: policyPane.policy.errors || []
                            delegate: Label {
                                required property var modelData
                                Layout.fillWidth: true
                                wrapMode: Text.Wrap
                                color: window.dark ? "#f0b45b" : "#a35b00"
                                text: "⚠ " + modelData
                            }
                        }
                        Label { text: "In effect"; font.weight: Font.DemiBold }
                        Label {
                            Layout.fillWidth: true
                            wrapMode: Text.Wrap
                            color: window.mutedColor
                            text: policyPane.policy.effective || ""
                        }
                        Label {
                            visible: (policyPane.policy.pending || "").length > 0
                                && policyPane.policy.status !== "confirmed"
                            text: "Waiting for confirmation"
                            font.weight: Font.DemiBold
                        }
                        Label {
                            Layout.fillWidth: true
                            visible: (policyPane.policy.pending || "").length > 0
                                && policyPane.policy.status !== "confirmed"
                            wrapMode: Text.Wrap
                            color: window.mutedColor
                            text: policyPane.policy.pending || ""
                        }
                        RowLayout {
                            Button { text: "Refresh"; enabled: backend.workspacePath.length > 0; onClicked: backend.refreshPolicy() }
                            Button {
                                text: "Confirm policy"
                                enabled: policyPane.policy.status === "unconfirmed"
                                    || policyPane.policy.status === "changed"
                                    || policyPane.policy.status === "removed"
                                onClicked: backend.confirmPolicy()
                            }
                        }
                        Label { text: "Dependency setup"; font.pixelSize: 18; font.weight: Font.DemiBold }
                        Label {
                            Layout.fillWidth: true
                            wrapMode: Text.Wrap
                            color: window.mutedColor
                            text: "Installs dependencies into staging in a one-shot container that can reach "
                                + "only the allowlisted registries. Isolated setup sees only the manifests and "
                                + "lockfiles; the Agent's runner stays offline."
                        }
                        Label {
                            Layout.fillWidth: true
                            wrapMode: Text.Wrap
                            text: policyPane.policy.setup && policyPane.policy.setup.command
                                ? "Command: " + policyPane.policy.setup.command
                                : "No setup command: add one to the policy or a lockfile to the workspace."
                        }
                        Label {
                            Layout.fillWidth: true
                            wrapMode: Text.Wrap
                            visible: text.length > 0
                            color: window.mutedColor
                            text: policyPane.policy.setup ? (policyPane.policy.setup.message || "") : ""
                        }
                        RowLayout {
                            Button {
                                text: policyPane.policy.setup && policyPane.policy.setup.state === "running"
                                    ? "Installing…" : "Install dependencies"
                                enabled: backend.workspaceReady && !backend.generating
                                    && !!(policyPane.policy.setup && policyPane.policy.setup.command)
                                    && !(policyPane.policy.setup && policyPane.policy.setup.state === "running")
                                onClicked: backend.runDependencySetup(false)
                            }
                            Button {
                                text: "Full setup…"
                                enabled: backend.workspaceReady && !backend.generating
                                    && !!(policyPane.policy.setup && policyPane.policy.setup.command)
                                    && !(policyPane.policy.setup && policyPane.policy.setup.state === "running")
                                onClicked: backend.runDependencySetup(true)
                                ToolTip.visible: hovered
                                ToolTip.text: "Runs setup against the whole staged copy, for projects that build during install"
                            }
                        }
                        TextArea {
                            objectName: "setupLog"
                            Layout.fillWidth: true
                            Layout.preferredHeight: 140
                            visible: text.length > 0
                            readOnly: true
                            wrapMode: TextEdit.Wrap
                            font.family: "Cascadia Code"
                            text: policyPane.policy.setup ? (policyPane.policy.setup.log || "") : ""
                        }
                        RowLayout {
                            Label { text: "Network audit log"; font.pixelSize: 18; font.weight: Font.DemiBold }
                            Item { Layout.fillWidth: true }
                            Button { text: "Load"; onClicked: backend.loadEgressAudit() }
                        }
                        Label {
                            Layout.fillWidth: true
                            wrapMode: Text.Wrap
                            color: window.mutedColor
                            text: (policyPane.policy.audit || []).length
                                ? "Most recent first. Every registry connection the proxy allowed or refused."
                                : "No network activity is recorded."
                        }
                        Repeater {
                            objectName: "egressAudit"
                            model: policyPane.policy.audit || []
                            delegate: Label {
                                required property var modelData
                                Layout.fillWidth: true
                                elide: Text.ElideRight
                                font.family: "Cascadia Code"
                                color: modelData.allowed ? window.textColor : (window.dark ? "#f0b45b" : "#a35b00")
                                text: new Date(Number(modelData.timestamp) * 1000).toLocaleString()
                                    + " · " + (modelData.allowed ? "allowed " : "refused ")
                                    + modelData.host + (modelData.port ? ":" + modelData.port : "")
                                    + (modelData.reason ? " · " + modelData.reason : "")
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
                            Component.onCompleted: text = settingsDialog.activeSkillNames()
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
        property bool allowRun: false
        property bool allowOnce: true
        property string commandHtml: ""
        property string network: ""
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
            Label {
                objectName: "approvalNetwork"
                Layout.fillWidth: true
                visible: approvalDialog.network === "registries"
                wrapMode: Text.Wrap
                color: window.dark ? "#f0b45b" : "#a35b00"
                text: "⚠ This command can reach the allowlisted package registries."
            }
            Label {
                objectName: "approvalCommand"
                Layout.fillWidth: true
                visible: approvalDialog.commandHtml.length > 0
                textFormat: Text.RichText
                wrapMode: Text.Wrap
                text: approvalDialog.commandHtml
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
                    visible: approvalDialog.allowRun
                    text: "Allow for run"
                    onClicked: { backend.resolveApproval(approvalDialog.approvalKey, "allow_run"); approvalDialog.close() }
                }
                Button {
                    visible: approvalDialog.allowRule
                    text: "Save exact rule"
                    onClicked: { backend.resolveApproval(approvalDialog.approvalKey, "allow_rule"); approvalDialog.close() }
                }
                Button {
                    visible: approvalDialog.allowOnce
                    text: "Allow once"
                    highlighted: true
                    onClicked: { backend.resolveApproval(approvalDialog.approvalKey, "allow_once"); approvalDialog.close() }
                }
            }
        }
    }

    Dialog {
        id: reviewDialog
        objectName: "reviewDialog"
        readonly property var review: backend.stagingReview
        title: "Review staged changes"
        modal: true
        visible: review.open === true
        anchors.centerIn: parent
        width: Math.min(1000, window.width - 40)
        height: Math.min(720, window.height - 40)
        closePolicy: Popup.NoAutoClose
        property string currentPath: ""
        function currentFile() {
            let files = review.files || []
            for (let i = 0; i < files.length; ++i)
                if (files[i].path === currentPath)
                    return files[i]
            return files.length ? files[0] : null
        }

        contentItem: ColumnLayout {
            spacing: 8
            Label {
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                text: (reviewDialog.review.summary || "")
                    + (reviewDialog.review.truncated ? " (list truncated)" : "")
            }
            RowLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                spacing: 8
                ColumnLayout {
                    Layout.preferredWidth: 360
                    Layout.fillHeight: true
                    RowLayout {
                        Button { text: "Select all"; onClicked: backend.setReviewAll(true) }
                        Button { text: "Select none"; onClicked: backend.setReviewAll(false) }
                    }
                    ListView {
                        id: reviewFiles
                        objectName: "reviewFiles"
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        clip: true
                        model: reviewDialog.review.files || []
                        ScrollBar.vertical: ScrollBar {}
                        delegate: ItemDelegate {
                            id: reviewRow
                            required property var modelData
                            width: reviewFiles.width
                            highlighted: modelData.path === reviewDialog.currentPath
                            onClicked: reviewDialog.currentPath = modelData.path
                            contentItem: RowLayout {
                                CheckBox {
                                    checked: reviewRow.modelData.selected
                                    onToggled: backend.setReviewSelected(reviewRow.modelData.path, checked)
                                }
                                ColumnLayout {
                                    Layout.fillWidth: true
                                    spacing: 0
                                    Label {
                                        Layout.fillWidth: true
                                        elide: Text.ElideMiddle
                                        text: reviewRow.modelData.path
                                        font.weight: reviewRow.modelData.confirm ? Font.DemiBold : Font.Normal
                                    }
                                    Label {
                                        Layout.fillWidth: true
                                        elide: Text.ElideRight
                                        color: reviewRow.modelData.flags.length
                                            ? (window.dark ? "#f0b45b" : "#a35b00") : window.mutedColor
                                        text: reviewRow.modelData.operation
                                            + " · +" + reviewRow.modelData.added + " −" + reviewRow.modelData.removed
                                            + (reviewRow.modelData.flags.length
                                                ? " · ⚠ " + reviewRow.modelData.flags.join(", ") : "")
                                    }
                                }
                            }
                        }
                    }
                }
                ScrollView {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    TextArea {
                        objectName: "reviewDiff"
                        readOnly: true
                        wrapMode: TextEdit.NoWrap
                        font.family: "Cascadia Code"
                        text: {
                            let file = reviewDialog.currentFile()
                            return file ? (file.diff || "(no textual diff)") : "No staged changes."
                        }
                    }
                }
            }
            Label {
                Layout.fillWidth: true
                visible: (reviewDialog.review.neverPublish || []).length > 0
                wrapMode: Text.Wrap
                color: window.mutedColor
                text: "Never published by workspace policy: " + (reviewDialog.review.neverPublish || []).join(", ")
            }
            Label {
                visible: (reviewDialog.review.checks || []).length > 0
                text: "Checks"
                font.weight: Font.DemiBold
            }
            Repeater {
                objectName: "reviewChecks"
                model: reviewDialog.review.checks || []
                delegate: RowLayout {
                    id: checkRow
                    required property var modelData
                    Layout.fillWidth: true
                    Label {
                        Layout.fillWidth: true
                        elide: Text.ElideRight
                        text: (checkRow.modelData.required ? "★ " : "") + checkRow.modelData.name
                            + " · " + checkRow.modelData.command + " · "
                            + (checkRow.modelData.running ? "running…"
                                : checkRow.modelData.status + (checkRow.modelData.status !== "not_run"
                                    && !checkRow.modelData.current ? " (stale)" : ""))
                        color: checkRow.modelData.status === "passed" && checkRow.modelData.current
                            ? window.textColor : (checkRow.modelData.status === "failed"
                                ? (window.dark ? "#f0b45b" : "#a35b00") : window.mutedColor)
                        ToolTip.visible: checkMouse.containsMouse && checkRow.modelData.output.length > 0
                        ToolTip.text: checkRow.modelData.output
                        MouseArea { id: checkMouse; anchors.fill: parent; hoverEnabled: true }
                    }
                    Button {
                        text: "Run"
                        enabled: !backend.generating && !backend.stagingBusy
                        onClicked: backend.runCheck(checkRow.modelData.name)
                    }
                }
            }
            RowLayout {
                Layout.alignment: Qt.AlignRight
                Label {
                    text: (reviewDialog.review.selectedCount || 0) + " of "
                        + (reviewDialog.review.files || []).length + " selected"
                    color: window.mutedColor
                }
                Button {
                    text: "Refresh"
                    enabled: !backend.stagingBusy
                    onClicked: backend.openReview()
                }
                Button {
                    text: "Close"
                    onClicked: backend.closeReview()
                }
                Button {
                    text: "Publish selected"
                    highlighted: true
                    enabled: (reviewDialog.review.selectedCount || 0) > 0
                        && !backend.generating && !backend.stagingBusy
                    onClicked: backend.publishSelection()
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
        function show(heading, message) {
            title = heading
            noticeText.text = message
            open()
        }
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

    Dialog {
        id: leaveDialog
        objectName: "leaveDialog"
        modal: true
        anchors.centerIn: parent
        width: Math.min(640, window.width - 50)
        property bool resolved: false
        onOpened: resolved = false
        onClosed: if (!resolved) backend.resolveLeave("cancel")
        function choose(choice) {
            resolved = true
            backend.resolveLeave(choice)
            close()
        }
        contentItem: ColumnLayout {
            spacing: 12
            Label {
                id: leaveText
                Layout.fillWidth: true
                wrapMode: Text.Wrap
            }
            RowLayout {
                Layout.alignment: Qt.AlignRight
                Button { text: "Review and publish"; highlighted: true; onClicked: leaveDialog.choose("publish") }
                Button { text: "Discard"; onClicked: leaveDialog.choose("discard") }
                Button { text: "Keep for later"; onClicked: leaveDialog.choose("keep") }
                Button { text: "Cancel"; onClicked: leaveDialog.choose("cancel") }
            }
        }
    }

    Connections {
        target: backend
        function onLeaveRequested(title, message) {
            leaveDialog.title = title
            leaveText.text = message
            leaveDialog.open()
        }
        function onInfoRequested(title, message) {
            noticeDialog.show(title, message)
        }
        function onErrorRequested(title, message) {
            noticeDialog.show(title, message)
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
            approvalDialog.allowRun = value.allowRun === true
            approvalDialog.allowOnce = value.allowOnce !== false
            approvalDialog.commandHtml = value.commandHtml || ""
            approvalDialog.network = value.network || ""
            approvalDialog.title = value.title
            approvalSummary.text = value.summary
            approvalDetails.text = value.details
            approvalDialog.open()
        }
        function onFileExported(name) {
            noticeDialog.show("Export complete", "Exported " + name)
        }
        function onFileImported(name) {
            noticeDialog.show("Import complete", "Imported " + name)
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
