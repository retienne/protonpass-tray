#!/usr/bin/env python3
"""Does a pass-cli build still say everything the classifier matches on?

passcli.py reaches every verdict by matching pass-cli's own words, and those words are not an
interface anyone promised to keep. That module's docstring says a release which reworded them
must turn it uncertain rather than confidently wrong; this is what makes the warning
actionable. Run it against a new build before trusting a verdict from it:

    just check-strings                    # whatever `pass-cli` resolves to
    just check-strings /path/to/pass-cli

The literals are introspected from passcli.py rather than copied here, so the list cannot
drift from the code it is meant to check. A missing one is a verdict the engine can no longer
reach: it arrives as Verdict.UNKNOWN, which is the designed failure, but still a regression
worth knowing about before a user meets it.

Two deliberate details:

  * A control string that must NOT be found. A checker whose negative verdict is unreachable
    would report success against an empty file, and that is the one way this tool could lie.
  * The `info` anchors are reported, not enforced. Losing one costs the countdown in the tray,
    not a verdict - refresh() asks `vault list` about the lock and deliberately does not
    believe `info` about whether one exists - so failing on them would be a false alarm.

Checked: every literal present in 2.5.1, and in 2.7.0 on 2026-11-12, unchanged.
"""

from __future__ import annotations

import pathlib
import re
import shutil
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from protonpass_tray import passcli  # noqa: E402

#: Must not appear in any build. If it does, the search itself is broken.
CONTROL = "this string is not in any pass-cli build"

#: What the `info` parsers key on. Informational: see the module docstring.
ANCHORS = (
    "session has lock:",
    "seconds of inactivity",
    "locks after",
)


def literals() -> dict[str, str]:
    """The classifier's message table, read off the module itself."""
    return {
        name: value
        for name, value in vars(passcli).items()
        if name.startswith("_") and not name.startswith("__") and isinstance(value, str)
    }


def main(argv: list[str]) -> int:
    given = argv[0] if argv else None
    path = given or shutil.which(passcli.binary())
    if not path or not pathlib.Path(path).is_file():
        print(f"not found: {given or passcli.binary()}", file=sys.stderr)
        return 2

    print(f"reading {path}")
    # latin-1 so every byte decodes; the literals are ASCII, so a substring search over the
    # whole file finds them without needing `strings` to be installed.
    haystack = pathlib.Path(path).read_bytes().decode("latin-1").lower()

    missing = []
    for name, text in sorted(literals().items()):
        found = text.lower() in haystack
        print(f"  {'ok     ' if found else 'MISSING'}  {name:20} {text!r}")
        if not found:
            missing.append(name)

    control_found = CONTROL.lower() in haystack
    print(f"  {'BROKEN ' if control_found else 'ok     '}  {'(control)':20} must be absent")

    print("  -- anchors, informational --")
    for anchor in ANCHORS:
        hits = haystack.count(anchor.lower())
        print(f"  {'ok     ' if hits else 'absent '}  {'(anchor)':20} {anchor!r} x{hits}")
    pattern = passcli._REQUEST_FAILED_RE.pattern  # noqa: SLF001
    hits = len(re.findall(pattern, haystack, re.I))
    print(f"  {'ok     ' if hits else 'absent '}  {'(regex)':20} {pattern!r} x{hits}")

    if control_found:
        print("\nFAIL: the control string was found, so this check cannot be trusted.")
        return 1
    if missing:
        names = ", ".join(missing)
        print(f"\nFAIL: {len(missing)} literal(s) this build no longer contains: {names}")
        print("Those verdicts are now unreachable. Re-read the build and fix the table.")
        return 1
    print(f"\nOK: all {len(literals())} literals present.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
