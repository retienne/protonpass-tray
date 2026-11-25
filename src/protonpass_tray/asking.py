"""Saying that a prompt is waiting for the lock code, so that a desktop face can tell its user.

A prompt in a terminal is easy to miss: the tab is behind another, the recipe that needs the
code was started an hour ago. While one waits, a small file in $XDG_RUNTIME_DIR says so, and
a face that reads it can raise a notification. Nothing here is an API call.

THE FILE PROVES ITSELF OR IS IGNORED. A prompt that is killed cannot take its file away, and
a notification about a prompt that is gone cannot be answered. So a waiting prompt rewrites
its file every few seconds, and a reader believes one only for STALE_S after the last write -
which needs no process table, and so works for a QML widget that can only `cat`.

WHAT IT SAYS is the program that was started, its directory's name and the terminal. Never a
command line: those carry secrets, and this text ends up in a notification.

The other direction is one empty file, `listening`, which a face that raises notifications
touches while it runs. Something with no terminal may wait for the code only while it is
fresh: with nobody to tell, waiting is a hang.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import time
from pathlib import Path

#: How often a waiting prompt says it is still there, and how long that is believed. The
#: second is three of the first, so one slow write does not close a notification.
BEAT_S = 5.0
STALE_S = 15.0

#: After a wait nobody answered, how long the next one fails at once. One run of a recipe asks
#: for several secrets, and each would otherwise wait its whole time for someone who is away.
UNANSWERED_S = 300.0

#: Anything a notification server could read as markup, and control characters.
_UNSAFE = re.compile(r"[^\w .,@:/+-]")


def directory() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(runtime) / "protonpass-tray" / "asking"


def _fresh(path: Path, seconds: float) -> bool:
    try:
        return time.time() - path.stat().st_mtime < seconds
    except OSError:
        return False


def listening() -> bool:
    """Is there a face running that would tell its user we are waiting?"""
    return _fresh(directory().parent / "listening", STALE_S)


def unanswered() -> bool:
    """Has a wait just ended with nobody answering it?"""
    return _fresh(directory().parent / "unanswered", UNANSWERED_S)


def went_unanswered() -> None:
    with contextlib.suppress(OSError):
        (directory().parent / "unanswered").touch(mode=0o600)


def _clean(text: str) -> str:
    return _UNSAFE.sub("", text).strip()[:60]


def _stat(pid: int) -> tuple[str, int, int]:
    """A process's name, its parent and its session, from /proc."""
    raw = Path(f"/proc/{pid}/stat").read_text()
    # The name is in brackets and may itself hold brackets and spaces, so from the last one.
    name = raw[raw.index("(") + 1 : raw.rindex(")")]
    fields = raw[raw.rindex(")") + 2 :].split()
    return name, int(fields[1]), int(fields[3])


def asker() -> str:
    """The program the code is wanted for: the one the shell of this terminal started.

    Between it and us are the scripts that led here - `just`, a bash, another bash, us - and
    the outermost is the one its user would recognise. The session leader itself is that
    shell, so the walk stops below it. Empty when we were started by hand.
    """
    ours = os.getpid()
    try:
        _, parent, session = _stat(ours)
        best = ""
        while parent > 1 and parent != session:
            name, above, its_session = _stat(parent)
            if its_session != session:
                break
            best = name
            parent = above
        return _clean(best)
    except (OSError, ValueError, IndexError):
        return ""  # no /proc, or a process that ended while we looked


def _terminal() -> str:
    for fd in (0, 2):
        with contextlib.suppress(OSError):
            return os.ttyname(fd).removeprefix("/dev/")
    return ""


def _starter() -> str:
    """What started this session, for one with no terminal to name: systemd, cron, an agent."""
    try:
        session = os.getsid(0)
        return _stat(_stat(session)[1])[0]
    except (OSError, ValueError, IndexError):
        return ""


def describe() -> str:
    """What to call this prompt: "just in jimi-ansible (pts/47)", or "A terminal (pts/47)"."""
    name = asker()
    terminal = _terminal()
    if name:
        with contextlib.suppress(OSError):
            place = _clean(os.path.basename(os.getcwd()))
            name = f"{name} in {place}" if place else name
    else:
        name = "A terminal" if terminal else "A program"
    where = _clean(terminal or _starter())
    return f"{name} ({where})" if where else name


class Asking:
    """A context manager: the file exists while it is open. `beat()` keeps it believed."""

    def __init__(self) -> None:
        self.path = directory() / f"{os.getpid()}.json"
        self.since = time.time()
        self.who = describe()
        self._last = 0.0

    def __enter__(self) -> Asking:
        _prune()
        self.beat()
        return self

    def __exit__(self, *_exc) -> None:
        with contextlib.suppress(OSError):
            self.path.unlink()

    def beat(self) -> None:
        """Say we are still here, at most every BEAT_S. Safe to call as often as one likes."""
        now = time.time()
        if 0 <= now - self._last < BEAT_S:
            return
        self._last = now
        record = {"id": f"{os.getpid()}-{self.since:.3f}", "who": self.who}
        record.update(since=self.since, alive=now)
        # A prompt that cannot announce itself still asks.
        with contextlib.suppress(OSError):
            self.path.parent.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.path.parent.parent.chmod(0o700)
            self.path.parent.mkdir(exist_ok=True, mode=0o700)
            tmp = self.path.with_suffix(".tmp")
            # One line each: a reader `cat`s them all at once.
            tmp.write_text(json.dumps(record) + "\n")
            tmp.chmod(0o600)
            tmp.replace(self.path)


def _prune() -> None:
    """Take away what killed prompts left. A stopped one that resumes writes its own again."""
    with contextlib.suppress(OSError):
        for path in directory().iterdir():
            with contextlib.suppress(OSError):
                if time.time() - path.stat().st_mtime > STALE_S:
                    path.unlink()
