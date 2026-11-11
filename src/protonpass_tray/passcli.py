"""The one place that talks to `pass-cli`.

Three rules hold everywhere in this module, and each is here because breaking it would be
invisible rather than noisy:

  * The lock code never reaches `argv`. It goes down stdin, or down a pty when pass-cli
    insists on a terminal (`feed_code` does both). `ps` and process accounting read argv.
  * stdout of a `pass-cli item` command is a SECRET and is never logged, echoed or returned.
    This module only ever classifies it.
  * An output this module does not recognise is `Verdict.UNKNOWN`, never a success. The
    strings below are read off pass-cli 2.5.1 and are not a stable interface; a release that
    reworded them must turn this module uncertain, not confidently wrong.
"""

from __future__ import annotations

import contextlib
import enum
import os
import pty
import re
import select
import shutil
import signal
import subprocess
import termios
import time
from collections.abc import Sequence
from dataclasses import dataclass

#: Overridable so tests can inject a stub, and so a user can point at an absolute path.
#: Read on every call, not once at import: a module-level constant would freeze whatever the
#: environment happened to say when the first import ran, which is a different value for a
#: tray started by systemd than for the same code called from a shell.
ENV_VAR = "PROTONPASS_TRAY_PASS_CLI"


def binary() -> str:
    return os.environ.get(ENV_VAR) or "pass-cli"


DEFAULT_TIMEOUT = 30

# pass-cli 2.5.1. Compared case-insensitively against stdout+stderr.
_LOCKED = "session is locked. please unlock your session"
_NO_LOCK = "session has no lock"
_NOT_LOCKED = "session is not locked"
_ALREADY_HAS_LOCK = "session already has a lock"
_NO_SESSION = "no active session"
_OK_LOCKED = "session locked successfully"
_OK_UNLOCKED = "session unlocked successfully"
_OK_REMOVED = "session lock removed successfully"
_CODE_MISMATCH = "lock codes do not match"
_CODE_UNREADABLE = "error reading lock code"
_PAT_SESSION = "session lock is not available for pat sessions"
_AGENT_SESSION = "session lock is not available for agent sessions"
# The operation was attempted and the API or the local store refused it. Nothing to do with
# the code, so a face must not answer these by describing the state: read off `strings
# /usr/local/bin/pass-cli` on 2026-11-11, after a create-lock failed in words this module did
# not have and was reported as "No lock configured" - true, and no use to anyone.
_LOCK_FAILED = "error locking session"
_PERSIST_FAILED = "error persisting session"
#: Every shape of "the request did not get there": create/send, lock/unlock/remove, force.
_REQUEST_FAILED_RE = re.compile(r"error (?:creating|sending) [a-z ]*session[a-z ]*request", re.I)
# These two ARE how a wrong code comes back: the code was read, sent, and refused. pass-cli
# has no "wrong code" string of its own - it reports the failed operation instead.
_UNLOCK_FAILED = "error unlocking session"
_REMOVE_FAILED = "error removing session lock"


class Verdict(enum.Enum):
    """What pass-cli said, reduced to something a caller can branch on."""

    OK = "ok"
    LOCKED = "locked"
    NOT_LOCKED = "not-locked"
    NO_LOCK = "no-lock"
    HAS_LOCK = "has-lock"
    NO_SESSION = "no-session"
    WRONG_CODE = "wrong-code"
    UNSUPPORTED = "unsupported"
    #: Attempted, and refused for a reason that is not the code - the API said no, or the
    #: session could not be written. Distinct from UNKNOWN because pass-cli NAMED it, so its
    #: own sentence is worth showing, and distinct from WRONG_CODE because asking for the
    #: code again would be asking the wrong question.
    FAILED = "failed"
    #: pass-cli could not read the code at all. The one case the stdin fallback exists for,
    #: and it never ran: this was WRONG_CODE until 2026-11-11, which both reported the wrong
    #: thing to the user and suppressed the retry the docstring below promises.
    CODE_UNREAD = "code-unread"
    #: It hung. Distinct from UNKNOWN because the two want opposite handling: an answer this
    #: cannot read is worth asking again down another channel, while one that never came is
    #: worth nothing twice - and feed_code's retry turned a 30s wait into 60.
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Result:
    verdict: Verdict
    returncode: int
    #: Safe to show a user: it is pass-cli's own message, never item content.
    message: str


