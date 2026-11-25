"""Where a lock code comes from, in order of preference.

Deliberately not `kdialog`, `zenity` or any one desktop's helper: the point of this file is
that the same code path works on KDE, GNOME, sway and over ssh. The GUI prompt, when there is
one, belongs to the tray process itself (Qt), reached over its socket - so a hook running
under systemd with no terminal still gets a dialog, and the same hook on a server still works.

A code is never taken from an environment variable or from argv. Both are readable by other
processes, and a lock code that leaks is a lock that was never there.

A prompt can outlive its question. An unlock left waiting in a terminal while the code is
typed into a panel has nothing left to ask, so `ask` takes a `done` - "has it happened
already?" - and every source here gives up when it turns true. `pass-cli session unlock` has
no such thing: it blocks on its own read of /dev/tty, and nothing outside can answer it.
"""

from __future__ import annotations

import contextlib
import getpass
import json
import os
import select
import shutil
import socket
import subprocess
import sys
import termios
import time
from collections.abc import Callable

#: "Has what the code was wanted for happened already?" Must be free to ask: it is polled.
Done = Callable[[], bool]

#: How often a waiting prompt asks it.
POLL_S = 0.5


class NoPrompt(RuntimeError):
    """Nothing on this machine can ask the user for a code right now."""


class Settled(RuntimeError):
    """What the code was wanted for happened elsewhere while we were asking for it."""


def socket_path() -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return os.path.join(runtime, "protonpass-tray", "prompt.sock")


def from_tray(reason: str, *, timeout: float = 120.0, done: Done | None = None) -> str | None:
    """Ask a running tray to show its dialog. None when no tray is listening."""
    request = {"ask": reason}
    if done is not None:
        # The dialog is the tray's, so closing it is too. An older tray ignores this.
        request["until"] = "unlocked"
    path = socket_path()
    if not os.path.exists(path):
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(path)
            s.sendall(json.dumps(request).encode() + b"\n")
            deadline = time.monotonic() + timeout
            while not select.select([s], [], [], POLL_S)[0]:
                if done is not None and done():
                    raise Settled
                if time.monotonic() > deadline:
                    return None
            data = s.makefile("rb").readline()
    except OSError:
        return None
    try:
        answer = json.loads(data)
    except ValueError:
        return None
    code = answer.get("code")
    return code if isinstance(code, str) and code else None


def _read_tty(reason: str, done: Done) -> str | None:
    """getpass, except that it stops waiting when `done` says so."""
    fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    try:
        before = termios.tcgetattr(fd)
        silent = list(before)
        silent[3] &= ~termios.ECHO
        # TCSAFLUSH both times, and the second is the one that matters: it discards a code
        # half typed when the prompt ended, which the shell would otherwise read and echo.
        termios.tcsetattr(fd, termios.TCSAFLUSH, silent)
        try:
            os.write(fd, f"{reason}: ".encode())
            while not select.select([fd], [], [], POLL_S)[0]:
                if done():
                    raise Settled
            return os.read(fd, 4096).decode(errors="replace").rstrip("\r\n") or None
        finally:
            termios.tcsetattr(fd, termios.TCSAFLUSH, before)
            os.write(fd, b"\n")
    finally:
        os.close(fd)


def from_tty(reason: str, *, done: Done | None = None) -> str | None:
    if not sys.stdin.isatty():
        return None
    try:
        if done is not None:
            with contextlib.suppress(OSError, termios.error):
                return _read_tty(reason, done)
        return getpass.getpass(f"{reason}: ") or None
    except (EOFError, KeyboardInterrupt):
        return None


#: Tried in this order, all with the same calling convention: the code on stdout, nothing
#: else. Not a dependency - each is used only if it happens to be installed.
_DIALOGS = (
    ("kdialog", ["--password", "{reason}", "--title", "Proton Pass"]),
    ("zenity", ["--password", "--title", "Proton Pass"]),
    ("qarma", ["--password", "--title", "Proton Pass"]),
)


