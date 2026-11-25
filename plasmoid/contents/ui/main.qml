/*
 * The KDE face of protonpass-tray. A widget, not a program: every decision is made by the
 * `protonpass-tray` command, which this installs nothing of and only ever calls.
 *
 * SIX THINGS WORTH KNOWING BEFORE CHANGING THIS FILE
 *
 * 1. The state comes from the engine's cache FILE, never from asking the API. The Proton Pass
 *    lock is an idle timer, so a widget that polled for its icon would keep the session
 *    unlocked for ever - and be right. Opening the popup IS allowed to ask: that is a person
 *    looking at it and expecting an answer, not a timer.
 *
 * 2. Reading that file means `cat`, not XMLHttpRequest. QML refuses GET on a file:// URL
 *    unless QML_XHR_ALLOW_FILE_READ=1 is in plasmashell's environment, which a widget cannot
 *    arrange and a user should not have to.
 *
 * 3. The WORDING is this file's own. The cache carries a state name and two numbers; it does
 *    not carry a sentence, and a widget that printed `parsed["description"]` showed the user
 *    the string "no-lock" until 2026-11-11.
 *
 * 4. The lock code is typed here and reaches the engine on stdin: `printf | protonpass-tray
 *    --code-stdin`. That puts it in the argv of one short-lived shell, where another process
 *    of this same user could read it for a fraction of a second. That is a deliberate trade
 *    and not an oversight. QML can neither write a file nor open a socket, so the only
 *    alternative is for the engine to raise a dialog of its own, which is a second window for
 *    something that belongs in this one; and the lock defends against a person at your
 *    keyboard, not against a process already running as you - one of those can read the
 *    keyring, ptrace plasmashell, or keylog any dialog in any case. The code is never passed
 *    as an ARGUMENT to protonpass-tray, and the field is cleared the moment it is sent.
 *
 * 5. Running anything from QML means Plasma5Support's executable data engine. It is a
 *    compatibility layer that Plasma 6 has shipped no declarative replacement for.
 *
 * 6. A prompt waiting in a terminal announces itself in asking/*.json beside the state file,
 *    and rewrites its file while it waits. One not rewritten lately is ignored: it was killed.
 *    The notification is in a file of its own, behind a Loader, so that a machine without
 *    org.kde.notification loses the notification and not the widget.
 */

import QtQuick
import QtQuick.Layouts
import org.kde.plasma.plasmoid
import org.kde.plasma.components as PlasmaComponents3
import org.kde.plasma.extras as PlasmaExtras
import org.kde.kirigami as Kirigami
import org.kde.plasma.plasma5support as Plasma5Support