class NotInstalled(RuntimeError):
    pass


def available() -> bool:
    return shutil.which(binary()) is not None or os.path.isfile(binary())


def _classify(rc: int, output: str, args: Sequence[str] = ()) -> Verdict:
    low = output.lower()
    if _LOCKED in low:
        return Verdict.LOCKED
    if _NOT_LOCKED in low:
        return Verdict.NOT_LOCKED
    if _NO_LOCK in low:
        return Verdict.NO_LOCK
    if _ALREADY_HAS_LOCK in low:
        return Verdict.HAS_LOCK
    if _NO_SESSION in low:
        return Verdict.NO_SESSION
    if _PAT_SESSION in low or _AGENT_SESSION in low:
        return Verdict.UNSUPPORTED
    if _CODE_MISMATCH in low:
        return Verdict.WRONG_CODE
    if _CODE_UNREADABLE in low:
        return Verdict.CODE_UNREAD
    if _UNLOCK_FAILED in low or _REMOVE_FAILED in low:
        return Verdict.WRONG_CODE
    if _LOCK_FAILED in low or _PERSIST_FAILED in low or _REQUEST_FAILED_RE.search(low):
        return Verdict.FAILED
    if _OK_LOCKED in low or _OK_UNLOCKED in low or _OK_REMOVED in low:
        return Verdict.OK
    # An unlock whose code was simply wrong exits non-zero with the API's own wording, which
    # is not in the binary's strings and so cannot be matched by name. The COMMAND is what
    # settles it: this scraped the output for "unlock" until 2026-11-11, and the output of a
    # failed unlock is the prompt - "Enter lock code:" - which does not contain the word.
    if rc != 0:
        return Verdict.WRONG_CODE if "unlock" in args else Verdict.UNKNOWN
    return Verdict.UNKNOWN


def _first_line(output: str) -> str:
    """The first line worth showing someone - which is never the prompt.

    pass-cli writes "Enter lock code: " with no newline after it, so the prompt is the first
    thing in every captured exchange and the answer continues the SAME line. Returning the
    line whole put the prompt on screen as the error: a Plasma widget spent an evening showing
    a red "Enter lock code:" for a session that had no lock to unlock.
    """
    for line in output.splitlines():
        text = _PROMPT_PREFIX_RE.sub("", line).strip()
        if text:
            return text
    return ""


