#!/usr/bin/env python3
"""A stand-in for `pass-cli`, implementing only its session lock, for the tests.

Modelled on the real 2.5.1 wording, because the wording IS the interface passcli.py reads.
State lives in $STUB_STATE (JSON); every invocation is appended to $STUB_LOG, which is how a
test can assert that something made no call at all.

  STUB_REQUIRE_TTY=1   refuse a code that arrives on a pipe, the way a CLI reading /dev/tty
                       would. This is what exercises the pty fallback in feed_code.
  STUB_NO_SESSION=1    behave like a logged-out install.
  STUB_UNREADABLE=1    for a `session` subcommand, print the prompt and then fail saying
                       nothing either side recognises. Scoped to `session` because that is
                       the real case: reading the session state went on working throughout,
                       which is how the cache came to hold the right answer all along while
                       the widget acted on the wrong one.
  STUB_REFUSE_TTY=1    the inverse of STUB_REQUIRE_TTY: refuse a code that arrives on a
                       TERMINAL. Nothing real does this - it is how a test reaches the stdin
                       fallback at all, since the pty is tried first and normally wins.
  STUB_FAIL=<message>  fail a `session` subcommand after reading the code, with one of
                       pass-cli's own failure lines (e.g. "Error locking session").
  STUB_STALE_CLIENT=1  the client's local view has lost the lock the server is enforcing.
                       `info` denies it, `unlock` and `remove-lock` answer "Session is not
                       locked", `lock` answers "Session has no lock" - and `vault list` and
                       `create-lock` still refuse because the SERVER has it locked. The exact
                       deadlock measured on a live session on 2026-11-11, 22:08 to 23:29.
"""

import json
import os
import select
import sys
import termios

STATE = os.environ.get("STUB_STATE", "")
LOG = os.environ.get("STUB_LOG", "")
CODE = os.environ.get("STUB_CODE", "1234")


def read_from_controlling_terminal():
    """Read one line from /dev/tty, the way a password prompt does.

    Modelled as a real READ and not as a comparison of names: os.ttyname() of a descriptor
    opened on /dev/tty answers "/dev/tty" rather than the pty behind it, so comparing it with
    stdin's name can never match and would call every implementation broken.

    Three outcomes, which is what makes this deterministic wherever the suite runs. No
    controlling terminal at all: the open fails. A controlling terminal that is NOT the one
    the caller wrote to - the runner's own, inherited - : nothing to read, and the timeout
    says so instead of hanging on a terminal someone is sitting at. The caller's pty, with
    the code already in it: read.
    """
    try:
        fd = os.open("/dev/tty", os.O_RDWR)
    except OSError:
        return None
    try:
        if os.environ.get("STUB_FLUSH_FIRST") == "1":
            # What a password reader does: turn echo off with TCSAFLUSH, which DISCARDS
            # whatever is already queued. A caller that wrote the code before this ran has
            # just lost it, and then both sides wait for each other.
            attrs = termios.tcgetattr(fd)
            attrs[3] &= ~termios.ECHO
            termios.tcsetattr(fd, termios.TCSAFLUSH, attrs)
        os.write(fd, b"Enter lock code: ")
        if not select.select([fd], [], [], 2.0)[0]:
            return None
        return os.read(fd, 256).decode(errors="replace").splitlines()[0].strip()
    except (OSError, IndexError):
        return None
    finally:
        os.close(fd)


def load():
    try:
        with open(STATE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"has_lock": False, "locked": False, "idle_timeout": 0}


def save(state):
    with open(STATE, "w") as fh:
        json.dump(state, fh)


def read_code():
    # Two ways a real CLI can refuse a code, both seen in the wild and both reproduced here.
    if os.environ.get("STUB_REQUIRE_TTY") == "1" and not sys.stdin.isatty():
        print("Error reading lock code", file=sys.stderr)
        raise SystemExit(1)
    if os.environ.get("STUB_REFUSE_TTY") == "1" and sys.stdin.isatty():
        print("Error reading lock code", file=sys.stderr)
        raise SystemExit(1)
    if os.environ.get("STUB_REQUIRE_CTTY") == "1":
        # What pass-cli does: the code is read from the CONTROLLING terminal, so a pty handed
        # over as stdin is not enough on its own.
        code = read_from_controlling_terminal()
        if code is None:
            print("Error reading lock code", file=sys.stderr)
            raise SystemExit(1)
        return code
    line = sys.stdin.readline()
    if not line:
        print("Error reading lock code", file=sys.stderr)
        raise SystemExit(1)
    return line.strip()


args = sys.argv[1:]
if LOG:
    with open(LOG, "a") as fh:
        fh.write(" ".join(args) + "\n")

if os.environ.get("STUB_HANG") == "1":
    # A pass-cli waiting on a terminal nobody is watching.
    import time

    time.sleep(120)

