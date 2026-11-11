"""What the five operations actually do, with no opinion about how to talk to a person.

cli.py prints the results of these and returns their exit codes; tray.py shows them in a
dialog. Keeping them here is what makes the two front-ends the same program rather than two
programs that agree for now - and it is why the exit codes live here too: they are the
interface a shell hook branches on, so they belong next to the logic that decides them.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import passcli, state
from .passcli import Verdict
from .state import Lock

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_UNRECOGNISED = 3
EXIT_NO_PASS_CLI = 4
EXIT_LOCKED = 10
EXIT_NO_LOCK = 11
EXIT_NO_SESSION = 12

_CODE_OF = {
    Lock.UNLOCKED: EXIT_OK,
    Lock.LOCKED: EXIT_LOCKED,
    Lock.NO_LOCK: EXIT_NO_LOCK,
    Lock.NO_SESSION: EXIT_NO_SESSION,
    Lock.UNKNOWN: EXIT_UNRECOGNISED,
}


@dataclass(frozen=True)
class Outcome:
    exit_code: int
    #: Safe to show anyone: our own words, or one line of pass-cli's. Never item content.
    message: str = ""
    is_error: bool = False
    state: state.State | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code == EXIT_OK


def exit_code_for(status: Lock) -> int:
    return _CODE_OF[status]


def status() -> state.State:
    """The cached belief. Free, and asks nothing - see state.py."""
    return state.load()


def refresh() -> Outcome:
    """Establish the state for real. Two API calls, and it resets the idle timer.

    `info` says whether a lock exists and what its timeout is, but not whether the session is
    locked at this moment. So a second, harmless read - `vault list`, which prints vault names
    and no secrets - is what distinguishes locked from unlocked.

    THE PROBE RUNS EVEN WHEN `info` SAYS THERE IS NO LOCK, and that is not caution: on
    2026-11-11 a live session answered `info` with "Session has lock: no" while every other
    call refused with "Session is locked. Please unlock your session and try again." Returning
    early on `info` published "no lock configured" for hours, so every face offered the one
    thing that fixes that - create a lock - which pass-cli also refuses on a locked session.
    A reading that contradicts the behaviour loses to the behaviour.
    """
    details = passcli.info()
    if details.verdict is Verdict.NO_SESSION:
        st = state.record(Lock.NO_SESSION, source="pass-cli info", idle_timeout=0)
        return Outcome(EXIT_NO_SESSION, st.describe(), state=st)

    probe = passcli.run(["vault", "list"])
    timeout = details.idle_timeout
    if probe.verdict is Verdict.LOCKED:
        # The source carries the contradiction when there is one: a cache saying "locked" on
        # a session whose `info` denies the lock is the single most useful thing to find here
        # next time, and it would otherwise be invisible.
        source = "pass-cli vault list"
        if details.has_lock is False:
            source += " (info said no lock)"
        st = state.record(Lock.LOCKED, source=source, idle_timeout=timeout)
    elif probe.verdict is Verdict.NO_SESSION:
        st = state.record(Lock.NO_SESSION, source="pass-cli vault list", idle_timeout=0)
    elif details.has_lock is False:
        st = state.record(Lock.NO_LOCK, source="pass-cli info", idle_timeout=0)
    elif probe.returncode == 0:
        st = state.record(Lock.UNLOCKED, source="pass-cli vault list", idle_timeout=timeout)
    else:
        st = state.record(Lock.UNKNOWN, source=f"pass-cli vault list: {probe.message}")
    return Outcome(exit_code_for(st.status), st.describe(), state=st)


def _reconciled() -> state.State | None:
    """What the session actually looks like, or None if that could not be established.

    The point is the CACHE as much as the answer. An operation whose outcome cannot be read
    leaves the cached state untouched, so every face reading that cache goes on offering the
    operation that just failed - which is how a Plasma widget came to ask for a PIN again and
    again, for a lock that no longer existed, reporting pass-cli's own prompt as the reason.

    A failed second opinion must not replace the first failure, so anything it raises means
    None and the caller keeps what it already knew. NotInstalled is the exception: pass-cli
    went away mid-run, and cli.py has a better sentence for that than this does.
    """
    try:
        outcome = refresh()
    except passcli.NotInstalled:
        raise
    except Exception:  # noqa: BLE001
        return None
    if outcome.state is None or outcome.state.status is Lock.UNKNOWN:
        return None
    return outcome.state


def _unreadable(said: str, fallback: str, *, unchanged: tuple[Lock, ...] = ()) -> Outcome:
    """An operation whose answer could not be read: repair the cache, then answer.

    Repairing the cache is unconditional and is half the point - a face reads it to decide
    what to offer next. Choosing the MESSAGE is the other half, and getting that order wrong
    cost an evening on 2026-11-11 in each direction. pass-cli's own sentence comes first when
    there is one, because it is the most specific thing anyone knows: answering with the state
    instead turned "Error locking session" into "No lock configured" - true, and no answer at
    all to someone who had just asked for a lock to be created.

    `unchanged` is the state, or states, the operation was trying to LEAVE. Finding the
    session still in one of them says only that the operation failed, so it is no answer
    either - and in particular must not become one of the exit codes that mean "here is the
    state", which a face reads as an instruction to offer that same operation again.
    """
    st = _reconciled()
    said = said.strip()
    informative = st is not None and st.status not in unchanged
    if informative and not said:
        return Outcome(
            exit_code_for(st.status),
            st.describe(),
            is_error=st.status is not Lock.UNLOCKED,
            state=st,
        )
    code = exit_code_for(st.status) if informative else EXIT_UNRECOGNISED
    # A failure never exits 0, whatever the state turned out to be: a shell hook branches on
    # this before it reads anything.
    return Outcome(code or EXIT_UNRECOGNISED, said or fallback, is_error=True, state=st)


def _timeout_for_unlock() -> int | None:
    """The idle timeout to record after an unlock: None keeps what the cache already knows.

    `state.record` carries the previous number forward, which is right almost always. But
    after any spell where the cache believed there was no lock it carries 0, and 0 means "no
    countdown" to every face: `expires_in()` returns None, so the tray's `expires_in() == 0`
    inference never fires and the icon reads "unlocked" until something else refreshes it.
    Found on 2026-11-11 in the cache written by the unlock that proved the lock had been there
    all along - `idle_timeout: 0` on a session with a live 300s lock. One `info` call, and
    only when the number is actually missing.
    """
    if state.load().idle_timeout:
        return None
    try:
        return passcli.info().idle_timeout or None
    except Exception:  # noqa: BLE001
        return None


def lock() -> Outcome:
    result = passcli.run(["session", "lock"])
    if result.verdict in (Verdict.OK, Verdict.LOCKED):
        st = state.record(Lock.LOCKED, source="protonpass-tray lock")
        return Outcome(EXIT_OK, st.describe(), state=st)
    if result.verdict is Verdict.NO_LOCK:
        st = state.record(Lock.NO_LOCK, source="protonpass-tray lock", idle_timeout=0)
        return Outcome(
            EXIT_NO_LOCK,
            "This session has no lock yet. Run: protonpass-tray setup",
            is_error=True,
            state=st,
        )
    if result.verdict is Verdict.NO_SESSION:
        return Outcome(
            EXIT_NO_SESSION, "No Proton Pass session. Run: pass-cli login", is_error=True
        )
    return _unreadable(
        result.message, "pass-cli could not lock the session", unchanged=(Lock.UNLOCKED,)
    )


def unlock(code: str) -> Outcome:
    result = passcli.feed_code(["session", "unlock"], code)
    if result.verdict is Verdict.OK:
        st = state.record(
            Lock.UNLOCKED, source="protonpass-tray unlock", idle_timeout=_timeout_for_unlock()
        )
        return Outcome(EXIT_OK, st.describe(), state=st)
    if result.verdict is Verdict.NOT_LOCKED:
        # "Session is not locked" is pass-cli's LOCAL belief, and it goes stale: on 2026-11-11
        # `session unlock` answered exactly that while `vault list` was being refused with
        # "Session is locked" - the server had auto-locked on idleness and the client had not
        # found out. Recording "unlocked" on its word is the same mistake as believing `info`,
        # and it would have had every face show a green icon over a session that could not
        # read a single secret. So probe, and report what the probe says.
        st = _reconciled()
        if st is None:
            st = state.record(
                Lock.UNLOCKED,
                source="pass-cli: was not locked",
                idle_timeout=_timeout_for_unlock(),
            )
        return Outcome(
            exit_code_for(st.status),
            st.describe(),
            is_error=st.status is not Lock.UNLOCKED,
            state=st,
        )
    if result.verdict is Verdict.NO_LOCK:
        st = state.record(Lock.NO_LOCK, source="protonpass-tray unlock", idle_timeout=0)
        return Outcome(EXIT_NO_LOCK, "This session has no lock.", is_error=True, state=st)
    if result.verdict is Verdict.WRONG_CODE:
        # "Wrong code" is a guess, and the one other thing it looks like matters: an unlock
        # with NO LOCK left to unlock also fails in wording this cannot read. Telling a face
        # "wrong code" for that leaves it offering the same field forever, which is exactly
        # what it did. So the state is re-established before the sentence is chosen.
        st = _reconciled()
        if st is not None and st.status is not Lock.LOCKED:
            return Outcome(
                exit_code_for(st.status),
                st.describe(),
                is_error=st.status is not Lock.UNLOCKED,
                state=st,
            )
        return Outcome(EXIT_LOCKED, "That code was not accepted.", is_error=True, state=st)
    return _unreadable(
        result.message, "pass-cli could not unlock the session", unchanged=(Lock.LOCKED,)
    )


def create_lock(code: str, idle_timeout: int) -> Outcome:
    if not 30 <= idle_timeout <= 900:
        return Outcome(
            EXIT_USAGE, "--idle-timeout must be between 30 and 900 seconds", is_error=True
        )
    result = passcli.feed_code(
        ["session", "create-lock", "--idle-timeout", str(idle_timeout)], code
    )
    if result.verdict is Verdict.OK:
        # Established, not assumed. pass-cli answers a successful create-lock with "Session
        # locked successfully" - the same sentence it uses for `session lock` - so whether
        # creating a lock also ENGAGES it cannot be read off the output, and guessing
        # "unlocked" would have every face cheerfully counting down a session that was already
        # locked. One extra API call at setup time settles it.
        outcome = refresh()
        return Outcome(
            EXIT_OK,
            f"Lock created. The session locks itself after {idle_timeout}s unused.",
            state=outcome.state,
        )
    if result.verdict is Verdict.HAS_LOCK:
        return Outcome(
            EXIT_USAGE,
            "This session already has a lock. Remove it first to change the timeout.",
            is_error=True,
        )
    if result.verdict is Verdict.UNSUPPORTED:
        return Outcome(
            EXIT_USAGE,
            "Personal-access-token and agent sessions cannot carry a session lock.",
            is_error=True,
        )
    # NO_LOCK is the state create-lock was asked to change, so finding it afterwards is
    # not news - it is the failure, and reporting it as the state is what sent a widget
    # back to the same button that had just failed.
    return _unreadable(
        result.message, "pass-cli could not create the lock", unchanged=(Lock.NO_LOCK,)
    )


def remove_lock(code: str) -> Outcome:
    result = passcli.feed_code(["session", "remove-lock"], code)
    if result.verdict is Verdict.OK:
        st = state.record(Lock.NO_LOCK, source="protonpass-tray remove-lock", idle_timeout=0)
        return Outcome(EXIT_OK, "Lock removed. The session is no longer gated by a code.", state=st)
    if result.verdict is Verdict.WRONG_CODE:
        return Outcome(EXIT_LOCKED, "That code was not accepted.", is_error=True)
    # Either state means the lock is still there, so neither answers a removal.
    return _unreadable(
        result.message,
        "pass-cli could not remove the lock",
        unchanged=(Lock.LOCKED, Lock.UNLOCKED),
    )
