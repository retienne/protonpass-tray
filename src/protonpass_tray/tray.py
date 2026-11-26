"""The Qt tray icon: one face over actions.py, for any desktop with a system tray.

Qt's QSystemTrayIcon speaks StatusNotifierItem, so this works on Plasma, XFCE, LXQt,
Cinnamon and most Wayland panels with no help. GNOME removed tray icons; there it needs the
AppIndicator extension, or the GNOME Shell extension that ships in this repository.

TWO THINGS THAT SHAPE THIS FILE

It never polls the Proton Pass API. The lock is an idle timer, so polling would reset it and
the icon would show "unlocked" for ever, correctly. Instead: one refresh at startup, a local
read of the shared cache every couple of seconds (free - it is a file in $XDG_RUNTIME_DIR),
and a countdown this process runs itself. When the countdown reaches zero the session has
locked itself, so the icon says so WITHOUT asking; the tooltip marks that inferred, because
any pass-cli command in any terminal resets the server's timer without telling us.

It is also the prompt of last resort for the rest of the machine. A shell hook or a systemd
unit with no terminal connects to the socket in prompt.py and gets this process to show the
dialog, which is why the socket server is here rather than an afterthought.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time
from importlib import resources
from typing import TYPE_CHECKING

from PyQt6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QAction, QColor, QIcon, QPainter, QPalette, QPixmap
from PyQt6.QtNetwork import QLocalServer
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import (
    QApplication,
    QInputDialog,
    QLineEdit,
    QMenu,
    QMessageBox,
    QSystemTrayIcon,
)

from . import actions, prompt, state
from .state import Lock

if TYPE_CHECKING:
    from importlib.abc import Traversable

#: How often the cache is re-read and the countdown redrawn. Local work only.
TICK_MS = 2000

#: Where the Proton Pass application's own icon is, when it is installed. First the mark alone,
#: on transparency, the way the browser extension shows it: the application's launcher icon is
#: the same mark small on a white plate, which at badge size is all plate. Where that file is
#: not, the icon theme, and then the launcher icon where Proton's .deb puts it - which the
#: freedesktop spec calls a legacy location and Qt does not search.
#:
#: NOTE ON USING IT AT ALL: this REFERENCES an icon the user's own Proton package installed.
#: No Proton artwork is shipped in this repository, and nothing that represents this project
#: - the application icon, the widget in Plasma's chooser, a store listing - uses their mark.
#: A third-party tool may say which service a status is about; it may not dress up as it.
#: Which is why the mark is only ever a BADGE on our own glyph: until 2026-11-26 the icon was
#: the mark itself, and an unlocked session sat in the panel looking exactly like the Proton
#: Pass application beside it.
PROTON_MARK_PATH = (
    "/usr/lib/proton-pass/resources/assets/Square44x44Logo.targetsize-256_altform-unplated.png"
)
PROTON_ICON_NAME = "proton-pass"
PROTON_ICON_PATHS = (
    "/usr/share/pixmaps/proton-pass.png",
    "/usr/lib/proton-pass/resources/assets/icon.png",
)

#: Our own glyph for each state: a terminal, with the state in its screen. Never the theme's
#: padlocks any more - the terminal is what makes this recognisably itself.
_GLYPHS = {
    Lock.UNLOCKED: "unlocked",
    Lock.LOCKED: "locked",
    Lock.NO_LOCK: "no-lock",
    Lock.NO_SESSION: "unknown",
    Lock.UNKNOWN: "unknown",
}

#: States drawn at full strength. Dimmed is kept for "cannot say".
_BRIGHT = (Lock.UNLOCKED, Lock.LOCKED, Lock.NO_LOCK)

#: How much of the icon's width the Proton mark takes, in the top-right corner over the
#: title bar, which carries no state. And the gap cut around it, so that its purple does not
#: run into the title bar: one pixel at panel size.
BADGE = 0.46
GAP = 1 / 22

_COMPOSED: dict[tuple[Lock, int], QIcon] = {}


def glyph_path(name: str) -> Traversable:
    # Inside the package rather than in data/, so a pipx install has them too.
    return resources.files(__package__) / "icons" / f"protonpass-tray-{name}.svg"


def proton_icon() -> QIcon | None:
    if os.path.exists(PROTON_MARK_PATH):
        return QIcon(PROTON_MARK_PATH)
    icon = QIcon.fromTheme(PROTON_ICON_NAME)
    if not icon.isNull():
        return icon
    for path in PROTON_ICON_PATHS:
        if os.path.exists(path):
            return QIcon(path)
    return None


def icon_for(status: Lock, ink: QColor | None = None) -> QIcon:
    """Our terminal glyph carrying the state, badged with the Proton mark where it exists."""
    if ink is None:
        ink = QApplication.palette().color(QPalette.ColorRole.WindowText)
    key = (status, ink.rgba())
    if key not in _COMPOSED:
        icon, mark = QIcon(), proton_icon()
        for size in (16, 22, 24, 32, 48, 64):
            icon.addPixmap(_compose(status, size, ink, mark))
        _COMPOSED[key] = icon
    return _COMPOSED[key]


def _compose(status: Lock, size: int, ink: QColor, mark: QIcon | None) -> QPixmap:
    canvas = QPixmap(size, size)
    canvas.fill(Qt.GlobalColor.transparent)
    painter = QPainter(canvas)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        # The glyph in the panel's ink: QtSvg draws currentColor black, so the SVG lays down
        # the shape and the ink is poured into it.
        with resources.as_file(glyph_path(_GLYPHS[status])) as path:
            QSvgRenderer(str(path)).render(painter)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(canvas.rect(), ink)
        if mark is not None:
            badge = round(size * BADGE)
            gap = max(1, round(size * GAP))
            # The mark drawn larger first, as an eraser, leaves its own outline cut out of
            # the title bar; then the mark itself.
            cut = mark.pixmap(badge + 2 * gap, badge + 2 * gap)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationOut)
            painter.drawPixmap(size - badge - 2 * gap, 0, badge + 2 * gap, badge + 2 * gap, cut)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            painter.drawPixmap(size - badge - gap, gap, badge, badge, mark.pixmap(badge, badge))
    finally:
        painter.end()
    if status not in _BRIGHT:
        dimmed = QPixmap(size, size)
        dimmed.fill(Qt.GlobalColor.transparent)
        painter = QPainter(dimmed)
        painter.setOpacity(0.45)
        painter.drawPixmap(0, 0, canvas)
        painter.end()
        return dimmed
    return canvas


class _Signals(QObject):
    done = pyqtSignal(object)


class _Job(QRunnable):
    """One pass-cli call, off the GUI thread.

    Not an optimisation: a call that hangs on the network would otherwise freeze the icon and
    its menu, and the one thing a lock indicator must never do is stop answering.
    """

    def __init__(self, func) -> None:
        super().__init__()
        self.func = func
        self.signals = _Signals()

    @pyqtSlot()
    def run(self) -> None:
        try:
            result = self.func()
        except Exception as exc:  # noqa: BLE001 - a worker must not take the process with it
            result = exc
        self.signals.done.emit(result)


class Tray:
    def __init__(self, app: QApplication) -> None:
        self.app = app
        self.pool = QThreadPool.globalInstance()
        self.state = state.load()
        self.busy = False
        # Dialogs asking for the code to UNLOCK, and when each began: tick() closes them when
        # the session is unlocked from somewhere else, a terminal or a panel widget.
        self._waiting: dict[QInputDialog, float] = {}
        # Jobs in flight are held here. The thread pool owns the QRunnable but nothing owns
        # its signal object, so without this Python collects it mid-call and the worker dies
        # emitting its own result ("wrapped C/C++ object ... has been deleted").
        self._jobs: set[_Job] = set()

        self.icon = QSystemTrayIcon()
        self.menu = QMenu()
        self.header = QAction("", self.menu)
        self.header.setEnabled(False)
        self.act_unlock = QAction("Unlock…", self.menu)
        self.act_lock = QAction("Lock now", self.menu)
        self.act_check = QAction("Check now", self.menu)
        self.act_setup = QAction("Create a lock…", self.menu)
        self.act_timeout = QAction("Change the lock time…", self.menu)
        self.act_remove = QAction("Remove the lock…", self.menu)
        self.act_quit = QAction("Quit", self.menu)

        self.act_unlock.triggered.connect(self.unlock)
        self.act_lock.triggered.connect(self.lock)
        self.act_check.triggered.connect(self.refresh)
        self.act_setup.triggered.connect(self.setup)
        self.act_timeout.triggered.connect(self.change_timeout)
        self.act_remove.triggered.connect(self.remove)
        self.act_quit.triggered.connect(self.app.quit)

        for item in (
            self.header,
            None,
            self.act_unlock,
            self.act_lock,
            self.act_check,
            None,
            self.act_setup,
            self.act_timeout,
            self.act_remove,
            None,
            self.act_quit,
        ):
            if item is None:
                self.menu.addSeparator()
            else:
                self.menu.addAction(item)

        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._clicked)
        # Opening the menu re-checks. Not a poll: it is a deliberate act by someone who is
        # looking at the thing and expects it to be current, and without it the icon is only
        # ever as fresh as its last start-up - a lock created in a terminal stays invisible.
        self.menu.aboutToShow.connect(self.refresh)

        self.timer = QTimer()
        self.timer.timeout.connect(self.tick)
        self.timer.start(TICK_MS)

        self.server = QLocalServer()
        self._listen()

        self.draw()
        self.icon.show()
        # The one call made without being asked. At startup nothing is known, and an icon that
        # opens on "unknown" and stays there until clicked is not worth having.
        self.refresh()

    # --- the picture ---------------------------------------------------------------------
    def draw(self) -> None:
        status = self.state.status
        self.icon.setIcon(icon_for(status))
        inferred = " (inferred)" if "inferred" in self.state.source else ""
        self.icon.setToolTip(f"Proton Pass: {self.state.describe()}{inferred}")
        self.header.setText(self.state.describe())
        self.act_unlock.setVisible(status in (Lock.LOCKED, Lock.UNKNOWN))
        self.act_lock.setVisible(status is Lock.UNLOCKED)
        self.act_setup.setVisible(status in (Lock.NO_LOCK, Lock.UNKNOWN))
        self.act_timeout.setVisible(status is Lock.UNLOCKED)
        self.act_remove.setVisible(status in (Lock.UNLOCKED, Lock.LOCKED))
        for action in (
            self.act_unlock,
            self.act_lock,
            self.act_check,
            self.act_setup,
            self.act_timeout,
            self.act_remove,
        ):
            action.setEnabled(not self.busy)

    def tick(self) -> None:
        """Local only: pick up what the CLI may have written, and age the countdown."""
        fresh = state.load()
        if (fresh.lock, fresh.since) != (self.state.lock, self.state.since):
            self.state = fresh
        elif self.state.status is Lock.UNLOCKED and self.state.expires_in() == 0:
            self.state = state.record(
                Lock.LOCKED,
                source="idle timeout elapsed (inferred)",
                idle_timeout=self.state.idle_timeout,
            )
        for dialog, began in list(self._waiting.items()):
            if state.unlocked_since(began):
                dialog.reject()
        self.draw()

    # --- the actions ---------------------------------------------------------------------
    def _run(self, func) -> None:
        if self.busy:
            return
        self.busy = True
        self.draw()

        def finished(result) -> None:
            self._jobs.discard(job)
            self.busy = False
            if isinstance(result, Exception):
                self._complain(str(result))
            else:
                self.state = result.state or state.load()
                if result.is_error and result.message:
                    self._complain(result.message)
            self.draw()

        job = _Job(func)
        self._jobs.add(job)
        job.signals.done.connect(finished)
        self.pool.start(job)

    def refresh(self) -> None:
        self._run(actions.refresh)

    def lock(self) -> None:
        self._run(actions.lock)

    def unlock(self) -> None:
        code = self._ask("Proton Pass is locked. Enter your lock code:", until_unlocked=True)
        if code:
            self._run(lambda: actions.unlock(code))

    def setup(self) -> None:
        code = self._ask("Choose a lock code (a PIN is fine):")
        if not code:
            return
        if self._ask("Enter it again:") != code:
            self._complain("The two codes were different; nothing was changed.")
            return
        timeout = self._ask_timeout(600)
        if timeout:
            self._run(lambda: actions.create_lock(code, timeout))

    def change_timeout(self) -> None:
        timeout = self._ask_timeout(self.state.idle_timeout or 600)
        if not timeout or timeout == self.state.idle_timeout:
            return
        code = self._ask("Enter the lock code to change the time:")
        if code:
            self._run(lambda: actions.set_timeout(code, timeout))

    def remove(self) -> None:
        code = self._ask("Enter the lock code to remove the lock:")
        if code:
            self._run(lambda: actions.remove_lock(code))

    # --- asking ---------------------------------------------------------------------------
    def _ask_timeout(self, current: int) -> int | None:
        seconds, ok = QInputDialog.getInt(
            None,
            "Proton Pass",
            "Lock the session after how many seconds unused?",
            current,
            30,
            900,
            30,
        )
        return seconds if ok else None

    def _ask(self, reason: str, *, until_unlocked: bool = False) -> str | None:
        dialog = QInputDialog()
        dialog.setWindowTitle("Proton Pass")
        dialog.setLabelText(reason)
        dialog.setTextEchoMode(QLineEdit.EchoMode.Password)
        if until_unlocked:
            self._waiting[dialog] = time.time()
        try:
            accepted = dialog.exec() == QInputDialog.DialogCode.Accepted
        finally:
            self._waiting.pop(dialog, None)
        return (dialog.textValue() or None) if accepted else None

    def _complain(self, text: str) -> None:
        QMessageBox.warning(None, "Proton Pass", text)

    def _clicked(self, reason) -> None:
        if reason != QSystemTrayIcon.ActivationReason.Trigger:
            return
        if self.state.status is Lock.LOCKED:
            self.unlock()
        else:
            self.menu.popup(self.icon.geometry().bottomLeft())

    # --- the prompt socket ----------------------------------------------------------------
    def _listen(self) -> None:
        path = prompt.socket_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        QLocalServer.removeServer(path)  # one left behind by a tray that was killed
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self.server.newConnection.connect(self._serve)
        if not self.server.listen(path):
            print(
                f"warning: cannot listen on {path}: {self.server.errorString()}",
                file=sys.stderr,
            )

    def _serve(self) -> None:
        conn = self.server.nextPendingConnection()
        if conn is None:
            return
        conn.disconnected.connect(conn.deleteLater)

        def ready() -> None:
            try:
                request = json.loads(bytes(conn.readLine()).decode() or "{}")
            except ValueError:
                request = {}
            reason = str(request.get("ask") or "Proton Pass lock code")
            code = self._ask(f"{reason}:", until_unlocked=request.get("until") == "unlocked")
            # The caller may have gone while the dialog was up, taking the connection with it:
            # disconnected -> deleteLater runs inside the dialog's own event loop.
            with contextlib.suppress(RuntimeError):
                conn.write(json.dumps({"code": code or ""}).encode() + b"\n")
                conn.flush()
                conn.close()

        conn.readyRead.connect(ready)


def run_tray(args: argparse.Namespace) -> int:
    app = QApplication(sys.argv[:1])
    app.setApplicationName("protonpass-tray")
    app.setDesktopFileName("io.github.retienne.protonpass-tray")
    app.setQuitOnLastWindowClosed(False)
    if not QSystemTrayIcon.isSystemTrayAvailable():
        print(
            "this desktop has no system tray.\n"
            "  GNOME removed tray icons: install the AppIndicator extension, or the GNOME "
            "Shell extension from this project.",
            file=sys.stderr,
        )
        return 1
    Tray(app)
    return app.exec()
