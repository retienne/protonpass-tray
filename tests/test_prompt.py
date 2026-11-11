"""Where a code comes from. The order matters more than any single source: a terminal must
win over a dialog, and a cancelled dialog must not fall through to the next one and ask twice.
"""

import os
import unittest
import unittest.mock

from common import Harness

from protonpass_tray.prompt import NoPrompt


class Dialogs(Harness):
    def fake(self, name, *, code="1234", rc=0):
        """A stand-in dialog on PATH, which is all from_dialog knows about one."""
        path = self.runtime / name
        path.write_text(f'#!/bin/sh\nprintf "%s\\n" "{code}"\nexit {rc}\n')
        path.chmod(0o755)
        os.environ["PATH"] = f"{self.runtime}:{os.environ['PATH']}"
        return path

    def test_kdialog_is_used_when_there_is_a_display(self):
        self.fake("kdialog")
        with unittest.mock.patch.dict("os.environ", {"WAYLAND_DISPLAY": "wayland-0"}):
            self.assertEqual(self.prompt.from_dialog("code"), "1234")

    def test_nothing_is_asked_without_a_display(self):
        self.fake("kdialog")
        with unittest.mock.patch.dict("os.environ", clear=False) as env:
            os.environ.pop("WAYLAND_DISPLAY", None)
            os.environ.pop("DISPLAY", None)
            self.assertIsNone(self.prompt.from_dialog("code"))
            del env

    def test_a_cancelled_dialog_does_not_fall_through(self):
        """Cancel means cancel. Falling through would pop a second dialog from another
        toolkit, which reads as the application refusing to take no for an answer."""
        self.fake("kdialog", code="", rc=1)
        self.fake("zenity", code="9999", rc=0)
        with unittest.mock.patch.dict("os.environ", {"WAYLAND_DISPLAY": "wayland-0"}):
            self.assertIsNone(self.prompt.from_dialog("code"))

    def test_a_terminal_wins_over_a_dialog(self):
        self.fake("kdialog", code="from-dialog")
        with (
            unittest.mock.patch.object(self.prompt, "from_tty", return_value="from-tty"),
            unittest.mock.patch.dict("os.environ", {"WAYLAND_DISPLAY": "wayland-0"}),
        ):
            self.assertEqual(self.prompt.ask(), "from-tty")

    def test_nothing_available_is_an_error_not_a_hang(self):
        with (
            unittest.mock.patch.object(self.prompt, "from_tty", return_value=None),
            unittest.mock.patch.object(self.prompt, "from_dialog", return_value=None),
            unittest.mock.patch.object(self.prompt, "from_systemd", return_value=None),
            self.assertRaises(NoPrompt),
        ):
            self.prompt.ask()


if __name__ == "__main__":
    unittest.main()
