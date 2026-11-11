"""The seam: that each of pass-cli's messages is read as the right thing, and that a code
reaches a command that insists on a terminal."""

import unittest
import unittest.mock

from common import Harness

from protonpass_tray.passcli import Verdict


class Classification(Harness):
    def test_locked_session_is_recognised(self):
        self.set_stub(has_lock=True, locked=True, idle_timeout=600)
        self.assertIs(self.passcli.run(["vault", "list"]).verdict, Verdict.LOCKED)

    def test_unlocked_session_reads_clean(self):
        self.set_stub(has_lock=True, locked=False, idle_timeout=600)
        result = self.passcli.run(["vault", "list"])
        self.assertEqual(result.returncode, 0)
        self.assertIsNot(result.verdict, Verdict.LOCKED)

    def test_lock_without_a_lock_says_so(self):
        self.set_stub(has_lock=False)
        self.assertIs(self.passcli.run(["session", "lock"]).verdict, Verdict.NO_LOCK)

    def test_no_session(self):
        self.set_stub()
        with unittest.mock.patch.dict("os.environ", {"STUB_NO_SESSION": "1"}):
            self.assertIs(self.passcli.run(["info"]).verdict, Verdict.NO_SESSION)

    def test_info_reports_the_lock_and_its_timeout(self):
        self.set_stub(has_lock=True, idle_timeout=600)
        details = self.passcli.info()
        self.assertTrue(details.has_lock)
        self.assertEqual(details.idle_timeout, 600)
        self.assertEqual(details.account, "someone@proton.me")

    def test_info_without_a_lock(self):
        self.set_stub(has_lock=False)
        details = self.passcli.info()
        self.assertFalse(details.has_lock)
        self.assertEqual(details.idle_timeout, 0)

    def test_an_item_command_never_returns_its_output(self):
        # The message a caller may show is one LINE of pass-cli's own text; nothing in Result
        # can carry item content, which is what keeps a secret out of a log by construction.
        self.set_stub(has_lock=True, locked=True)
        result = self.passcli.run(["vault", "list"])
        self.assertNotIn("\n", result.message)


class FeedingTheCode(Harness):
    def test_over_a_pipe(self):
        self.set_stub(has_lock=True, locked=True, code="1234")
        result = self.passcli.feed_code(["session", "unlock"], "1234")
        self.assertIs(result.verdict, Verdict.OK)

    def test_a_wrong_code_is_not_a_success(self):
        self.set_stub(has_lock=True, locked=True, code="1234")
        result = self.passcli.feed_code(["session", "unlock"], "9999")
        self.assertIs(result.verdict, Verdict.WRONG_CODE)

    def test_a_command_that_demands_a_terminal(self):
        """The pty fallback. Without it this is the failure mode a GUI would hit and a
        terminal would not, which is the worst kind to find later."""
        self.set_stub(has_lock=True, locked=True, code="1234")
        with unittest.mock.patch.dict("os.environ", {"STUB_REQUIRE_TTY": "1"}):
            result = self.passcli.feed_code(["session", "unlock"], "1234")
        self.assertIs(result.verdict, Verdict.OK)

    def test_create_lock_answers_the_confirmation_too(self):
        self.set_stub(has_lock=False)
        result = self.passcli.feed_code(["session", "create-lock", "--idle-timeout", "600"], "4321")
        self.assertIs(result.verdict, Verdict.OK)


class TimeoutParsing(Harness):
    """The idle timeout is cosmetic - it only drives the countdown - but a parser that reads
    it from one shape of output and silently misses another is how a countdown goes missing
    after an upstream release. Both shapes seen in the wild are covered."""

    def test_after_the_yes(self):
        self.set_stub(has_lock=True, idle_timeout=600)
        self.assertEqual(self.passcli.info().idle_timeout, 600)

    def test_on_a_line_of_its_own(self):
        details = self.passcli.info  # the function, so the parse is exercised below
        self.assertTrue(callable(details))
        from protonpass_tray.passcli import _HAS_LOCK_RE, _SECONDS_RE, _TIMEOUT_RE

        output = "- Session has lock: yes\n- Idle timeout: 900 seconds\n"
        match = _HAS_LOCK_RE.search(output)
        self.assertIsNotNone(match)
        self.assertIsNone(_SECONDS_RE.search(match.group(2)))
        self.assertEqual(self.passcli._seconds(_TIMEOUT_RE.search(output).group(1)), 900)

    def test_no_timeout_anywhere_is_zero_not_a_crash(self):
        self.set_stub(has_lock=False)
        self.assertEqual(self.passcli.info().idle_timeout, 0)


