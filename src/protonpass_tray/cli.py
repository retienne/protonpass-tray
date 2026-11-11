"""`protonpass-tray` - the command line, and what every shell hook should call.

The exit codes are the interface; they are what lets a hook decide without parsing prose:

    0   unlocked, or the command did what was asked
    10  locked - a code is needed
    11  no lock is configured on this session (run `setup`)
    12  no Proton Pass session at all (run `pass-cli login`)
    3   pass-cli said something this version does not recognise
    4   pass-cli is not installed
    2   usage

`status` is free and silent: it reads the cache and asks nothing, so a hook may call it on
every ssh connection. `status --refresh` costs two API calls AND resets the server's idle
timer - see state.py. Nothing else here is implicit.

The decisions themselves are in actions.py, which the tray calls too.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import traceback

from . import __version__, actions, passcli, prompt, state
from .actions import (
    EXIT_LOCKED,
    EXIT_NO_LOCK,
    EXIT_NO_PASS_CLI,
    EXIT_NO_SESSION,
    EXIT_OK,
    EXIT_UNRECOGNISED,
    EXIT_USAGE,
)

__all__ = [
    "EXIT_LOCKED",
    "EXIT_NO_LOCK",
    "EXIT_NO_PASS_CLI",
    "EXIT_NO_SESSION",
    "EXIT_OK",
    "EXIT_UNRECOGNISED",
    "EXIT_USAGE",
    "main",
]


#: Where a failure is written down. A tray icon and a Plasma widget can show one line, and a
#: desktop face has nowhere to print a traceback at all - so anything that goes wrong leaves a
#: record here instead of only on a screen somebody has to transcribe. It holds no secrets:
#: the code never reaches argv, and passcli.py scrubs it out of every message.
LOG_LINES_KEPT = 40


def log_path() -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return os.path.join(runtime, "protonpass-tray", "failures.log")


def _log(message: str) -> None:
    try:
        path = log_path()
        directory = os.path.dirname(path)
        os.makedirs(directory, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)  # mode= applies only to a directory makedirs CREATES
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        try:
            with open(path) as fh:
                kept = fh.read().splitlines()[-(LOG_LINES_KEPT - 1) :]
        except OSError:
            kept = []
        with open(path, "w") as fh:
            fh.write("\n".join([*kept, f"{stamp}  {message}"]) + "\n")
        os.chmod(path, 0o600)
    except OSError:
        pass  # a face that cannot write its log still works


def _code(args: argparse.Namespace, reason: str) -> str:
    """Where the lock code comes from. One place, so every subcommand offers the same ways."""
    if getattr(args, "code_file", None):
        return prompt.from_file(args.code_file)
    if getattr(args, "code_stdin", False):
        return prompt.from_stdin()
    return prompt.ask(reason)


def _report(st: state.State, as_json: bool) -> None:
    if as_json:
        print(
            json.dumps(
                {
                    "lock": st.lock,
                    "since": st.since,
                    "idle_timeout": st.idle_timeout,
                    "source": st.source,
                    "expires_in": st.expires_in(),
                    "description": st.describe(),
                }
            )
        )
    else:
        print(st.describe())


def _say(outcome: actions.Outcome, as_json: bool) -> int:
    """Print an outcome the way the exit code implies, and hand the code back."""
    if outcome.state is not None and not outcome.is_error:
        _report(outcome.state, as_json)
    elif outcome.message:
        print(outcome.message, file=sys.stderr if outcome.is_error else sys.stdout)
    if outcome.exit_code != EXIT_OK:
        _log(f"exit={outcome.exit_code} {outcome.message}")
    return outcome.exit_code


def cmd_status(args: argparse.Namespace) -> int:
    if args.refresh:
        outcome = actions.refresh()
        _report(outcome.state if outcome.state else actions.status(), args.json)
        return outcome.exit_code
    st = actions.status()
    _report(st, args.json)
    return actions.exit_code_for(st.status)


def cmd_lock(args: argparse.Namespace) -> int:
    return _say(actions.lock(), args.json)


def cmd_unlock(args: argparse.Namespace) -> int:
    return _say(actions.unlock(_code(args, "Proton Pass lock code")), args.json)


def cmd_setup(args: argparse.Namespace) -> int:
    # Checked before asking for a code: a number pass-cli would reject is a mistake worth
    # catching while the person is still looking at the command they typed.
    if not 30 <= args.idle_timeout <= 900:
        print("--idle-timeout must be between 30 and 900 seconds", file=sys.stderr)
        return EXIT_USAGE
    code = _code(args, "Choose a lock code (a PIN is fine)")
    return _say(actions.create_lock(code, args.idle_timeout), args.json)


def cmd_remove_lock(args: argparse.Namespace) -> int:
    code = _code(args, "Lock code, to remove the lock")
    return _say(actions.remove_lock(code), args.json)


def cmd_tray(args: argparse.Namespace) -> int:
    try:
        from .tray import run_tray
    except ImportError as exc:  # pragma: no cover - depends on the install's extras
        print(
            f"the tray needs PyQt6, which is not installed ({exc}).\n"
            "  pipx install 'protonpass-tray[tray]'   or   apt install python3-pyqt6",
            file=sys.stderr,
        )
        return EXIT_USAGE
    return run_tray(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="protonpass-tray",
        description="Lock and unlock a Proton Pass CLI session with a code, from a tray icon "
        "or from a script. Unofficial; not affiliated with Proton AG.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subs = parser.add_subparsers(dest="command", required=True)

    def add(name: str, func, help_: str) -> argparse.ArgumentParser:
        sub = subs.add_parser(name, help=help_)
        sub.add_argument("--json", action="store_true", help="machine-readable output")
        sub.set_defaults(func=func)
        return sub

    def add_with_code(name: str, func, help_: str) -> argparse.ArgumentParser:
        """A subcommand that needs the lock code, and so offers all three ways to give it."""
        sub = add(name, func, help_)
        source = sub.add_mutually_exclusive_group()
        source.add_argument(
            "--code-file",
            metavar="PATH",
            help="read the code from this file and delete it; it must be mode 0600 and owned "
            "by you (for a front-end that can only run a command line, where argv is public)",
        )
        source.add_argument("--code-stdin", action="store_true", help="read the code from stdin")
        return sub

    status = add("status", cmd_status, "say whether the session is locked (free, from cache)")
    status.add_argument(
        "--refresh",
        action="store_true",
        help="ask pass-cli instead of the cache; this resets the server's idle timer",
    )
    add("lock", cmd_lock, "lock the session now")
    add_with_code("unlock", cmd_unlock, "unlock the session, asking for the code")
    setup = add_with_code("setup", cmd_setup, "create the lock and set how long idle is too long")
    setup.add_argument(
        "--idle-timeout",
        type=int,
        default=600,
        metavar="SECONDS",
        help="30-900, pass-cli's own limits (default: 600)",
    )
    add_with_code("remove-lock", cmd_remove_lock, "remove the lock entirely")
    add("tray", cmd_tray, "run the tray icon")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _log(f"run: {args.command}")
    try:
        return args.func(args)
    except passcli.NotInstalled as exc:
        print(f"{exc}. Install the Proton Pass CLI first.", file=sys.stderr)
        _log(f"not installed: {exc}")
        return EXIT_NO_PASS_CLI
    except prompt.NoPrompt as exc:
        print(str(exc), file=sys.stderr)
        _log(f"no prompt: {exc}")
        return EXIT_USAGE
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        # A desktop face has nowhere to show a traceback, and the one that escaped before this
        # arrived in a tray icon's error line as its first line. The whole thing goes to the
        # log; the user gets a sentence.
        _log(f"crash: {traceback.format_exc()}")
        print(f"protonpass-tray failed: {exc} (see {log_path()})", file=sys.stderr)
        return EXIT_UNRECOGNISED


if __name__ == "__main__":
    raise SystemExit(main())
