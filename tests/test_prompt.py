"""Where a code comes from. The order matters more than any single source: a terminal must
win over a dialog, and a cancelled dialog must not fall through to the next one and ask twice.
"""

import os
import pty
import select
import sys
import termios
import time
import unittest
import unittest.mock

from common import ROOT, Harness

from protonpass_tray.prompt import NoPrompt, Settled


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

    def test_a_dialog_closes_when_it_has_nothing_left_to_ask(self):
        path = self.fake("kdialog")
        path.write_text("#!/bin/sh\nexec sleep 30\n")
        began = time.monotonic()
        with (
            unittest.mock.patch.dict("os.environ", {"WAYLAND_DISPLAY": "wayland-0"}),
            self.assertRaises(Settled),
        ):
            self.prompt.from_dialog("code", done=lambda: True)
        self.assertLess(time.monotonic() - began, 10, "the dialog was left running")

    def test_a_source_that_gave_up_does_not_hand_over_to_the_next(self):
        """A tray that closed its own dialog answers with no code, which on its own reads as
        "no tray" - and the terminal would then ask for what has just been done."""
        with (
            unittest.mock.patch.object(self.prompt, "from_tray", return_value=None),
            unittest.mock.patch.object(self.prompt, "from_tty") as tty,
            self.assertRaises(Settled),
        ):
            self.prompt.ask(done=lambda: True)
        tty.assert_not_called()

    def test_nothing_available_is_an_error_not_a_hang(self):
        with (
            unittest.mock.patch.object(self.prompt, "from_tty", return_value=None),
            unittest.mock.patch.object(self.prompt, "from_dialog", return_value=None),
            unittest.mock.patch.object(self.prompt, "from_systemd", return_value=None),
            self.assertRaises(NoPrompt),
        ):
            self.prompt.ask()


class UnlockedFromElsewhere(Harness):
    """A terminal left waiting at `protonpass-tray unlock` while the code is typed into a
    panel (seen 2026-11-25). On a real pty, because what is tested is a terminal's state."""

    def setUp(self):
        super().setUp()
        self.set_stub(has_lock=True, locked=True, idle_timeout=600, code="1234")
        self.state.record(self.state.Lock.LOCKED, source="a test", idle_timeout=600)
        self.seen = b""

    def start(self):
        pid, fd = pty.fork()
        if pid == 0:  # pragma: no cover - the child either execs or dies
            try:
                for name in ("DISPLAY", "WAYLAND_DISPLAY"):
                    os.environ.pop(name, None)
                os.environ["PYTHONPATH"] = str(ROOT / "src")
                os.execv(sys.executable, [sys.executable, "-m", "protonpass_tray", "unlock"])
            finally:
                os._exit(127)
        self.pid, self.fd = pid, fd
        self.addCleanup(os.close, fd)
        self.read_until(b"lock code: ")

    def read_until(self, text, *, timeout=20):
        deadline = time.monotonic() + timeout
        while text not in self.seen:
            self.assertGreater(deadline, time.monotonic(), f"never said {text!r}: {self.seen!r}")
            if select.select([self.fd], [], [], 0.2)[0]:
                self.seen += os.read(self.fd, 4096)

    def finish(self, *, timeout=20):
        deadline = time.monotonic() + timeout
        while True:
            reaped, status = os.waitpid(self.pid, os.WNOHANG)
            if reaped:
                return os.waitstatus_to_exitcode(status)
            if time.monotonic() > deadline:
                os.kill(self.pid, 9)
                os.waitpid(self.pid, 0)
                self.fail(f"still waiting: {self.seen!r}")
            time.sleep(0.05)

    def unlock_elsewhere(self):
        self.state.record(self.state.Lock.UNLOCKED, source="another face")

    def test_the_prompt_ends_and_the_command_succeeds(self):
        self.start()
        self.unlock_elsewhere()
        self.read_until(b"Unlocked from elsewhere.")
        self.assertEqual(self.finish(), 0)
        self.assertEqual(self.stub_calls(), [], "there was nothing left to ask pass-cli")

    def test_the_terminal_gets_its_echo_back_and_not_half_a_code(self):
        self.start()
        self.assertFalse(termios.tcgetattr(self.fd)[3] & termios.ECHO)
        os.write(self.fd, b"12")
        self.unlock_elsewhere()
        self.read_until(b"Unlocked from elsewhere.")
        self.assertTrue(termios.tcgetattr(self.fd)[3] & termios.ECHO)
        self.assertNotIn(b"12\r\n", self.seen)
        self.assertEqual(self.finish(), 0)

    def test_an_older_unlocked_in_the_cache_does_not_end_it(self):
        """What an idle lock leaves behind: the server locked, the file still says unlocked."""
        self.unlock_elsewhere()
        self.start()
        time.sleep(1.5)
        self.assertEqual(os.waitpid(self.pid, os.WNOHANG)[0], 0, self.seen)
        os.write(self.fd, b"1234\n")
        self.assertEqual(self.finish(), 0)
        self.assertNotIn(b"1234", self.seen)
        self.assertTrue(any("unlock" in call for call in self.stub_calls()))

    def test_a_lock_elsewhere_does_not_end_it(self):
        self.start()
        self.state.record(self.state.Lock.LOCKED, source="another face")
        time.sleep(1.5)
        self.assertEqual(os.waitpid(self.pid, os.WNOHANG)[0], 0, self.seen)
        os.write(self.fd, b"1234\n")
        self.assertEqual(self.finish(), 0)


if __name__ == "__main__":
    unittest.main()