class AControllingTerminal(Harness):
    """pass-cli reads the lock code from /dev/tty, not from stdin.

    A pty on stdin is not enough for that: /dev/tty resolves to the process's CONTROLLING
    terminal, and a process spawned from a desktop panel has none - so the read fails with
    "Error reading lock code", which is exactly what a real Plasma widget reported on
    2026-11-11 after everything else about it had been fixed.
    """

    def test_a_command_that_opens_dev_tty(self):
        self.set_stub(has_lock=True, locked=True, code="1234")
        with unittest.mock.patch.dict("os.environ", {"STUB_REQUIRE_CTTY": "1"}):
            result = self.passcli.feed_code(["session", "unlock"], "1234")
        self.assertIs(result.verdict, Verdict.OK)

    def test_create_lock_too(self):
        self.set_stub(has_lock=False)
        with unittest.mock.patch.dict("os.environ", {"STUB_REQUIRE_CTTY": "1"}):
            result = self.passcli.feed_code(
                ["session", "create-lock", "--idle-timeout", "600"], "4321"
            )
        self.assertIs(result.verdict, Verdict.OK)

    def test_the_code_is_never_in_what_comes_back(self):
        """A pty echoes by default, so without care the code lands in the output this
        classifies - and from there in a message shown to a user or written to a log."""
        self.set_stub(has_lock=True, locked=True, code="hunter2")
        with unittest.mock.patch.dict("os.environ", {"STUB_REQUIRE_CTTY": "1"}):
            result = self.passcli.feed_code(["session", "unlock"], "hunter2")
        self.assertNotIn("hunter2", result.message)


class TheCodeMustWaitForThePrompt(Harness):
    """Writing the code the moment the child is forked is a race, and it is lost about as
    often as it is won.

    A password reader turns echo off with tcsetattr(TCSAFLUSH), and TCSAFLUSH DISCARDS input
    already queued on the terminal. Write first and the code is thrown away by the very call
    that prepares the prompt; both sides then wait for the other until the timeout. That is
    what a Plasma widget showed on 2026-11-11 - pass-cli's own "Enter lock code:" sitting in
    the error line, with the spinner still going - after the same flow had worked minutes
    earlier.
    """

    def test_a_reader_that_flushes_before_prompting(self):
        self.set_stub(has_lock=True, locked=True, code="1234")
        with unittest.mock.patch.dict(
            "os.environ", {"STUB_REQUIRE_CTTY": "1", "STUB_FLUSH_FIRST": "1"}
        ):
            result = self.passcli.feed_code(["session", "unlock"], "1234", timeout=15)
        self.assertIs(result.verdict, Verdict.OK)

    def test_create_lock_answers_both_prompts(self):
        self.set_stub(has_lock=False)
        with unittest.mock.patch.dict(
            "os.environ", {"STUB_REQUIRE_CTTY": "1", "STUB_FLUSH_FIRST": "1"}
        ):
            result = self.passcli.feed_code(
                ["session", "create-lock", "--idle-timeout", "600"], "4321", timeout=15
            )
        self.assertIs(result.verdict, Verdict.OK)