PlasmoidItem {
    id: root

    readonly property string engine: "protonpass-tray"

    property string lockState: "unknown"
    property int idleTimeout: 0
    property double since: 0
    property string statePath: ""
    property bool engineMissing: false
    property bool busy: false
    property string lastError: ""

    // Prompts waiting for the code somewhere, see (6). Each is {id, who, since, alive}.
    property var waiting: []
    readonly property int staleAfter: 15     // seconds; asking.py's STALE_S
    // Long enough that a person at that very terminal has answered and needs no telling.
    readonly property int tellAfter: 4
    property var told: ({})
    readonly property string waitingText: waiting.length === 0 ? ""
        : waiting[0].who + (waiting.length > 1 ? " and " + (waiting.length - 1) + " more" : "")
          + (waiting.length > 1 ? " are" : " is") + " waiting for the lock code."

    // The engine's exit codes, which are its interface and not its prose: 10 locked, 11 no
    // lock, 12 not signed in. Each means "here is the state", not "something went wrong", and
    // the state is already on screen in this widget's own words - so painting stderr red for
    // one put a second, redder copy of the same sentence beside it, reading as a failure.
    // Only 2 (usage), 3 (unrecognised) and 4 (no pass-cli) are faults worth showing.
    readonly property var stateCodes: [0, 10, 11, 12]
    property double nowSeconds: Date.now() / 1000

    readonly property bool locked: lockState === "locked"
    readonly property int expiresIn: {
        if (lockState !== "unlocked" || idleTimeout <= 0 || since <= 0)
            return -1;
        var left = Math.floor(since + idleTimeout - nowSeconds);
        return left > 0 ? left : 0;
    }

    // See (3): the sentences live here, not in the cache.
    readonly property string headline: {
        if (engineMissing)
            return "protonpass-tray is not installed";
        switch (lockState) {
        case "locked": return "Locked";
        case "unlocked": return "Unlocked";
        case "no-lock": return "No lock set up";
        case "no-session": return "Not signed in to Proton Pass";
        default: return "Checking…";
        }
    }

    readonly property string detail: {
        switch (lockState) {
        case "locked": return "Enter your code to use Proton Pass again.";
        case "no-lock": return "Choose a code, and the session will lock itself when idle.";
        case "no-session": return "Run pass-cli login in a terminal first.";
        default: return "";
        }
    }

    // The Proton Pass application's own icon, REFERENCED from the user's installation and never
    // shipped here; our own padlock where it is not installed. A third-party tool may say
    // which service a status is about; it may not dress up as it.
    //
    // First the mark alone on transparency, the way the browser extension shows it: the
    // launcher icon is the same mark small on a white plate, which on a panel is mostly plate.
    readonly property string markPath: "file:///usr/lib/proton-pass/resources/assets/"
        + "Square44x44Logo.targetsize-256_altform-unplated.png"
    readonly property string baseIcon: markProbe.status === Image.Ready ? markPath : "proton-pass"
    Image {
        // Kirigami.Icon does not say whether a FILE it was given exists; this does.
        id: markProbe
        visible: false
        asynchronous: true
        source: root.markPath
        sourceSize.width: 1
    }
    readonly property string glyphName: {
        switch (lockState) {
        case "locked": return "object-locked";
        case "unlocked": return "object-unlocked";
        case "no-lock": return "object-unlocked";
        case "no-session": return "dialog-warning";
        default: return "dialog-question";
        }
    }
    readonly property string emblem: {
        switch (lockState) {
        // As in the browser extension: a padlock when locked, the plain mark when not. An
        // open padlock and a closed one differ too little at this size, so the open one is
        // for "no lock set up", which would otherwise look merely unlocked.
        case "locked": return "emblem-locked";
        case "unlocked": return "";
        case "no-lock": return "emblem-unlocked";
        default: return "emblem-alert";
        }
    }
    // Dimmed is for "cannot say". Locked is an ordinary state, and the padlock says it.
    readonly property bool bright: lockState === "unlocked" || lockState === "locked"
                                   || lockState === "no-lock"

    // pass-cli accepts 30 to 900 seconds and nothing outside it.
    readonly property var timeouts: [
        { label: "30 seconds", value: 30 },
        { label: "2 minutes", value: 120 },
        { label: "5 minutes", value: 300 },
        { label: "10 minutes", value: 600 },
        { label: "15 minutes", value: 900 }
    ]
    function timeoutIndex(seconds) {
        for (var i = 0; i < timeouts.length; i++)
            if (timeouts[i].value === seconds)
                return i;
        return -1;
    }

    preferredRepresentation: compactRepresentation
    toolTipMainText: "Proton Pass"
    toolTipSubText: {
        if (lastError !== "")
            return lastError;
        if (expiresIn > 0)
            return headline + " - locks itself in " + root.spell(expiresIn);
        return headline;
    }

    function spell(seconds) {
        var m = Math.floor(seconds / 60);
        var s = seconds % 60;
        return m > 0 ? m + "m " + s + "s" : s + "s";
    }

    // Single quotes, with any embedded quote closed and reopened the POSIX way. The code is
    // typed by a person and will not normally contain one, which is exactly why it must be
    // handled: the one that does would otherwise end the quoting and run as a command.
    function quoted(text) {
        return "'" + String(text).split("'").join("'\\''") + "'";
    }

    Plasma5Support.DataSource {
        id: shell
        engine: "executable"
        connectedSources: []

        onNewData: function (source, data) {
            disconnectSource(source);
            var out = (data["stdout"] || "").trim();
            var status = data["exit code"];

            if (source.indexOf("XDG_RUNTIME_DIR") !== -1) {
                root.statePath = out;
                root.readState();
                return;
            }
            if (source.indexOf("cat ") === 0 && source.indexOf("/asking/") !== -1) {
                root.applyWaiting(out);      // nothing waiting is an empty answer
                return;
            }
            if (source.indexOf("cat ") === 0) {
                if (status === 0 && out !== "")
                    root.applyState(out);
                return;
            }

            root.busy = false;
            // 127 is the shell's "command not found": the widget is installed, the engine is
            // not. A store can only ever deliver this half of the program.
            root.engineMissing = (status === 127);
            if (root.engineMissing)
                return;
            // Whatever the engine said when it failed. Dropping this on the floor is what made
            // a setup that could not run look exactly like one that had worked.
            root.lastError = (root.stateCodes.indexOf(status) !== -1)
                ? "" : (data["stderr"] || "").trim().split("\n")[0];
            root.readState();
        }

        function exec(command) {
            connectSource(command);
        }
    }

    function run(args) {
        if (root.engineMissing)
            return;
        root.busy = true;
        shell.exec(root.engine + " " + args);
    }

    // See (4): on stdin, never as an argument.
    function runWithCode(args, code) {
        if (root.engineMissing)
            return;
        root.busy = true;
        shell.exec("printf '%s' " + root.quoted(code) + " | " + root.engine + " " + args
                   + " --code-stdin");
    }

    function readState() {
        if (statePath === "")
            return;
        shell.exec("cat " + statePath);
        // The touch says that someone here would be told: `unlock --wait` waits only then.
        shell.exec("cat " + statePath.replace("state.json", "asking/*.json") + " 2>/dev/null; touch "
                   + statePath.replace("state.json", "listening"));
    }

    function applyWaiting(text) {
        var now = Date.now() / 1000;
        var found = [];
        var lines = text.split("\n");
        for (var i = 0; i < lines.length; i++) {
            try {
                var one = JSON.parse(lines[i]);
                if (one["id"] && now - one["alive"] < root.staleAfter)
                    found.push(one);
            } catch (e) {
                // an empty line, or one being written
            }
        }
        root.waiting = found;

        // Told once each, so closing the notification means "I have seen it". Not while the
        // popup is open: it says the same thing, with the field to type in.
        var fresh = false;
        for (var j = 0; j < found.length; j++) {
            if (root.told[found[j].id] || now - found[j].since < root.tellAfter)
                continue;
            root.told[found[j].id] = true;
            fresh = true;
        }
        if (notice.item === null)
            return;
        if (found.length === 0 || root.expanded)
            notice.item.close();
        else if (fresh)
            notice.item.tell(root.waitingText);
    }

    Loader {
        id: notice
        source: "Notice.qml"
        Connections {
            target: notice.item
            ignoreUnknownSignals: true
            function onWanted() { root.expanded = true; }
        }
    }

    function applyState(text) {
        try {
            var parsed = JSON.parse(text);
            root.lockState = parsed["lock"] || "unknown";
            root.idleTimeout = parsed["idle_timeout"] || 0;
            root.since = parsed["since"] || 0;
        } catch (e) {
            root.lockState = "unknown";
        }
    }

    onExpandedChanged: {
        if (!expanded)
            return;
        if (notice.item !== null)
            notice.item.close();
        root.run("status --refresh --json");
    }

    Component.onCompleted: shell.exec(
        "sh -c 'echo ${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/protonpass-tray/state.json'")

    Timer {
        interval: 2000
        running: true
        repeat: false
        onTriggered: root.run("status --refresh --json")
    }

    Timer {
        // The countdown, and the inference when it runs out. No process, no API call.
        interval: 1000
        running: true
        repeat: true
        onTriggered: {
            root.nowSeconds = Date.now() / 1000;
            if (root.lockState === "unlocked" && root.expiresIn === 0)
                root.lockState = "locked";
        }
    }

    Timer {
        // Pick up what the command line or another face may have written. One `cat`.
        interval: 5000
        running: true
        repeat: true
        onTriggered: root.readState()
    }

    compactRepresentation: MouseArea {
        id: compact
        property bool wasExpanded: false
        activeFocusOnTab: true
        Accessible.name: root.headline
        Accessible.role: Accessible.Button

        Kirigami.Icon {
            id: panelIcon
            anchors.fill: parent
            source: root.baseIcon
            fallback: root.glyphName
            active: compact.containsMouse
            opacity: (root.bright ? 1.0 : 0.45) * (root.busy ? 0.5 : 1.0)
        }

        Image {
            // Only over the real Proton icon: on the fallback it would be a padlock wearing
            // a padlock.
            //
            // An Image and not a Kirigami.Icon, which resolves even a file's NAME against the
            // icon theme first: Breeze has an emblem-locked of its own, an orange square, and
            // that is what stood here until 2026-11-25.
            visible: panelIcon.valid && root.emblem !== ""
            source: root.emblem === "" ? "" : Qt.resolvedUrl("../icons/" + root.emblem + ".svg")
            width: Math.round(parent.width * 0.69)
            height: width
            sourceSize: Qt.size(width * Screen.devicePixelRatio, height * Screen.devicePixelRatio)
            anchors.right: parent.right
            anchors.bottom: parent.bottom
        }

        onPressed: wasExpanded = root.expanded
        onClicked: root.expanded = !wasExpanded
    }

    fullRepresentation: PlasmaExtras.Representation {
        Layout.minimumWidth: Kirigami.Units.gridUnit * 17
        Layout.minimumHeight: Kirigami.Units.gridUnit * 13

        header: PlasmaExtras.PlasmoidHeading {
            RowLayout {
                anchors.fill: parent
                spacing: Kirigami.Units.smallSpacing
                Kirigami.Icon {
                    source: root.baseIcon
                    fallback: root.glyphName
                    Layout.preferredWidth: Kirigami.Units.iconSizes.smallMedium
                    Layout.preferredHeight: Kirigami.Units.iconSizes.smallMedium
                }
                ColumnLayout {
                    spacing: 0
                    Layout.fillWidth: true
                    PlasmaExtras.Heading {
                        level: 4
                        text: root.headline
                        Layout.fillWidth: true
                        elide: Text.ElideRight
                    }
                    PlasmaComponents3.Label {
                        visible: text !== ""
                        text: root.detail
                        opacity: 0.7
                        font: Kirigami.Theme.smallFont
                        wrapMode: Text.WordWrap
                        Layout.fillWidth: true
                    }
                }
                PlasmaComponents3.BusyIndicator {
                    visible: root.busy
                    running: root.busy
                    Layout.preferredWidth: Kirigami.Units.iconSizes.small
                    Layout.preferredHeight: Kirigami.Units.iconSizes.small
                }
            }
        }

        ColumnLayout {
            anchors.fill: parent
            spacing: Kirigami.Units.largeSpacing

            // --- unlocked: how long is left, and a way to end it now --------------------
            ColumnLayout {
                visible: root.lockState === "unlocked"
                spacing: Kirigami.Units.smallSpacing
                Layout.fillWidth: true

                PlasmaComponents3.ProgressBar {
                    visible: root.expiresIn >= 0
                    from: 0
                    to: Math.max(root.idleTimeout, 1)
                    value: Math.max(root.expiresIn, 0)
                    Layout.fillWidth: true
                }
                PlasmaComponents3.Label {
                    visible: root.expiresIn >= 0
                    text: "Locks itself in " + root.spell(root.expiresIn) + " if unused"
                    opacity: 0.7
                    font: Kirigami.Theme.smallFont
                    Layout.fillWidth: true
                }
            }

            // --- unlocked: how long idle is too long, changed with the code -------------
            GridLayout {
                visible: root.lockState === "unlocked"
                columns: 2
                columnSpacing: Kirigami.Units.smallSpacing
                rowSpacing: Kirigami.Units.smallSpacing
                Layout.fillWidth: true

                PlasmaComponents3.Label { text: "Lock after" }
                PlasmaComponents3.ComboBox {
                    id: changeBox
                    model: root.timeouts
                    textRole: "label"
                    valueRole: "value"
                    // A lock made in a terminal can have a time that is not in the list.
                    displayText: currentIndex < 0 ? root.spell(root.idleTimeout) : currentText
                    function follow() {
                        currentIndex = root.timeoutIndex(root.idleTimeout);
                    }
                    readonly property bool changed: currentIndex >= 0
                                                    && currentValue !== root.idleTimeout
                    Component.onCompleted: follow()
                    Layout.fillWidth: true
                    Connections {
                        target: root
                        function onIdleTimeoutChanged() { changeBox.follow(); }
                        function onExpandedChanged() {
                            changeBox.follow();
                            changeField.text = "";
                        }
                    }
                }

                // Only once there is something to change: the code is not needed before that.
                PlasmaComponents3.Label {
                    visible: changeBox.changed
                    text: "Code"
                }
                RowLayout {
                    visible: changeBox.changed
                    spacing: Kirigami.Units.smallSpacing
                    Layout.fillWidth: true

                    PlasmaComponents3.TextField {
                        id: changeField
                        echoMode: TextInput.Password
                        placeholderText: "Your lock code"
                        inputMethodHints: Qt.ImhSensitiveData
                        Layout.fillWidth: true
                        onAccepted: changeButton.go()
                    }
                    PlasmaComponents3.Button {
                        id: changeButton
                        text: "Change"
                        enabled: !root.busy && changeField.text !== ""
                        function go() {
                            if (changeField.text === "" || !changeBox.changed)
                                return;
                            root.lastError = "";
                            root.runWithCode("set-timeout " + changeBox.currentValue,
                                             changeField.text);
                            changeField.text = "";
                        }
                        onClicked: go()
                    }
                }
            }

            PlasmaComponents3.Label {
                visible: root.waitingText !== "" && root.lockState !== "unlocked"
                text: root.waitingText
                textFormat: Text.PlainText
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }

            // --- locked: the code, and nothing else ------------------------------------
            RowLayout {
                visible: root.locked || (root.lockState === "unknown" && !root.engineMissing)
                spacing: Kirigami.Units.smallSpacing
                Layout.fillWidth: true

                PlasmaComponents3.TextField {
                    id: unlockField
                    echoMode: TextInput.Password
                    placeholderText: "Code"
                    inputMethodHints: Qt.ImhSensitiveData
                    Layout.fillWidth: true
                    onAccepted: unlockButton.go()
                    // Opened from the panel or from a notification, it is to type this.
                    onVisibleChanged: if (visible) forceActiveFocus()
                    Connections {
                        target: root
                        function onExpandedChanged() {
                            if (root.expanded && unlockField.visible)
                                unlockField.forceActiveFocus();
                        }
                    }
                }
                PlasmaComponents3.Button {
                    id: unlockButton
                    text: "Unlock"
                    icon.name: "object-unlocked"
                    enabled: !root.busy && unlockField.text !== ""
                    function go() {
                        if (unlockField.text === "")
                            return;
                        root.runWithCode("unlock", unlockField.text);
                        unlockField.text = "";       // see (4): cleared the moment it is sent
                    }
                    onClicked: go()
                }
            }

            // --- no lock yet: choose a code, confirm it, choose when ---------------------
            GridLayout {
                visible: root.lockState === "no-lock"
                columns: 2
                columnSpacing: Kirigami.Units.smallSpacing
                rowSpacing: Kirigami.Units.smallSpacing
                Layout.fillWidth: true

                PlasmaComponents3.Label { text: "Code" }
                PlasmaComponents3.TextField {
                    id: pinField
                    echoMode: TextInput.Password
                    placeholderText: "A PIN is fine"
                    inputMethodHints: Qt.ImhSensitiveData
                    Layout.fillWidth: true
                }

                PlasmaComponents3.Label { text: "Again" }
                PlasmaComponents3.TextField {
                    id: pinAgain
                    echoMode: TextInput.Password
                    inputMethodHints: Qt.ImhSensitiveData
                    Layout.fillWidth: true
                    onAccepted: createButton.go()
                }

                PlasmaComponents3.Label { text: "Lock after" }
                PlasmaComponents3.ComboBox {
                    id: timeoutBox
                    model: root.timeouts
                    textRole: "label"
                    valueRole: "value"
                    currentIndex: 3
                    Layout.fillWidth: true
                }
            }

            PlasmaComponents3.Label {
                visible: root.lastError !== ""
                text: root.lastError
                color: Kirigami.Theme.negativeTextColor
                font: Kirigami.Theme.smallFont
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }

            Item { Layout.fillHeight: true }

            RowLayout {
                Layout.fillWidth: true
                spacing: Kirigami.Units.smallSpacing

                PlasmaComponents3.Button {
                    id: createButton
                    visible: root.lockState === "no-lock"
                    text: "Create the lock"
                    icon.name: "object-locked"
                    enabled: !root.busy && pinField.text !== ""
                    function go() {
                        if (pinField.text === "")
                            return;
                        if (pinField.text !== pinAgain.text) {
                            root.lastError = "The two codes are different.";
                            return;
                        }
                        root.lastError = "";
                        root.runWithCode("setup --idle-timeout " + timeoutBox.currentValue,
                                         pinField.text);
                        pinField.text = "";
                        pinAgain.text = "";
                    }
                    onClicked: go()
                    Layout.fillWidth: true
                }

                PlasmaComponents3.Button {
                    visible: root.lockState === "unlocked"
                    text: "Lock now"
                    icon.name: "object-locked"
                    enabled: !root.busy
                    onClicked: root.run("lock")
                    Layout.fillWidth: true
                }

                PlasmaComponents3.Button {
                    visible: !root.engineMissing
                    text: "Check"
                    icon.name: "view-refresh"
                    enabled: !root.busy
                    onClicked: root.run("status --refresh --json")
                }
            }
        }
    }
}
