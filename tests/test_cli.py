"""The command line, exit codes included - they are what the shell hooks branch on.

The first test is the one that matters most. `status` must cost nothing: the lock is an idle
timer, so a status check that asked the API would reset it, and a tray polling for its icon
would keep the session unlocked for ever while displaying "unlocked" quite correctly.
"""

import io
import unittest
import unittest.mock

from common import Harness

from protonpass_tray import cli
from protonpass_tray.state import Lock


class StatusIsFree(Harness):
    def test_status_asks_pass_cli_nothing(self):
        self.set_stub(has_lock=True, locked=False, idle_timeout=600)
        self.run_cli("status")
        self.assertEqual(self.stub_calls(), [])

    def test_refresh_is_the_only_thing_that_asks(self):
        self.set_stub(has_lock=True, locked=False, idle_timeout=600)
        self.run_cli("status", "--refresh")
        self.assertEqual(self.stub_calls(), ["info", "vault list"])

    def test_refresh_sees_a_locked_session(self):
        self.set_stub(has_lock=True, locked=True, idle_timeout=600)
        self.assertEqual(self.run_cli("status", "--refresh"), cli.EXIT_LOCKED)
        self.assertIs(self.state.load().status, Lock.LOCKED)

    def test_refresh_sees_an_unlocked_session_and_learns_the_timeout(self):
        self.set_stub(has_lock=True, locked=False, idle_timeout=600)
        self.assertEqual(self.run_cli("status", "--refresh"), cli.EXIT_OK)
        self.assertEqual(self.state.load().idle_timeout, 600)

    def test_refresh_probes_even_when_info_says_there_is_no_lock(self):
        """This asserted the opposite until 2026-11-11 - that the probe was skipped as an
        optimisation - and so held the bug in place: a session can answer `info` with "has
        lock: no" and every other call with "Session is locked", and the saving was one read
        of a list of vault names."""
        self.set_stub(has_lock=False)
        self.assertEqual(self.run_cli("status", "--refresh"), cli.EXIT_NO_LOCK)
        self.assertEqual(self.stub_calls(), ["info", "vault list"])

    def test_no_session(self):
        self.set_stub()
        with unittest.mock.patch.dict("os.environ", {"STUB_NO_SESSION": "1"}):
            self.assertEqual(self.run_cli("status", "--refresh"), cli.EXIT_NO_SESSION)


class LockAndUnlock(Harness):
    def setUp(self):
        super().setUp()
        patcher = unittest.mock.patch.object(self.prompt, "ask", return_value="1234")
        self.ask = patcher.start()
        self.addCleanup(patcher.stop)

    def test_setup_then_lock_then_unlock(self):
        self.set_stub(has_lock=False)
        self.assertEqual(self.run_cli("setup", "--idle-timeout", "600"), cli.EXIT_OK)
        self.assertEqual(self.state.load().idle_timeout, 600)

        self.assertEqual(self.run_cli("lock"), cli.EXIT_OK)
        self.assertIs(self.state.load().status, Lock.LOCKED)

        self.assertEqual(self.run_cli("unlock"), cli.EXIT_OK)
        self.assertIs(self.state.load().status, Lock.UNLOCKED)

    def test_a_wrong_code_leaves_it_locked(self):
        self.set_stub(has_lock=True, locked=True, code="1234")
        self.ask.return_value = "9999"
        self.assertEqual(self.run_cli("unlock"), cli.EXIT_LOCKED)

    def test_setup_refuses_a_timeout_pass_cli_would_reject(self):
        self.set_stub(has_lock=False)
        self.assertEqual(self.run_cli("setup", "--idle-timeout", "10"), cli.EXIT_USAGE)
        self.assertEqual(self.stub_calls(), [], "it must not even try")

    def test_setup_on_a_session_that_already_has_one(self):
        self.set_stub(has_lock=True, idle_timeout=300)
        self.assertEqual(self.run_cli("setup"), cli.EXIT_USAGE)

    def test_lock_without_a_lock_points_at_setup(self):
        self.set_stub(has_lock=False)
        self.assertEqual(self.run_cli("lock"), cli.EXIT_NO_LOCK)

    def test_unlock_when_it_was_not_locked_is_success(self):
        self.set_stub(has_lock=True, locked=False, code="1234")
        self.assertEqual(self.run_cli("unlock"), cli.EXIT_OK)
        self.assertIs(self.state.load().status, Lock.UNLOCKED)

    def test_remove_lock(self):
        self.set_stub(has_lock=True, locked=False, code="1234")
        self.assertEqual(self.run_cli("remove-lock"), cli.EXIT_OK)
        self.assertIs(self.state.load().status, Lock.NO_LOCK)

    def test_the_code_never_reaches_argv(self):
        """What `ps` would have seen. The stub logs its own argv, so this is that, exactly."""
        self.set_stub(has_lock=True, locked=True, code="1234")
        self.run_cli("unlock")
        self.assertTrue(self.stub_calls())
        for call in self.stub_calls():
            self.assertNotIn("1234", call)