class TheOrderOfTheTwoStrategies(Harness):
    """The pty is tried first because pass-cli reads /dev/tty.

    The other way round, a run from a TERMINAL made pass-cli prompt on the operator's own
    terminal and ignore the piped code - asking twice for the same thing, and waiting until
    the timeout if nobody was looking at that window.
    """

    def test_a_working_command_never_reaches_the_stdin_path(self):
        self.set_stub(has_lock=True, locked=True, code="1234")
        with unittest.mock.patch.dict("os.environ", {"STUB_REQUIRE_CTTY": "1"}):
            result = self.passcli.feed_code(["session", "unlock"], "1234", timeout=15)
        self.assertIs(result.verdict, Verdict.OK)
        # One invocation: the pty answered, so nothing tried again.
        self.assertEqual(len(self.stub_calls()), 1, self.stub_calls())

    def test_a_hang_is_a_result_and_not_a_traceback(self):
        self.set_stub(has_lock=True, locked=True, code="1234")
        with unittest.mock.patch.dict("os.environ", {"STUB_HANG": "1"}):
            result = self.passcli.feed_code(["session", "unlock"], "1234", timeout=3)
        self.assertEqual(result.returncode, 124)
        self.assertIn("did not answer", result.message)


class WhatAFailureSays(Harness):
    """The prompt is not the answer, and a timeout is not an unreadable answer.

    Both of these cost an evening on 2026-11-11. pass-cli writes "Enter lock code: " with no
    newline, so it is the first thing in every captured exchange and the real message, if
    there is one, continues the SAME line. Reporting that line whole put a red "Enter lock
    code:" in a Plasma widget as the reason an unlock had failed - for a session whose lock
    had quietly removed itself, so there was no failure to report in the first place.
    """

    def test_a_failure_never_reports_the_prompt(self):
        self.set_stub(has_lock=True, locked=True, code="1234")
        with unittest.mock.patch.dict("os.environ", {"STUB_UNREADABLE": "1"}):
            result = self.passcli.feed_code(["session", "unlock"], "1234")
        self.assertNotIn("code:", result.message.lower())

    def test_a_message_behind_the_prompt_survives_it(self):
        # The stripping is narrower than the matching on purpose: answering a prompt this does
        # not recognise costs nothing, while deleting a message it mistook for a prompt throws
        # away the only thing pass-cli said.
        self.assertEqual(
            self.passcli._first_line("Enter lock code: Invalid lock code: too short"),
            "Invalid lock code: too short",
        )

    def test_the_command_decides_whether_a_failure_was_a_wrong_code(self):
        """Not the output, which until 2026-11-11 was scraped for the word "unlock" - and the
        output of a failed unlock is the prompt, which does not contain it."""
        prompt_only = "Enter lock code: "
        self.assertIs(
            self.passcli._classify(1, prompt_only, ["session", "unlock"]), Verdict.WRONG_CODE
        )
        self.assertIs(self.passcli._classify(1, prompt_only, ["session", "lock"]), Verdict.UNKNOWN)

    def test_a_timeout_is_not_retried(self):
        """A pty that timed out means nobody answered the prompt. Running the whole exchange
        again down a channel pass-cli ignores only spends the timeout twice - a minute of a
        widget spinning with nothing to show - and it MASKED a real bug: the pty path could
        not answer a silent confirmation at all, because stdin takes both lines at once."""
        self.set_stub(has_lock=True, locked=True, code="1234")
        with unittest.mock.patch.dict("os.environ", {"STUB_HANG": "1"}):
            result = self.passcli.feed_code(["session", "unlock"], "1234", timeout=2)
        self.assertIs(result.verdict, Verdict.TIMEOUT)
        self.assertEqual(len(self.stub_calls()), 1, self.stub_calls())

    def test_the_pty_alone_answers_the_confirmation(self):
        """create-lock asks twice. Proven with stdin refused outright, because the fallback
        was what made this pass while the pty path timed out."""
        self.set_stub(has_lock=False)
        with unittest.mock.patch.dict("os.environ", {"STUB_REQUIRE_CTTY": "1"}):
            result = self.passcli.feed_code(
                ["session", "create-lock", "--idle-timeout", "600"], "4321"
            )
        self.assertIs(result.verdict, Verdict.OK)