if os.environ.get("STUB_UNREADABLE") == "1" and args[:1] == ["session"]:
    # The shape pass-cli 2.5.1 produced on 2026-11-11: the prompt, and then a failure whose
    # wording is in neither the binary's strings nor this stub's. Everything a caller can see
    # is the prompt it printed on the way in - which is how "Enter lock code:" came to be
    # displayed as the reason an unlock failed.
    sys.stdout.write("Enter lock code: ")
    sys.stdout.flush()
    raise SystemExit(1)

if os.environ.get("STUB_FAIL") and args[:1] == ["session"]:
    # Read the code first, the way pass-cli does: these failures come from the API, after it
    # has the code, so a caller must not be told to ask for the code again. `session lock`
    # takes no code, and reading one there would wait for a line nobody is going to send.
    if args[1:2] != ["lock"]:
        read_code()
    print(os.environ["STUB_FAIL"], file=sys.stderr)
    raise SystemExit(1)

if os.environ.get("STUB_NO_SESSION") == "1":
    print("No active session", file=sys.stderr)
    raise SystemExit(1)

state = load()

if args[:1] == ["info"]:
    print("- Release track: stable")
    print("- Email: someone@proton.me")
    if os.environ.get("STUB_STALE_CLIENT") == "1":
        # The client's own copy of the session knows nothing about the lock the server is
        # enforcing - which is what made `info` useless as evidence.
        print("- Session has lock: no")
        raise SystemExit(0)
    if state["has_lock"]:
        print(f"- Session has lock: yes ({state['idle_timeout']}s)")
    else:
        print("- Session has lock: no")
    raise SystemExit(0)

if args[:2] == ["vault", "list"]:
    if state["locked"]:
        print("Session is locked. Please unlock your session and try again.", file=sys.stderr)
        raise SystemExit(1)
    print("- [shareid]: jimi-secrets")
    raise SystemExit(0)

if args[:2] == ["session", "create-lock"]:
    if state["locked"]:
        # What the real 2.5.1 did on 2026-11-11, and the reason an evening went nowhere: a
        # locked session refuses create-lock too, so when `info` wrongly reports no lock the
        # only operation a face offers is also the only one that cannot run.
        print("Session is locked. Please unlock your session and try again.", file=sys.stderr)
        raise SystemExit(1)
    if state["has_lock"]:
        print("Session already has a lock", file=sys.stderr)
        raise SystemExit(1)
    first = read_code()
    second = read_code()
    if first != second:
        print("Lock codes do not match", file=sys.stderr)
        raise SystemExit(1)
    timeout = 300
    if "--idle-timeout" in args:
        timeout = int(args[args.index("--idle-timeout") + 1])
    save({"has_lock": True, "locked": False, "idle_timeout": timeout, "code": first})
    print("Session locked successfully")
    raise SystemExit(0)

if args[:2] == ["session", "lock"]:
    if os.environ.get("STUB_STALE_CLIENT") == "1":
        print(
            "Session has no lock. Create one first with `pass-cli session create-lock`",
            file=sys.stderr,
        )
        raise SystemExit(1)
    if not state["has_lock"]:
        print(
            "Session has no lock. Create one first with `pass-cli session create-lock`",
            file=sys.stderr,
        )
        raise SystemExit(1)
    state["locked"] = True
    save(state)
    print("Session locked successfully")
    raise SystemExit(0)

if args[:2] == ["session", "unlock"]:
    if os.environ.get("STUB_STALE_CLIENT") == "1":
        print("Session is not locked", file=sys.stderr)
        raise SystemExit(1)
    if not state["has_lock"]:
        print(
            "Session has no lock. Create one first with `pass-cli session create-lock`",
            file=sys.stderr,
        )
        raise SystemExit(1)
    if not state["locked"]:
        print("Session is not locked", file=sys.stderr)
        raise SystemExit(1)
    if read_code() != state.get("code", CODE):
        print("Error unlocking session", file=sys.stderr)
        raise SystemExit(1)
    state["locked"] = False
    save(state)
    print("Session unlocked successfully")
    raise SystemExit(0)

if args[:2] == ["session", "remove-lock"]:
    if os.environ.get("STUB_STALE_CLIENT") == "1":
        # It reads the code first and refuses anyway, which is what made this a dead end: the
        # operator types the right PIN and is told the session is not locked.
        read_code()
        print("Error: Session is not locked", file=sys.stderr)
        raise SystemExit(1)
    if read_code() != state.get("code", CODE):
        print("Error removing session lock", file=sys.stderr)
        raise SystemExit(1)
    save({"has_lock": False, "locked": False, "idle_timeout": 0})
    print("Session lock removed successfully")
    raise SystemExit(0)

print(f"stub pass-cli: unsupported {args}", file=sys.stderr)
raise SystemExit(64)
