"""What a waiting prompt tells the desktop, and that it stops telling when it stops waiting."""

import json
import os
import pty
import select
import signal
import subprocess
import sys
import threading
import time
import unittest
import unittest.mock

from common import ROOT, Harness

from protonpass_tray import asking


class WhatItSays(Harness):
    def records(self):
        return [json.loads(p.read_text()) for p in sorted(asking.directory().glob("*.json"))]

    def test_it_is_there_while_waiting_and_gone_after(self):
        with asking.Asking():
            (record,) = self.records()
            self.assertLess(time.time() - record["alive"], 5)
            self.assertTrue(record["who"])
        self.assertEqual(self.records(), [])

    def test_it_goes_when_the_prompt_is_interrupted(self):
        with self.assertRaises(KeyboardInterrupt), asking.Asking():
            raise KeyboardInterrupt
        self.assertEqual(self.records(), [])

    def test_a_beat_moves_alive_and_nothing_else(self):
        with asking.Asking() as request:
            (before,) = self.records()
            with unittest.mock.patch("time.time", return_value=time.time() + 60):
                request.beat()
            (after,) = self.records()
        self.assertGreater(after["alive"], before["alive"] + 50)
        self.assertEqual((before["id"], before["since"]), (after["id"], after["since"]))

    def test_a_beat_too_soon_writes_nothing(self):
        with asking.Asking() as request:
            request.path.unlink()
            request.beat()
            self.assertEqual(self.records(), [])

    def test_a_file_per_prompt_and_one_line_each(self):
        """A widget reads them with one `cat`."""
        with asking.Asking() as request:
            text = request.path.read_text()
        self.assertTrue(text.endswith("\n"))
        self.assertEqual(text.count("\n"), 1)

    def test_only_its_owner_can_read_it(self):
        with asking.Asking() as request:
            self.assertEqual(request.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(asking.directory().stat().st_mode & 0o777, 0o700)

    def test_what_a_killed_prompt_left_is_taken_away(self):
        with asking.Asking() as request:
            left = request.path.with_name("99999999.json")
            left.write_text("{}\n")
            old = time.time() - asking.STALE_S - 1
            os.utime(left, (old, old))
        with asking.Asking():
            self.assertFalse(left.exists())

    def test_a_directory_that_cannot_be_written_does_not_stop_the_prompt(self):
        (self.runtime / "protonpass-tray").write_text("in the way")
        with asking.Asking():
            pass

    def test_no_markup_reaches_a_notification(self):
        self.assertEqual(asking._clean("<b>x</b>&\x1b[1m"), "bx/b1m")

    def test_a_command_handed_its_code_announces_nothing(self):
        self.set_stub(has_lock=True, locked=True, idle_timeout=600, code="1234")
        with (
            unittest.mock.patch("sys.stdin.readline", return_value="1234\n"),
            unittest.mock.patch.object(asking, "Asking") as announced,
        ):
            self.run_cli("unlock", "--code-stdin")
        announced.assert_not_called()


class WaitingForAFace(Harness):
    """`unlock --wait`: nothing is asked here, and it must never be a hang."""

    def setUp(self):
        super().setUp()
        self.set_stub(has_lock=True, locked=True, idle_timeout=600, code="1234")
        self.state.record(self.state.Lock.LOCKED, source="a test", idle_timeout=600)
        self.folder = asking.directory().parent

    def listen(self):
        (self.folder / "listening").touch()

    def wait(self, seconds="1"):
        began = time.monotonic()
        code = self.run_cli("unlock", "--wait", seconds)
        self.took = time.monotonic() - began
        return code

    def test_with_nobody_to_tell_it_fails_at_once(self):
        self.assertEqual(self.wait("60"), self.cli.EXIT_LOCKED)
        self.assertLess(self.took, 5)
        self.assertIn("no desktop face", self.err)
        self.assertEqual(self.out, "")

    def test_a_face_that_stopped_is_nobody(self):
        self.listen()
        old = time.time() - asking.STALE_S - 1
        os.utime(self.folder / "listening", (old, old))
        self.assertEqual(self.wait("60"), self.cli.EXIT_LOCKED)
        self.assertLess(self.took, 5)

    def test_an_unlock_elsewhere_ends_it(self):
        self.listen()
        timer = threading.Timer(
            1, self.state.record, (self.state.Lock.UNLOCKED,), {"source": "a face"}
        )
        timer.start()
        self.addCleanup(timer.join)
        self.assertEqual(self.wait("60"), 0)
        self.assertLess(self.took, 10)
        self.assertIn("Unlocked from elsewhere.", self.err)
        self.assertEqual(self.stub_calls(), [], "there was nothing to ask pass-cli")

    def test_it_ends_by_itself_and_the_next_one_does_not_wait(self):
        self.listen()
        self.assertEqual(self.wait("1"), self.cli.EXIT_LOCKED)
        self.assertIn("within 1s", self.err)
        self.assertEqual(list(asking.directory().glob("*.json")), [])
        self.assertEqual(self.wait("60"), self.cli.EXIT_LOCKED)
        self.assertLess(self.took, 5)
        self.assertIn("nobody answered", self.err)

    def test_it_asks_again_after_a_while(self):
        self.listen()
        asking.went_unanswered()
        old = time.time() - asking.UNANSWERED_S - 1
        os.utime(self.folder / "unanswered", (old, old))
        self.assertFalse(asking.unanswered())

    def test_it_opens_no_prompt_of_its_own(self):
        self.listen()
        with unittest.mock.patch.object(self.prompt, "ask") as ask:
            self.wait("1")
        ask.assert_not_called()

    def test_a_running_tray_is_asked_instead(self):
        (self.folder / "prompt.sock").touch()
        with unittest.mock.patch.object(self.prompt, "from_tray", return_value="1234"):
            self.assertEqual(self.wait("5"), 0)

    def test_a_refusal_in_the_tray_is_an_answer(self):
        (self.folder / "prompt.sock").touch()
        with unittest.mock.patch.object(self.prompt, "from_tray", return_value=None):
            self.assertEqual(self.wait("60"), self.cli.EXIT_LOCKED)
        self.assertLess(self.took, 5)

    def test_the_limits(self):
        for seconds in ("0", "3601"):
            self.assertEqual(self.wait(seconds), self.cli.EXIT_USAGE)


class WhoAsked(Harness):
    """On a real pty, through a real chain of processes: that chain is what is being read."""

    def start(self, script):
        pid, fd = pty.fork()
        if pid == 0:  # pragma: no cover - the child either execs or dies
            try:
                for name in ("DISPLAY", "WAYLAND_DISPLAY"):
                    os.environ.pop(name, None)
                os.environ["PYTHONPATH"] = str(ROOT / "src")
                os.chdir(self.runtime)
                # `; true` keeps the shell alive instead of exec'ing its last command: it is
                # the session leader, the part a terminal's own shell plays.
                os.execv("/bin/sh", ["sh", "-c", f"{script}; true"])
            finally:
                os._exit(127)
        self.addCleanup(os.close, fd)
        self.addCleanup(os.waitpid, pid, 0)
        self.addCleanup(os.kill, pid, signal.SIGKILL)
        seen = b""
        deadline = time.monotonic() + 20
        while b"lock code: " not in seen:
            self.assertGreater(deadline, time.monotonic(), seen)
            if select.select([fd], [], [], 0.2)[0]:
                seen += os.read(fd, 4096)
        self.fd = fd
        (path,) = asking.directory().glob("*.json")
        return json.loads(path.read_text())["who"]

    def test_by_hand_it_is_the_terminal(self):
        who = self.start(f"{sys.executable} -m protonpass_tray unlock")
        self.assertRegex(who, r"^A terminal \(pts/\d+\)$")

    def test_from_a_recipe_it_is_the_program_that_was_started(self):
        recipe = self.runtime / "deploy"
        recipe.write_text(
            f"#!/bin/sh\nsh -c '{sys.executable} -m protonpass_tray unlock; true' secret=1; true\n"
        )
        recipe.chmod(0o755)
        who = self.start("./deploy --password hunter2")
        self.assertRegex(who, r"^deploy in runtime \(pts/\d+\)$")

    def test_with_no_terminal_it_is_what_started_the_session(self):
        out = subprocess.run(
            [sys.executable, "-c", "from protonpass_tray import asking; print(asking.describe())"],
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            start_new_session=True,
            check=True,
        ).stdout.strip()
        ours = asking._clean(asking._stat(os.getpid())[0])
        self.assertEqual(out, f"A program ({ours})")


if __name__ == "__main__":
    unittest.main()
