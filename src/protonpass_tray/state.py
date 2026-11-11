"""What we believe the session's lock state to be, and where that belief is kept.

THE RULE THIS MODULE EXISTS TO ENFORCE: nothing here asks the Proton Pass API on its own.

The lock is an *idle* timer held server-side, so every request is activity that resets it. A
tray icon that polled to keep its picture current would stop the lock ever firing - and would
be telling the truth while doing it, which is what makes the bug so hard to see. So the state
is only ever written from a command the user caused: a lock, an unlock, a setup, or an
explicit refresh from the menu.

The cache lives in $XDG_RUNTIME_DIR, not in $XDG_STATE_HOME, because the runtime directory is
emptied at logout. A remembered "unlocked" that outlived the session it described would be the
one wrong answer with consequences.
"""

from __future__ import annotations

import enum
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path


class Lock(enum.Enum):
    UNLOCKED = "unlocked"
    LOCKED = "locked"
    #: The session has no lock configured at all - `setup` has not been run.
    NO_LOCK = "no-lock"
    NO_SESSION = "no-session"
    UNKNOWN = "unknown"


@dataclass
class State:
    lock: str = Lock.UNKNOWN.value
    #: Epoch seconds when this was last established, for the countdown and for staleness.
    since: float = 0.0
    #: The configured idle timeout, when `pass-cli info` has told us. 0 when unknown.
    idle_timeout: int = 0
    #: Free text for the tooltip - how we came to believe this.
    source: str = "never asked"

    @property
    def status(self) -> Lock:
        try:
            return Lock(self.lock)
        except ValueError:
            return Lock.UNKNOWN

    def expires_in(self) -> int | None:
        """Seconds until the session locks itself, if that is knowable.

        None when we have no timeout, no timestamp, or the state is not UNLOCKED. The number
        is a FLOOR, not a promise: any pass-cli command, including one run in another
        terminal, resets the server's timer without telling us.
        """
        if self.status is not Lock.UNLOCKED or not self.idle_timeout or not self.since:
            return None
        left = int(self.since + self.idle_timeout - time.time())
        return max(left, 0)

    def describe(self) -> str:
        left = self.expires_in()
        if self.status is Lock.UNLOCKED and left is not None:
            mins, secs = divmod(left, 60)
            return f"Unlocked - locks itself in about {mins}m {secs:02d}s if unused"
        return {
            Lock.UNLOCKED: "Unlocked",
            Lock.LOCKED: "Locked - a code is needed",
            Lock.NO_LOCK: "No lock configured",
            Lock.NO_SESSION: "Not signed in to Proton Pass",
            Lock.UNKNOWN: "State unknown",
        }[self.status]


def _path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(runtime) / "protonpass-tray" / "state.json"


def load() -> State:
    try:
        raw = json.loads(_path().read_text())
    except (OSError, ValueError):
        return State()
    known = {f: raw[f] for f in State.__dataclass_fields__ if f in raw}
    try:
        return State(**known)
    except TypeError:
        return State()


def save(state: State) -> State:
    p = _path()
    try:
        # 0700, and the file 0600. Nothing here is a secret - a lock state and two timestamps
        # - but the prompt socket lives in this directory too, and a directory that starts out
        # right cannot be got wrong later by something that only creates files.
        p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # mkdir's mode is applied only to a directory it CREATES, and this one usually exists
        # already - made under whatever umask the first process to get here had. Found at 0775.
        p.parent.chmod(0o700)
        # Written beside and renamed, so a reader never sees half a record.
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(state)))
        tmp.chmod(0o600)
        tmp.replace(p)
    except OSError:
        pass  # A tray that cannot cache still works; it just forgets between invocations.
    return state


def record(lock: Lock, *, source: str, idle_timeout: int | None = None) -> State:
    previous = load()
    return save(
        State(
            lock=lock.value,
            since=time.time(),
            idle_timeout=previous.idle_timeout if idle_timeout is None else idle_timeout,
            source=source,
        )
    )
