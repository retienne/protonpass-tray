"""Where a lock code comes from, in order of preference.

Deliberately not `kdialog`, `zenity` or any one desktop's helper: the point of this file is
that the same code path works on KDE, GNOME, sway and over ssh. The GUI prompt, when there is
one, belongs to the tray process itself (Qt), reached over its socket - so a hook running
under systemd with no terminal still gets a dialog, and the same hook on a server still works.

A code is never taken from an environment variable or from argv. Both are readable by other
processes, and a lock code that leaks is a lock that was never there.
"""

from __future__ import annotations

import contextlib
import getpass
import json
import os
import shutil
import socket
import subprocess
import sys


class NoPrompt(RuntimeError):
    """Nothing on this machine can ask the user for a code right now."""


def socket_path() -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return os.path.join(runtime, "protonpass-tray", "prompt.sock")


def from_tray(reason: str, *, timeout: float = 120.0) -> str | None:
    """Ask a running tray to show its dialog. None when no tray is listening."""
    path = socket_path()
    if not os.path.exists(path):
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(path)
            s.sendall(json.dumps({"ask": reason}).encode() + b"\n")
            data = s.makefile("rb").readline()
    except OSError:
        return None
    try:
        answer = json.loads(data)
    except ValueError:
        return None
    code = answer.get("code")
    return code if isinstance(code, str) and code else None


def from_tty(reason: str) -> str | None:
    if not sys.stdin.isatty():
        return None
    try:
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


def from_dialog(reason: str, *, timeout: int = 120) -> str | None:
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
            proc = subprocess.run(  # noqa: S603
                argv, capture_output=True, text=True, timeout=timeout
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        code = proc.stdout.strip()
        if proc.returncode == 0 and code:
            return code
        # A dialog that ran and was cancelled is an answer: do not fall through to another.
        if proc.returncode == 0 or proc.returncode == 1:
            return None
    return None


def from_systemd(reason: str, *, timeout: int = 120) -> str | None:
    """`systemd-ask-password` - present on every systemd distribution, and it finds whatever
    agent the session has: a terminal one, Plasma's, GNOME's, or the one on tty1."""
    try:
        proc = subprocess.run(  # noqa: S603
            [
                "systemd-ask-password",
                f"--timeout={timeout}",
                "--icon=dialog-password",
                "--id=protonpass-tray",
                f"{reason}:",
            ],
            capture_output=True,
            text=True,
            timeout=timeout + 5,
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


def ask(reason: str = "Proton Pass lock code") -> str:
    for source in (from_tray, from_tty, from_dialog, from_systemd):
        code = source(reason)
        if code:
            return code
    raise NoPrompt(
        "no way to ask for the lock code here: no tray is running, stdin is not a terminal, "
        "no password dialog is installed, and systemd-ask-password gave nothing"
    )