class MissingPassCli(Harness):
    def test_a_missing_binary_is_its_own_exit_code(self):
        with unittest.mock.patch.dict(
            "os.environ", {"PROTONPASS_TRAY_PASS_CLI": "/nonexistent/pass-cli"}
        ):
            self.assertEqual(self.run_cli("status", "--refresh"), cli.EXIT_NO_PASS_CLI)


class WhatItSays(Harness):
    """The words, not just the codes: a hook prints them and a person reads them."""

    def test_locked_says_a_code_is_needed(self):
        self.set_stub(has_lock=True, locked=True, idle_timeout=600)
        self.run_cli("status", "--refresh")
        self.assertIn("code", self.out.lower())

    def test_no_session_names_the_fix(self):
        self.set_stub(has_lock=False)
        with unittest.mock.patch.dict("os.environ", {"STUB_NO_SESSION": "1"}):
            self.run_cli("lock")
        self.assertIn("pass-cli login", self.err)

    def test_no_lock_names_setup(self):
        self.set_stub(has_lock=False)
        self.run_cli("lock")
        self.assertIn("setup", self.err)

    def test_json_is_json(self):
        import json as _json

        self.set_stub(has_lock=True, locked=False, idle_timeout=600)
        self.run_cli("status", "--refresh", "--json")
        payload = _json.loads(self.out)
        self.assertEqual(payload["lock"], "unlocked")
        self.assertEqual(payload["idle_timeout"], 600)
        self.assertIsInstance(payload["expires_in"], int)


class CodeFromAFile(Harness):
    """The path a Plasma widget has to take: it can run a command line and nothing else, so
    the code arrives in a file instead of in argv."""

    def _code_file(self, code="1234", mode=0o600):
        path = self.runtime / "code"
        path.write_text(code + "\n")
        path.chmod(mode)
        return path

    def test_unlock_reads_the_file_and_deletes_it(self):
        self.set_stub(has_lock=True, locked=True, code="1234")
        path = self._code_file()
        self.assertEqual(self.run_cli("unlock", "--code-file", str(path)), cli.EXIT_OK)
        self.assertFalse(path.exists(), "a one-shot code file must not survive its use")

    def test_a_world_readable_code_file_is_refused(self):
        self.set_stub(has_lock=True, locked=True, code="1234")
        path = self._code_file(mode=0o644)
        self.assertEqual(self.run_cli("unlock", "--code-file", str(path)), cli.EXIT_USAGE)
        self.assertIn("readable by others", self.err)
        self.assertEqual(self.stub_calls(), [], "it must not try with a leaked code")

    def test_the_file_is_deleted_even_when_the_unlock_fails(self):
        self.set_stub(has_lock=True, locked=True, code="1234")
        path = self._code_file(code="9999")
        self.assertEqual(self.run_cli("unlock", "--code-file", str(path)), cli.EXIT_LOCKED)
        self.assertFalse(path.exists())

    def test_a_missing_file_is_a_usage_error(self):
        self.set_stub(has_lock=True, locked=True)
        code = self.run_cli("unlock", "--code-file", str(self.runtime / "nope"))
        self.assertEqual(code, cli.EXIT_USAGE)

    def test_stdin(self):
        self.set_stub(has_lock=True, locked=True, code="1234")
        with unittest.mock.patch("sys.stdin", io.StringIO("1234\n")):
            self.assertEqual(self.run_cli("unlock", "--code-stdin"), cli.EXIT_OK)

    def test_the_two_sources_are_mutually_exclusive(self):
        self.set_stub(has_lock=True, locked=True)
        with self.assertRaises(SystemExit):
            self.run_cli("unlock", "--code-stdin", "--code-file", "/x")


