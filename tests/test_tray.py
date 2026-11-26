"""The tray, driven headless. Skipped where PyQt6 is not installed, so the suite still runs
in a build root with no Qt - but not silently: a skip says which import was missing.

What is worth testing without a screen is the logic a person cannot see: which icon and which
menu entries each state produces, and that the countdown reaching zero locks the icon WITHOUT
a call to pass-cli. That last one is the design rule this project is built on.
"""

import os
import subprocess
import sys
import time
import unittest
import unittest.mock

from common import ROOT, Harness

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtWidgets import QApplication

    from protonpass_tray import tray

    QT = None
except ImportError as exc:  # pragma: no cover
    QT = str(exc)


@unittest.skipIf(QT is not None, f"PyQt6 is not installed: {QT}")
class TrayLogic(Harness):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make(self):
        """A Tray with its one startup call already made, so a test sees a known state."""
        widget = tray.Tray.__new__(tray.Tray)
        widget.app = self.app
        widget.busy = False
        widget._waiting = {}
        widget.state = self.state.load()
        return widget

    def test_every_state_has_an_icon(self):
        for status in self.state.Lock:
            self.assertFalse(tray.icon_for(status).isNull(), status)

    def test_the_proton_mark_is_a_badge_on_our_own_glyph(self):
        """Until 2026-11-26 the icon WAS the mark, and an unlocked session sat in the panel
        looking exactly like the Proton Pass application. Now: our glyph in the panel's ink,
        the mark in the top-right corner, and a gap cut between them."""
        from PyQt6.QtGui import QColor, QIcon, QPixmap

        solid = QPixmap(64, 64)
        solid.fill(QColor("#ff0000"))
        mark, ink, size = QIcon(solid), QColor("#00ff00"), 44
        image = tray._compose(self.state.Lock.LOCKED, size, ink, mark).toImage()
        badge, gap = round(size * tray.BADGE), max(1, round(size * tray.GAP))

        def at(x, y):
            return image.pixelColor(x, y)

        self.assertEqual(at(size - gap - 1, gap).name(), "#ff0000", "the mark, top right")
        self.assertEqual(at(size - badge - gap - 1, 2).alpha(), 0, "the gap beside it")
        self.assertEqual(at(2, size // 2).name(), "#00ff00", "the glyph's frame, in the ink")
        colours = {
            at(x, y).name() for x in range(size) for y in range(size) if at(x, y).alpha() == 255
        }
        self.assertIn("#00ff00", colours)
        # Nothing of the mark outside its corner: it is a badge, not a background.
        for x in range(size - badge - gap):
            for y in range(size):
                self.assertNotEqual(at(x, y).name(), "#ff0000", (x, y))

    def test_without_proton_pass_it_is_the_glyph_alone(self):
        from PyQt6.QtGui import QColor

        image = tray._compose(self.state.Lock.LOCKED, 44, QColor("#00ff00"), None).toImage()
        corner = image.pixelColor(44 - 8, 9)  # inside the title bar, where the badge would be
        self.assertEqual(corner.name(), "#00ff00", "the title bar, not cut")

    def test_the_menu_follows_the_state(self):
        self.set_stub(has_lock=True, locked=True, idle_timeout=600)
        self.state.record(self.state.Lock.LOCKED, source="a test", idle_timeout=600)
        # The startup refresh is stubbed out rather than merely waited for. It runs on the
        # thread pool, and on 2026-11-11 a worker outlived the fixture: by the time it wrote,
        # setUp's environment had been restored, so it resolved $XDG_RUNTIME_DIR and
        # PROTONPASS_TRAY_PASS_CLI to the REAL ones - writing into the live cache and calling
        # the real pass-cli. A test must not be able to reach the machine it runs on, and
        # waiting for the pool only makes that unlikely rather than impossible.
        with unittest.mock.patch.object(tray.actions, "refresh") as refreshed:
            # A real Outcome: it is delivered whenever something next runs Qt's loop, which is
            # another test, and a MagicMock for a message ends the process there.
            refreshed.return_value = tray.actions.Outcome(10, state=self.state.load())
            widget = tray.Tray(self.app)
            self.addCleanup(widget.timer.stop)
            self.addCleanup(widget.server.close)
            self.addCleanup(lambda: widget.pool.waitForDone(5000))
            self.addCleanup(widget.icon.hide)
            widget.pool.waitForDone(5000)
        self.assertTrue(refreshed.called, "the tray must establish the state at start-up")
        self.assertTrue(widget.act_unlock.isVisible())
        self.assertFalse(widget.act_lock.isVisible())

    def test_the_countdown_locks_the_icon_without_asking(self):
        self.set_stub(has_lock=True, locked=False, idle_timeout=60)
        self.state.save(
            self.state.State(
                lock="unlocked", since=time.time() - 120, idle_timeout=60, source="a test"
            )
        )
        widget = self.make()
        before = len(self.stub_calls())
        widget.icon = tray.QSystemTrayIcon()
        widget.header = tray.QAction("")
        for name in (
            "act_unlock",
            "act_lock",
            "act_check",
            "act_setup",
            "act_timeout",
            "act_remove",
        ):
            setattr(widget, name, tray.QAction(""))
        widget.tick()
        self.assertIs(widget.state.status, self.state.Lock.LOCKED)
        self.assertIn("inferred", widget.state.source)
        self.assertEqual(len(self.stub_calls()), before, "tick() must make no API call")

    def test_a_dialog_closes_when_the_session_is_unlocked_elsewhere(self):
        self.state.record(self.state.Lock.LOCKED, source="a test", idle_timeout=600)
        widget = self.make()
        widget.draw = lambda: None
        timer = tray.QTimer()
        timer.timeout.connect(widget.tick)
        timer.start(50)
        tray.QTimer.singleShot(
            300, lambda: self.state.record(self.state.Lock.UNLOCKED, source="a terminal")
        )
        # A dialog tick() failed to close would hang the suite, so it is closed - as ACCEPTED,
        # with text in it, which is what tells the two apart below.
        giving_up = tray.QTimer()
        giving_up.setSingleShot(True)
        giving_up.timeout.connect(lambda: [self.give_up(d) for d in list(widget._waiting)])
        giving_up.start(5000)
        self.addCleanup(timer.stop)
        self.addCleanup(giving_up.stop)
        self.assertIsNone(widget._ask("code", until_unlocked=True))
        self.assertEqual(widget._waiting, {})

    def test_the_socket_lets_both_ends_go_when_unlocked_elsewhere(self):
        """The whole road: a caller with no terminal, the tray's dialog, and neither left
        waiting. The caller goes first, so the tray answers a connection that is gone - and
        PyQt ends the process on an exception in a slot, which would take the icon with it."""
        self.state.record(self.state.Lock.LOCKED, source="a test", idle_timeout=600)
        widget = self.make()
        widget.draw = lambda: None
        widget.server = tray.QLocalServer()
        widget._listen()
        self.addCleanup(widget.server.close)
        ticking = tray.QTimer()
        ticking.timeout.connect(widget.tick)
        ticking.start(2000)
        self.addCleanup(ticking.stop)
        caller = subprocess.Popen(
            [sys.executable, "-m", "protonpass_tray", "unlock"],
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
            stdin=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
        )
        self.addCleanup(caller.stderr.close)
        self.addCleanup(caller.kill)
        # From a timer, because the dialog runs an event loop of its own inside this one: a
        # line after processEvents() is not reached until the dialog has already closed.
        opened = []
        deadline = time.monotonic() + 15

        def look():
            if time.monotonic() > deadline:
                for dialog in list(widget._waiting):
                    self.give_up(dialog)
            elif widget._waiting and not opened:
                opened.append(True)
                self.state.record(self.state.Lock.UNLOCKED, source="a terminal")

        watch = tray.QTimer()
        watch.timeout.connect(look)
        watch.start(20)
        self.addCleanup(watch.stop)
        while caller.poll() is None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.app.processEvents()
        self.assertTrue(opened, "the dialog never opened")
        self.assertLess(time.monotonic(), deadline, "something was still waiting")
        self.assertEqual(caller.returncode, 0)
        self.assertIn(b"Unlocked from elsewhere", caller.stderr.read())
        self.assertEqual(widget._waiting, {})

    @staticmethod
    def give_up(dialog):
        dialog.setTextValue("never closed")
        dialog.accept()


if __name__ == "__main__":
    unittest.main()
