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
- **pass-cli 2.7.1 fixes the deadlock at its source.** Read off Proton's published source on
  2026-11-23, and seen on a live session on 2026-11-24: with `vault list` refused as locked,
  `session unlock` asked for the code and unlocked, and `session lock` on the locked session
  answered "Session is already locked" - every string this engine matches on is in the binary
  too (`just check-strings`, 18 of 18). The session store's lock value is now
  shared between its clones (`Arc<RwLock<…>>`, where it had been a per-clone copy written
  back stale), and `lock`, `unlock`, `remove-lock`, `create-lock` and `info` each ask
  `GET /pass/v1/user/session/lock/check` before acting instead of trusting the local value.
  A locked session answers that check with the API's "session locked" code, so `unlock`
  asks for the code on exactly the session 2.7.0 refused to. The `--idle-timeout` help now
  says the session *locks* after that much inactivity. Every literal this engine classifies
  on is still there; two are new, below.

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

- **A waiting unlock prompt ends when the session is unlocked from somewhere else.** Seen on
  2026-11-25: `protonpass-tray unlock` waiting in a terminal, the code typed into the widget,
  and the terminal still asking. Every prompt (terminal, the tray's dialog, kdialog, zenity,
  systemd) now watches the state file, at no API call, and gives up once it says "unlocked"
  with a time after the prompt began; the command then exits 0. What was half typed is
  discarded rather than left for the shell. It cannot reach `pass-cli session unlock`, which
  waits on its own read of the terminal, nor learn of an unlock made there until a face
  refreshes.

- **`protonpass-tray set-timeout SECONDS`**, and the same in the Qt tray's menu and the Plasma
  widget: change how long idle is too long, keeping the code. pass-cli has no command for it,
  so the lock is removed and created again with the code the removal has just proved right.
  A creation that fails is tried once more, and if the session is left with no lock the
  message says so. Only on an unlocked session. Not in the GNOME extension yet.

- **The Plasma widget says when something is waiting for the code**: a notification naming the
  program and terminal, which opens the popup with the cursor in the code field. It comes
  about five seconds after the prompt, so not to someone answering it, once per prompt, and
  it goes when the prompt does. A waiting prompt announces itself in
  `$XDG_RUNTIME_DIR/protonpass-tray/asking/` and is believed only while it keeps rewriting
  its file. It names a program and never a command line, which can hold secrets. Only
  `protonpass-tray unlock` does this: `pass-cli session unlock` tells nobody it is waiting.
  The Qt tray needs no such thing, as it shows the dialog itself. Not in the GNOME extension.
- **`protonpass-tray unlock --wait SECONDS`**, for a script with no terminal: it asks nothing
  itself, has the widget say what is waiting, and waits for an unlock from there. Not a hang:
  it fails at once unless a face is running (the widget touches `listening`, every five
  seconds) and, for five minutes after a wait nobody answered, fails at once again.

### Changed

- The icon is our own: a terminal in the panel's text colour, the state in its screen (closed
  padlock, open padlock, "!" for no lock, a dimmed prompt for unknown), with the Proton Pass
  mark as a badge in its top-right corner where the application is installed. It had been the
  mark itself, and an unlocked session looked exactly like the Proton Pass application beside
  it in the panel. The mark is still referenced from the user's installation, never shipped.
  In the Qt tray and the Plasma widget; the GNOME extension keeps its symbolic padlock.
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
- pass-cli 2.7.1's two new sentences are recognised. `Session is already locked` (from `lock`,
  exit 0) is a lock, not an unreadable answer. `Error checking session lock` - the server
  check failing before any code is read - is a failure on every command, and in particular
  not a wrong code on `unlock`, which is what a non-zero exit in unknown words reads as there.
- `setup` on a locked session answers EXIT_LOCKED and "unlock it first" again on 2.7.1, which
  says `Session already has a lock` there where 2.5.1 said `Session is locked`. Taken at its
  word it was a usage error telling the operator to remove a lock they could not yet reach.
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

- The tray survived no caller that left while its dialog was up: answering a connection that
  had already been deleted raised in a Qt slot, which ends the process.
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