class SetupDoesNotGuess(Harness):
    """`create-lock` and `lock` share a success sentence, so what the session is in AFTER a
    setup cannot be read from the output - it has to be asked."""

    def setUp(self):
        super().setUp()
        patcher = unittest.mock.patch.object(self.prompt, "ask", return_value="1234")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_setup_asks_rather_than_assuming(self):
        self.set_stub(has_lock=False)
        self.assertEqual(self.run_cli("setup", "--idle-timeout", "600"), cli.EXIT_OK)
        self.assertIn("session create-lock --idle-timeout 600", self.stub_calls())
        self.assertIn("info", self.stub_calls(), "it must establish the state afterwards")
        self.assertEqual(self.state.load().idle_timeout, 600)


class AFailureRepairsTheState(Harness):
    """The bug that outlasted three fixes, and the reason it did.

    An operation whose outcome could not be read left the cached state exactly as it was. The
    Plasma widget reads that cache to decide what to offer, so it went on showing an Unlock
    field - and a red copy of pass-cli's prompt - for a session whose lock had removed itself
    hours earlier. Nothing the operator could type would ever work, and nothing said why.

    So a failed operation now establishes the state for real before answering. That is one
    extra API call on a path that has already failed, and it is the difference between a face
    that recovers by itself and one that has to be restarted.
    """

    def setUp(self):
        super().setUp()
        patcher = unittest.mock.patch.object(self.prompt, "ask", return_value="1234")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_an_unreadable_unlock_finds_out_there_is_no_lock(self):
        self.set_stub(has_lock=False)
        with unittest.mock.patch.dict("os.environ", {"STUB_UNREADABLE": "1"}):
            code = self.run_cli("unlock")
        self.assertEqual(code, cli.EXIT_NO_LOCK)
        self.assertIs(self.state.load().status, Lock.NO_LOCK)

    def test_it_never_shows_the_prompt_as_the_reason(self):
        self.set_stub(has_lock=False)
        with unittest.mock.patch.dict("os.environ", {"STUB_UNREADABLE": "1"}):
            self.run_cli("unlock")
        self.assertNotIn("lock code", self.err.lower())

    def test_a_wrong_code_on_a_real_lock_still_says_so(self):
        """The reconcile must not turn every refusal into "no lock": a session that IS locked
        and got the wrong code has to keep saying the code was wrong."""
        self.set_stub(has_lock=True, locked=True, code="1234")
        with unittest.mock.patch.object(self.prompt, "ask", return_value="9999"):
            self.assertEqual(self.run_cli("unlock"), cli.EXIT_LOCKED)
        self.assertIs(self.state.load().status, Lock.LOCKED)


