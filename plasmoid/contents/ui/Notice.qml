/*
 * "Something is waiting for the lock code." One notification, reused.
 *
 * A file of its own because main.qml loads it through a Loader: where org.kde.notification is
 * missing this file fails alone. It has no reply field, although the module offers one: that
 * one shows what is typed into it.
 */

import QtQuick
import org.kde.notification

Item {
    id: root

    signal wanted()

    function tell(what) {
        notification.text = what;
        notification.sendEvent();
    }

    function close() {
        notification.close();
    }

    Notification {
        id: notification
        // plasmashell's own event, because a widget from a store cannot install a notifyrc.
        componentName: "plasma_workspace"
        eventId: "notification"
        title: "Proton Pass is locked"
        iconName: "object-locked"
        // Stays until answered, closed, or no longer true: what it is about is waiting too.
        flags: Notification.Persistent
        defaultAction: open
        actions: [
            NotificationAction {
                label: "Unlock…"
                onActivated: root.wanted()
            }
        ]
    }

    NotificationAction {
        id: open
        label: "Open"
        onActivated: root.wanted()
    }
}