class TheTimeoutPassCliActuallyPrints(Harness):
    """The two shapes in pass-cli 2.5.1's own strings, read off the binary on 2026-11-11:

        - Session has lock: yes (locks after 300s)
        Session will lock after 300 seconds of inactivity

    Neither was what this parser had been written against, and the unit is the dangerous part:
    "5m" read as 5 SECONDS gives every face a countdown that expires at once, from which it
    infers a lock that has not engaged.
    """

    def test_the_parenthetical_on_the_yes_line(self):
        self.assertEqual(self.passcli._seconds("(locks after 300s)"), 300)

    def test_a_unit_is_not_ignored(self):
        self.assertEqual(self.passcli._seconds("(locks after 5m)"), 300)
        self.assertEqual(self.passcli._seconds("(locks after 15 minutes)"), 900)

    def test_a_bare_number_is_still_seconds(self):
        self.assertEqual(self.passcli._seconds("yes (600)"), 600)

    def test_no_number_is_zero(self):
        self.assertEqual(self.passcli._seconds("yes"), 0)

    def test_the_sentence_on_its_own_line(self):
        from protonpass_tray.passcli import _LOCKS_AFTER_RE

        found = _LOCKS_AFTER_RE.search("Session will lock after 300 seconds of inactivity")
        self.assertIsNotNone(found)
        self.assertEqual(self.passcli._seconds(found.group(1)), 300)


class WhatPassCliSaysWhenItFails(Harness):
    """Its own failure lines, taken from `strings` on the 2.5.1 binary.

    None of these was in the table until 2026-11-11, so each arrived as UNKNOWN - and an
    UNKNOWN create-lock was answered by describing the state, which said "No lock configured":
    the very thing the operator had just asked to change.
    """

    def test_a_refused_operation_is_not_a_refused_code(self):
        self.assertIs(
            self.passcli._classify(1, "Error locking session", ["session", "create-lock"]),
            Verdict.FAILED,
        )
        self.assertIs(
            self.passcli._classify(1, "Error persisting session", ["session", "lock"]),
            Verdict.FAILED,
        )

    def test_a_request_that_never_arrived(self):
        for line in (
            "Error sending lock session request",
            "Error creating unlock session request",
            "Error sending remove session lock request",
            "Error sending force lock session request",
        ):
            with self.subTest(line=line):
                self.assertIs(self.passcli._classify(1, line, ["session", "lock"]), Verdict.FAILED)

    def test_the_two_that_really_are_a_wrong_code(self):
        """pass-cli has no "wrong code" string: it names the operation that failed."""
        self.assertIs(
            self.passcli._classify(1, "Error unlocking session", ["session", "unlock"]),
            Verdict.WRONG_CODE,
        )
        self.assertIs(
            self.passcli._classify(1, "Error removing session lock", ["session", "remove-lock"]),
            Verdict.WRONG_CODE,
        )

    def test_a_code_that_could_not_be_read_is_its_own_answer(self):
        """And not a wrong code, which it was until 2026-11-11 - reporting the wrong thing to
        the user AND suppressing the stdin fallback, the one case it exists for."""
        self.assertIs(
            self.passcli._classify(1, "Error reading lock code", ["session", "unlock"]),
            Verdict.CODE_UNREAD,
        )

    def test_the_fallback_runs_when_the_code_could_not_be_read(self):
        self.set_stub(has_lock=True, locked=True, code="1234")
        with unittest.mock.patch.dict("os.environ", {"STUB_REFUSE_TTY": "1"}):
            result = self.passcli.feed_code(["session", "unlock"], "1234")
        self.assertIs(result.verdict, Verdict.OK)
        # Twice: the pty could not deliver it, so stdin was tried - and answered.
        self.assertEqual(len(self.stub_calls()), 2, self.stub_calls())

    def test_a_named_failure_is_not_retried(self):
        """It was attempted and refused. Asking again down another channel just asks the API
        to refuse it twice."""
        self.set_stub(has_lock=True, locked=True, code="1234")
        with unittest.mock.patch.dict("os.environ", {"STUB_FAIL": "Error unlocking session"}):
            result = self.passcli.feed_code(["session", "unlock"], "1234")
        self.assertIs(result.verdict, Verdict.WRONG_CODE)
        self.assertEqual(len(self.stub_calls()), 1, self.stub_calls())


if __name__ == "__main__":
    unittest.main()