class WhatAFailedSetupSays(Harness):
    """The 21:31 line in the operator's own failures.log on 2026-11-11:

        21:31:32  run: setup
        21:31:33  exit=11 No lock configured

    A create-lock had failed in a second, and the engine answered by describing the state -
    which was "no lock", the very thing the operator had asked it to change. True, useless,
    and it sent the widget back to the button that had just failed. pass-cli HAD said
    something; the fix that repaired the cache was throwing it away.
    """

    def setUp(self):
        super().setUp()
        patcher = unittest.mock.patch.object(self.prompt, "ask", return_value="1234")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_pass_clis_own_words_reach_the_operator(self):
        self.set_stub(has_lock=False)
        with unittest.mock.patch.dict("os.environ", {"STUB_FAIL": "Error locking session"}):
            code = self.run_cli("setup", "--idle-timeout", "300")
        self.assertNotEqual(code, cli.EXIT_OK)
        self.assertIn("error locking session", self.err.lower())

    def test_a_failed_setup_is_not_reported_as_the_state_it_failed_to_change(self):
        self.set_stub(has_lock=False)
        with unittest.mock.patch.dict("os.environ", {"STUB_FAIL": "Error locking session"}):
            code = self.run_cli("setup", "--idle-timeout", "300")
        # Not EXIT_NO_LOCK: that is the code meaning "no lock is configured, run setup", and a
        # face branching on it offers setup again - which is where this came in.
        self.assertEqual(code, cli.EXIT_UNRECOGNISED)
        self.assertNotIn("no lock configured", self.err.lower())

    def test_an_unreadable_setup_still_does_not_claim_success(self):
        self.set_stub(has_lock=False)
        with unittest.mock.patch.dict("os.environ", {"STUB_UNREADABLE": "1"}):
            code = self.run_cli("setup", "--idle-timeout", "300")
        self.assertEqual(code, cli.EXIT_UNRECOGNISED)
        self.assertNotIn("lock code", self.err.lower())

    def test_a_setup_that_fails_because_the_session_went_away_says_THAT(self):
        """The state is worth reporting when it is not the one the operation set out to
        leave: "log in again" is actionable, "no lock configured" is not."""
        self.set_stub(has_lock=False)
        with unittest.mock.patch.dict("os.environ", {"STUB_NO_SESSION": "1"}):
            code = self.run_cli("setup", "--idle-timeout", "300")
        self.assertEqual(code, cli.EXIT_NO_SESSION)


class InfoCanContradictTheSession(Harness):
    """The state the operator was actually in on 2026-11-11, found at 22:08.

    `pass-cli info` answered "Session has lock: no" while `session create-lock` refused with
    "Session is locked. Please unlock your session and try again." Believing `info` published
    "No lock configured" for hours, and every face offered to create one - which a locked
    session also refuses. The widget could not get out of it by itself and neither could the
    operator, because the only button on screen was the one that could not work.

    So the behaviour outranks the reading: if an API call says locked, the session is locked,
    whatever `info` claims about a lock being configured.
    """

    def setUp(self):
        super().setUp()
        patcher = unittest.mock.patch.object(self.prompt, "ask", return_value="1234")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_session_is_reported_locked_not_lockless(self):
        self.set_stub(has_lock=False, locked=True)
        self.assertEqual(self.run_cli("status", "--refresh"), cli.EXIT_LOCKED)
        self.assertIs(self.state.load().status, Lock.LOCKED)

    def test_the_cache_records_the_contradiction(self):
        """A cache saying "locked" over an `info` that denies the lock is the most useful
        thing to find here next time; it would otherwise be invisible."""
        self.set_stub(has_lock=False, locked=True)
        self.run_cli("status", "--refresh")
        self.assertIn("info said no lock", self.state.load().source)

    def test_a_setup_in_that_state_says_the_session_is_locked(self):
        self.set_stub(has_lock=False, locked=True)
        code = self.run_cli("setup", "--idle-timeout", "300")
        self.assertIn("session is locked", self.err.lower())
        # EXIT_LOCKED, so a face offers the code field rather than the create form: it is the
        # only one of the two that can do anything here.
        self.assertEqual(code, cli.EXIT_LOCKED)

    def test_an_ordinary_lockless_session_is_still_lockless(self):
        """The probe must not turn "no lock" into "locked" for everyone else."""
        self.set_stub(has_lock=False, locked=False)
        self.assertEqual(self.run_cli("status", "--refresh"), cli.EXIT_NO_LOCK)
        self.assertIs(self.state.load().status, Lock.NO_LOCK)


