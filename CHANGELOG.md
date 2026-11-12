# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - unreleased

### Measured

- A locked session does **not** disable `pass-cli ssh-agent`. `ssh` keeps working and every
  key stays listed, in both states; the documented warning is about refreshing keys, not
  signing.
- **`pass-cli info` can report `Session has lock: no` while the session is locked.** Proved on
  a live session: `session create-lock` refused with `Session is locked. Please unlock your
  session and try again.` at the same time as `info` denied any lock. So `info`'s lock line is
  not evidence, and `refresh()` always probes with `vault list` instead of trusting it.
- **The lock does not remove itself.** Settled: the code set at 10:44 unlocked the session at
  23:17, 12.5 hours and many idle periods later. The earlier "removes itself" note came from
  one `info` reading and was an artefact of the bug above. The lock is a standing gate.
- What the timeout counts is settled, from the binary's own strings: `- Session has lock: yes
  (locks after …)`, `Session will lock after <n> seconds of inactivity` and the session field
  `session_lock_after_seconds` all say the session **locks** after that much **inactivity**.
  The `--idle-timeout` help line saying "auto-unlocks" is a slip in one string, and it had
  been read here as corroboration for the self-removal above. It is not.
- **pass-cli 2.7.0 does not touch the session lock.** Checked on 2026-11-12, the day it was
  installed here: all sixteen literals this engine classifies on are present, and the lock
  vocabulary is identical in both wording and count - down to the wrong "auto-unlocks" help
  line and the single `set_session_lock_after_seconds` setter. What 2.7.0 adds is an item
  cache and a core-event cursor, and the locked-session refusal now appears in those new
  paths too, where this engine already recognises it. Unchanged words are evidence that the
  lock code was not touched, not proof: a fix that changed where `info` reads the lock from
  would have left every string alone. `just check-strings` is what re-runs this check.

### Added

- **The Qt tray icon** (`protonpass-tray tray`, extra `[tray]`). StatusNotifierItem, so it
  works on Plasma, XFCE, LXQt, Cinnamon and most Wayland panels. It is also the prompt of last
  resort for the rest of the machine: a hook with no terminal reaches it over a socket in
  `$XDG_RUNTIME_DIR` and gets a dialog.
- **A Plasma 6 widget** (`plasmoid/`), installable from store.kde.org. It asks for no code of
  its own: QML cannot write a file or open a socket, so the code would have had to travel
  through a command line.
- **A GNOME Shell extension** (`gnome-extension/`), for the desktop that has no tray at all.
  The PIN is typed in the panel menu and piped to the engine's stdin.
- `--code-file` and `--code-stdin` on every subcommand that needs the code, for a front-end
  that cannot prompt.
- A graphical fallback in the prompt chain: `kdialog`, `zenity` or `qarma` when there is a
  display but no terminal and no tray. None of them is a dependency.
- A `justfile` with recipes for both desktop faces, their store artefacts, and
  `install-data` for packagers. `just` rather than `make`: packaging goes through PEP 517
  either way, so the only readers are contributors, and `just test -k lock` passes its
  argument through where the make equivalent needed `ARGS="..."` across an expansion
  layer that stripped quotes.

### Changed

- A failed operation answers in **pass-cli's own words** when it has any, and only falls back
  to describing the state when it does not. Describing the state instead turned `Error locking
  session` into `No lock configured` — true, and no answer at all to someone who had just
  asked for a lock to be created.
- The state a failed operation was trying to *leave* is no longer reported as its outcome, and
  no failure exits 0. Both are read by a face deciding what to offer next: answering a failed
  `setup` with "no lock is configured" sent a widget back to the button that had just failed,
  and a refused `remove-lock` on an unlocked session had exited 0.
- Seven of pass-cli's failure lines are now recognised by name (`Error locking session`,
  `Error persisting session`, `Error unlocking session`, `Error removing session lock` and the
  three shapes of `Error {creating,sending} … session … request`), read off the 2.5.1 binary.
  Each used to arrive as "unrecognised".
- "pass-cli could not read the code" is no longer classified as a wrong code. It is the one
  case the stdin fallback exists for, and calling it a wrong code both told the user the wrong
  thing and suppressed that retry — which therefore had never run.
- The idle timeout is parsed with its unit, and from both shapes pass-cli prints it in.
  `(locks after 5m)` had been read as five seconds, which expires a countdown at once and has
  every face infer a lock that has not engaged.
- The five operations moved into `actions.py`, so the command line and the tray are the same
  program rather than two that agree for now.
- `pass-cli`'s path is read on every call instead of once at import, because a tray started by
  systemd and a shell hook do not share an environment.
- A failed operation establishes the state for real before answering, instead of leaving the
  cache as it was. One extra API call, on a path that has already failed, and it is what lets
  a face recover by itself rather than wait to be restarted.
- `status --json` aside, the exit codes are the interface both desktop faces now read: 10, 11
  and 12 say what the state *is*, so neither of them reports one as a fault any more.

### Fixed

- **An unlock that could not be read reported pass-cli's prompt as the reason.** `Enter lock
  code: ` carries no newline, so it is the first thing in every captured exchange and any real
  message continues the same line. A Plasma widget spent an evening showing a red
  `Enter lock code:` as the error — for a session whose lock had removed itself, so there was
  nothing to unlock and nothing had gone wrong.
- **A failed operation left the cached state untouched,** so the widget went on offering an
  Unlock field for a lock that no longer existed. No PIN could ever work, and nothing said
  why. This was the bug behind three earlier attempts at the symptoms.
- **A timeout was retried down a channel `pass-cli` ignores,** spending the timeout twice and
  leaving a widget spinning for a minute. It also masked a real bug: the pty path could not
  answer a *silent* confirmation prompt at all — only the first code was ever sent — because
  the stdin fallback writes both lines at once and quietly covered for it.
- Whether a failure was a wrong code is decided by the command that ran, not by scraping the
  output for the word "unlock". The output of a failed unlock is the prompt, which does not
  contain it.
- `$XDG_RUNTIME_DIR/protonpass-tray` is forced to `0700`. `mkdir(mode=…)` applies only to a
  directory it creates, and this one is usually already there — found at `0775`.

## [0.1.0] - 2026-11-11

### Added

- `protonpass-tray status|lock|unlock|setup|remove-lock`, a front-end over `pass-cli session`
  that never puts the lock code in `argv` and never prints a secret.
- Lock state cached in `$XDG_RUNTIME_DIR`, so every face agrees without any of them asking the
  Proton Pass API.
