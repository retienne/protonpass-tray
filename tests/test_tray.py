"""The tray, driven headless. Skipped where PyQt6 is not installed, so the suite still runs
in a build root with no Qt - but not silently: a skip says which import was missing.

What is worth testing without a screen is the logic a person cannot see: which icon and which
menu entries each state produces, and that the countdown reaching zero locks the icon WITHOUT
a call to pass-cli. That last one is the design rule this project is built on.
"""

import os
import time
import unittest
import unittest.mock

from common import Harness

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
        widget.state = self.state.load()
        return widget

    def test_every_state_has_an_icon(self):
        for status in self.state.Lock:
            self.assertFalse(tray.icon_for(status).isNull(), status)

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
            widget = tray.Tray(self.app)
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
        for name in ("act_unlock", "act_lock", "act_check", "act_setup", "act_remove"):
            setattr(widget, name, tray.QAction(""))
        widget.tick()
        self.assertIs(widget.state.status, self.state.Lock.LOCKED)
        self.assertIn("inferred", widget.state.source)
        self.assertEqual(len(self.stub_calls()), before, "tick() must make no API call")


if __name__ == "__main__":
    unittest.main()