class AnUnlockLeavesACountdown(Harness):
    """What the operator's cache held at 23:17 on 2026-11-11, right after the unlock that
    proved the lock had been there all along: `unlocked`, and `idle_timeout: 0`.

    Zero is not a small countdown, it is NO countdown - `expires_in()` returns None, so the
    tray's `expires_in() == 0` inference never fires and the icon reads "unlocked" until
    something else refreshes it. The cache had lost the number during the hours it wrongly
    believed there was no lock, and `record` faithfully carried the zero forward.
    """

    def setUp(self):
        super().setUp()
        patcher = unittest.mock.patch.object(self.prompt, "ask", return_value="1234")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_timeout_is_learned_when_the_cache_has_none(self):
        self.set_stub(has_lock=True, locked=True, idle_timeout=300, code="1234")
        self.state.record(self.state.Lock.NO_LOCK, source="stale", idle_timeout=0)
        self.assertEqual(self.run_cli("unlock"), cli.EXIT_OK)
        st = self.state.load()
        self.assertEqual(st.idle_timeout, 300)
        self.assertIsNotNone(st.expires_in())

    def test_a_cache_that_knows_the_timeout_is_not_asked_again(self):
        """The extra read happens only when the number is missing."""
        self.set_stub(has_lock=True, locked=True, idle_timeout=300, code="1234")
        self.state.record(self.state.Lock.LOCKED, source="stale", idle_timeout=600)
        self.run_cli("unlock")
        self.assertEqual(self.state.load().idle_timeout, 600)
        self.assertNotIn("info", self.stub_calls())

    def test_an_already_unlocked_session_learns_it_too(self):
        """The "was not locked" path wrote the same zero."""
        self.set_stub(has_lock=True, locked=False, idle_timeout=300, code="1234")
        self.state.record(self.state.Lock.NO_LOCK, source="stale", idle_timeout=0)
        self.assertEqual(self.run_cli("unlock"), cli.EXIT_OK)
        self.assertEqual(self.state.load().idle_timeout, 300)


class TheClientsViewGoesStale(Harness):
    """`pass-cli session unlock` can answer "Session is not locked" on a locked session.

    Seen 2026-11-11, minutes after a successful unlock: the server auto-locked on idleness,
    the client did not find out, and so `unlock` refused to even try while `vault list` was
    being refused with "Session is locked". jimi-ansible's read_secret.sh - which probes with
    `vault list` - was right, and pass-cli's own `unlock` and `info` were both wrong.

    Taking "not locked" at its word recorded UNLOCKED over a session that could not read a
    single secret, which is a green icon on a dead session. The probe decides instead.
    """

    def setUp(self):
        super().setUp()
        patcher = unittest.mock.patch.object(self.prompt, "ask", return_value="1234")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_stale_not_locked_does_not_become_unlocked(self):
        self.set_stub(has_lock=True, locked=True, idle_timeout=300, code="1234")
        with unittest.mock.patch.dict("os.environ", {"STUB_STALE_CLIENT": "1"}):
            code = self.run_cli("unlock")
        self.assertEqual(code, cli.EXIT_LOCKED)
        self.assertIs(self.state.load().status, Lock.LOCKED)

    def test_a_genuine_not_locked_is_still_a_success(self):
        """The ordinary case: nothing was locked, and saying so is right."""
        self.set_stub(has_lock=True, locked=False, idle_timeout=300, code="1234")
        self.assertEqual(self.run_cli("unlock"), cli.EXIT_OK)
        self.assertIs(self.state.load().status, Lock.UNLOCKED)


if __name__ == "__main__":
    unittest.main()