def run(args: list[str], *, timeout: int = DEFAULT_TIMEOUT) -> Result:
    """Run pass-cli with no stdin at all. Never used for anything that wants a code."""
    if not available():
        raise NotInstalled(f"{binary()} is not on PATH")
    proc = subprocess.run(  # noqa: S603 - fixed program, caller-controlled argv, no shell
        [binary(), *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    combined = f"{proc.stdout}\n{proc.stderr}"
    return Result(
        _classify(proc.returncode, combined, args), proc.returncode, _first_line(combined)
    )


def feed_code(args: list[str], code: str, *, timeout: int = DEFAULT_TIMEOUT) -> Result:
    """Run pass-cli and answer its `Enter lock code:` prompt.

    THE PTY FIRST, and that order is load-bearing. pass-cli reads the code from /dev/tty, so a
    pipe on stdin is not merely useless to it - from a TERMINAL it is actively wrong, because
    there /dev/tty is the operator's own terminal: pass-cli prints its prompt into that window,
    ignores the code on the pipe, and waits. The operator is asked twice for the same code, and
    if they are not watching that window it waits until the timeout.

    stdin stays as the fallback, for a release that one day reads it - but only for an answer
    that could not be READ. A pty that timed out means nobody answered the prompt, and running
    the whole exchange again down a channel pass-cli ignores only spends the timeout twice,
    which is a minute of a widget spinning with nothing to show for it.
    """
    result = _feed_pty(args, code, timeout=timeout)
    if result.verdict not in (Verdict.UNKNOWN, Verdict.CODE_UNREAD):
        return result
    return _feed_stdin(args, code, timeout=timeout)


def _feed_stdin(args: list[str], code: str, *, timeout: int) -> Result:
    if not available():
        raise NotInstalled(f"{binary()} is not on PATH")
    try:
        proc = subprocess.run(  # noqa: S603
            [binary(), *args],
            input=f"{code}\n{code}\n",
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        # Not a crash. A pass-cli reading /dev/tty ignores this pipe and waits for a person,
        # and a traceback arriving in a tray icon's error line helps nobody.
        return Result(Verdict.TIMEOUT, 124, "pass-cli did not answer in time")
    combined = _scrub(f"{proc.stdout}\n{proc.stderr}", code)
    return Result(
        _classify(proc.returncode, combined, args), proc.returncode, _first_line(combined)
    )


def _scrub(text: str, code: str) -> str:
    """Take the code out of anything that might be shown, logged or classified.

    A pty echoes by default and a program may print what it read, so this is not theoretical:
    without it the lock code can reach a tooltip, a notification or a journal."""
    return text.replace(code, "***") if code else text


def _feed_pty(args: list[str], code: str, *, timeout: int) -> Result:
    """Same, with the command given a terminal OF ITS OWN.

    pty.fork rather than openpty plus Popen, and the difference is the whole point: pass-cli
    reads the lock code from /dev/tty, which resolves to the process's CONTROLLING terminal
    and not to whatever is on its stdin. A pty handed over as stdin therefore changes nothing
    - the program still opens /dev/tty, still finds whatever the parent had, and on a process
    started from a desktop panel that is nothing at all. It then says "Error reading lock
    code", which is what a Plasma widget reported on 2026-11-11 with everything else about it
    already fixed. pty.fork puts the child in a new session with the pty as its controlling
    terminal, so /dev/tty resolves to the pipe we are holding.
    """
    if not available():
        raise NotInstalled(f"{binary()} is not on PATH")
    argv = [binary(), *args]

    pid, fd = pty.fork()
    if pid == 0:  # pragma: no cover - the child either execs or dies
        try:
            os.execvp(argv[0], argv)
        finally:
            os._exit(127)

    # Echo off before anything is written: what a pty would echo here is the lock code.
    with contextlib.suppress(OSError, termios.error):
        attrs = termios.tcgetattr(fd)
        attrs[3] &= ~termios.ECHO
        termios.tcsetattr(fd, termios.TCSANOW, attrs)

    # ONE PROMPT, ONE ANSWER - never write before being asked.
    #
    # A password reader turns echo off with tcsetattr(TCSAFLUSH), and TCSAFLUSH DISCARDS
    # whatever is already queued on the terminal. Writing the code at fork time therefore
    # loses it to the very call that prepares the prompt, roughly as often as it wins the
    # race: the program then waits for input that was thrown away, and we wait for output
    # that never comes, until the timeout. So this waits for the prompt and answers it, up
    # to twice - create-lock asks for the code and then for a confirmation.
    chunks: list[bytes] = []
    status: int | None = None
    timed_out = False
    sent = 0
    pending = ""
    quiet = 0
    deadline = time.monotonic() + timeout
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            timed_out = True
            break
        try:
            ready, _, _ = select.select([fd], [], [], min(left, 0.2))
        except OSError:
            break
        if ready:
            try:
                data = os.read(fd, 4096)
            except OSError:  # EIO: the child has gone and taken the pty with it
                break
            if not data:
                break
            chunks.append(data)
            quiet = 0
            pending += data.decode(errors="replace")
            if sent < 2 and _PROMPT_RE.search(pending):
                with contextlib.suppress(OSError):
                    os.write(fd, f"{code}\n".encode())
                sent += 1
                pending = ""  # so the next prompt is a new match and not this one again
        else:
            reaped, reaped_status = os.waitpid(pid, os.WNOHANG)
            if reaped:
                status = reaped_status
                break
            quiet += 1
            # A reader that asks for nothing in particular, or asks in words this does not
            # recognise, would otherwise wait for a prompt that never comes. After a second
            # of silence, answer anyway.
            #
            # `sent < 2` and not `sent == 0`: a CONFIRMATION can be the silent one. A
            # create-lock that printed no second prompt got the code once and then waited for
            # a line that never came, for the whole timeout - invisible until a timeout stopped
            # being retried on stdin, where both lines go at once. A spare line written to a
            # program that was about to answer anyway is unread input on a pty about to close.
            if sent < 2 and quiet >= 5:
                with contextlib.suppress(OSError):
                    os.write(fd, f"{code}\n".encode())
                sent += 1
                quiet = 0

    os.close(fd)
    if status is None:
        if timed_out:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        _, status = os.waitpid(pid, 0)

    if timed_out:
        return Result(Verdict.TIMEOUT, 124, "pass-cli did not answer in time")

    rc = os.waitstatus_to_exitcode(status)
    combined = _scrub(b"".join(chunks).decode(errors="replace"), code)
    return Result(_classify(rc, combined, args), rc, _first_line(combined))


@dataclass(frozen=True)
class Info:
    """What `pass-cli info` says, parsed here so no caller has to hold its raw output.

    `has_lock` is None when the output did not contain the field at all - an older or newer
    pass-cli, or no session. Note what this CANNOT tell you: whether the session is locked
    right now. Proton's own documentation is explicit that "having a session lock does not
    mean that the session is locked at this moment", so a second read is what settles that.
    """

    verdict: Verdict
    has_lock: bool | None
    idle_timeout: int
    account: str


_HAS_LOCK_RE = re.compile(r"session has lock:\s*(yes|no)([^\n]*)", re.I)
_SECONDS_RE = re.compile(r"(\d+)")
#: A duration as pass-cli may print it, with the unit it may or may not carry. The unit is why
#: this is not a bare number: "(locks after 5m)" read as 5 SECONDS gives every face a countdown
#: that expires immediately and then infers a lock that has not engaged.
_DURATION_RE = re.compile(r"(\d+)\s*(seconds?|secs?|minutes?|mins?|s|m)?\b", re.I)
#: pass-cli 2.5.1 prints the timeout in two shapes, both read off the binary's own strings:
#:   "- Session has lock: yes (locks after 300s)"
#:   "Session will lock after 300 seconds of inactivity"
_LOCKS_AFTER_RE = re.compile(r"locks? after\s+([0-9]+\s*[a-z]*)", re.I)
#: A fallback for the timeout, in case a release prints it on its own line rather than after
#: the yes. Missing it costs only the countdown, never the state, so this is insurance and not
#: a second parser to keep in step.
_TIMEOUT_RE = re.compile(r"idle[ _-]?timeout\D{0,20}?([0-9]+\s*[a-z]*)", re.I)


def _seconds(text: str) -> int:
    """A duration in seconds, whatever unit it was written in. 0 if there is no number."""
    match = _DURATION_RE.search(text)
    if not match:
        return 0
    unit = (match.group(2) or "s").lower()
    return int(match.group(1)) * (60 if unit.startswith("m") else 1)


_EMAIL_RE = re.compile(r"^\s*-?\s*(?:email|username):\s*(\S+)", re.I | re.M)

#: A prompt is the tail of what has been read with no newline after it - "Enter lock code: ",
#: "Re-enter lock code: ". Anchored at the end so a prompt already answered cannot match again.
_PROMPT_RE = re.compile(r"[^\n]*code[^\n]*:[ \t]*$", re.I)

#: The same prompts where a message FOLLOWS them on the line, for taking off the front of one.
#: Deliberately narrower than _PROMPT_RE, and the asymmetry is the point: answering a prompt
#: this does not recognise costs nothing, while deleting a message it mistook for a prompt
#: loses the only thing pass-cli said. So this insists on the asking word, which leaves
#: "Invalid lock code: too short" intact. Repeated, because create-lock asks twice.
_PROMPT_PREFIX_RE = re.compile(
    r"^(?:\s*(?:re-?)?(?:enter|confirm|repeat)[^\n:]*\bcode\b[^\n:]*:[ \t]*)+", re.I
)


def info(*, timeout: int = DEFAULT_TIMEOUT) -> Info:
    if not available():
        raise NotInstalled(f"{binary()} is not on PATH")
    proc = subprocess.run(  # noqa: S603
        [binary(), "info"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    combined = f"{proc.stdout}\n{proc.stderr}"
    verdict = _classify(proc.returncode, combined, ["info"])
    has_lock: bool | None = None
    seconds = 0
    if match := _HAS_LOCK_RE.search(combined):
        has_lock = match.group(1).lower() == "yes"
        if has_lock:
            # The tail of the line that said "yes" first, since the number there is certainly
            # this lock's; then the other two shapes, anywhere in the output.
            for text in (
                match.group(2),
                *(found.group(1) for found in _LOCKS_AFTER_RE.finditer(combined)),
                *(found.group(1) for found in _TIMEOUT_RE.finditer(combined)),
            ):
                seconds = _seconds(text)
                if seconds:
                    break
    account = match.group(1) if (match := _EMAIL_RE.search(combined)) else ""
    return Info(verdict, has_lock, seconds, account)