def _run(argv: list[str], timeout: float, done: Done | None) -> subprocess.CompletedProcess:
    """subprocess.run for a program that is asking a person, closed when `done` says so."""
    with subprocess.Popen(  # noqa: S603
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    ) as proc:
        deadline = time.monotonic() + timeout
        while True:
            try:
                out, err = proc.communicate(timeout=POLL_S)
                return subprocess.CompletedProcess(argv, proc.returncode, out, err)
            except subprocess.TimeoutExpired:
                if done is not None and done():
                    proc.terminate()
                    raise Settled from None
                if time.monotonic() > deadline:
                    proc.kill()
                    raise
            except BaseException:
                proc.kill()
                raise


def from_dialog(reason: str, *, timeout: int = 120, done: Done | None = None) -> str | None:
    """The desktop's own password dialog, for a caller with a screen but no terminal.

    This is what a Plasma widget relies on. QML can neither write a file nor open a socket, so
    a PIN typed into a widget could only reach us through argv, where any process on the
    machine can read it - the widget therefore asks for nothing and triggers the unlock, and
    the code is typed into this dialog instead, going straight down a pipe.
    """
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        return None
    for program, template in _DIALOGS:
        if not shutil.which(program):
            continue
        argv = [program, *(part.format(reason=reason) for part in template)]
        try:
            proc = _run(argv, timeout, done)
        except (OSError, subprocess.TimeoutExpired):
            continue
        code = proc.stdout.strip()
        if proc.returncode == 0 and code:
            return code
        # A dialog that ran and was cancelled is an answer: do not fall through to another.
        if proc.returncode == 0 or proc.returncode == 1:
            return None
    return None


def from_systemd(reason: str, *, timeout: int = 120, done: Done | None = None) -> str | None:
    """`systemd-ask-password` - present on every systemd distribution, and it finds whatever
    agent the session has: a terminal one, Plasma's, GNOME's, or the one on tty1."""
    try:
        proc = _run(
            [
                "systemd-ask-password",
                f"--timeout={timeout}",
                "--icon=dialog-password",
                "--id=protonpass-tray",
                f"{reason}:",
            ],
            timeout + 5,
            done,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    code = proc.stdout.strip()
    return code if proc.returncode == 0 and code else None


def from_file(path: str) -> str:
    """Read a one-shot code file and delete it.

    This exists for a front-end that can only run a command LINE - a Plasma widget, whose one
    way of invoking anything is the executable data engine - where putting the code in argv
    would publish it to `ps`. The file is read and unlinked whatever happens, so a crash
    cannot leave a code lying about.

    It must be ours and it must not be readable by anyone else. Both are refused rather than
    warned about: a code file with the wrong mode is not a smaller version of this feature,
    it is the leak this feature exists to avoid.
    """
    try:
        info = os.stat(path)
    except OSError as exc:
        raise NoPrompt(f"cannot read the code file: {exc}") from exc
    try:
        if info.st_uid != os.getuid():
            raise NoPrompt(f"{path} is not owned by this user")
        if info.st_mode & 0o077:
            raise NoPrompt(f"{path} is readable by others (mode {info.st_mode & 0o777:04o})")
        with open(path) as fh:
            code = fh.readline().strip()
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)
    if not code:
        raise NoPrompt(f"{path} held no code")
    return code


def from_stdin() -> str:
    """Read the code from stdin, for a front-end that can write to a pipe - which is every
    front-end except a Plasma widget. GJS's Gio.Subprocess does this natively."""
    code = sys.stdin.readline().strip()
    if not code:
        raise NoPrompt("no code arrived on stdin")
    return code


def ask(reason: str = "Proton Pass lock code", *, done: Done | None = None) -> str:
    """The code, from the first source that can ask. Settled when `done` turns true first."""
    for source in (from_tray, from_tty, from_dialog, from_systemd):
        code = source(reason, done=done)
        if code:
            return code
        # A tray that closed its own dialog answers with no code. Without this the next source
        # would ask again for what has just been done.
        if done is not None and done():
            raise Settled
    raise NoPrompt(
        "no way to ask for the lock code here: no tray is running, stdin is not a terminal, "
        "no password dialog is installed, and systemd-ask-password gave nothing"
    )
