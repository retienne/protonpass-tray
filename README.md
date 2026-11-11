# protonpass-tray

Lock your [Proton Pass CLI](https://protonpass.github.io/pass-cli/) session behind a short
code, unlock it from your panel or a dialog, and let your scripts find out which it is without
a second thought.

> **Unofficial.** Not affiliated with, endorsed by, or supported by Proton AG. It drives the
> `pass-cli` binary and holds no credentials of its own.

## The problem it solves

`pass-cli` keeps an authenticated session on your machine, and every process running as you
can spend it — read any password, sign with any SSH key — for as long as it lasts. Since 2.5.x
the CLI can gate that session behind a *lock code*: a PIN-length secret, held server-side,
that re-arms itself after an idle period you choose (`--idle-timeout`, 30–900 seconds).

What the CLI has nowhere to do is **ask** you for that code, except on the terminal that
happens to be in front of it. So a locked session surfaces as a command that failed, the wrong
error in a deploy script, and an SSH key that will not sign. This fills that gap.

## One engine, three faces

The engine is a small Python program; each face is a thin presentation layer that runs it and
decides nothing itself. All three do the same five things.

| | |
| --- | --- |
| **the command** | `protonpass-tray status\|lock\|unlock\|setup\|remove-lock` — works over SSH, in a script, on a server with no desktop |
| **the Qt tray** | `protonpass-tray tray` — StatusNotifierItem, so Plasma, XFCE, LXQt, Cinnamon and most Wayland panels show it |
| **the Plasma widget** | a native KDE applet with the code field, the countdown and the setup in its popup |
| **the GNOME extension** | GNOME removed system tray icons, so there an extension is the only way to have an indicator at all |

## Install

```bash
pipx install protonpass-tray            # the command, no Qt needed
pipx install 'protonpass-tray[tray]'    # with the Qt tray icon
```

You also need `pass-cli` itself, from
[Proton's releases](https://github.com/ProtonPass/pass-cli/releases), and a session
(`pass-cli login`). Then put the lock in place:

```bash
protonpass-tray setup --idle-timeout 600   # 10 minutes; 30-900 are pass-cli's own limits
```

The code may be a four-digit PIN — the CLI enforces no minimum. Whether the API rate-limits
wrong attempts is not documented anywhere, so treat a short PIN as protection against someone
walking past your desk, not against someone scripting against your terminal.

**Either desktop face needs the engine installed too**, because neither store can deliver a
dependency: the widget and the extension each say so plainly rather than looking broken.

<details>
<summary>Running a face from a git checkout, before any store is involved</summary>

```bash
just plasmoid-install   # then: right-click a panel -> Add Widgets
just gnome-install      # then log out and in, and: gnome-extensions enable ...
protonpass-tray tray    # the Qt one, from the terminal
```
</details>

## Using it from a script

The exit codes are the interface, so a hook never has to parse prose:

| Code | Meaning |
| --- | --- |
| `0` | unlocked, or the command did what was asked |
| `10` | locked — a code is needed |
| `11` | no lock is configured (`protonpass-tray setup`) |
| `12` | no Proton Pass session at all (`pass-cli login`) |
| `3` | `pass-cli` said something this version does not recognise |
| `4` | `pass-cli` is not installed |

```bash
protonpass-tray status --refresh >/dev/null || protonpass-tray unlock || exit 1
secret="$(pass-cli item view "pass://vault/title/password")"
```

`status` alone is free and silent — it reads a cache and asks nothing, so a hook may call it
on every SSH connection. `--json` adds the idle countdown.

## Three rules the code is built on

**Nothing polls the API.** The lock is an *idle* timer, so every request resets it. A panel
icon that polled to keep its picture fresh would prevent the lock from ever engaging — while
displaying "unlocked" perfectly accurately, which is what makes that bug so hard to see. So
every face reads the same cache in `$XDG_RUNTIME_DIR`, runs the countdown locally, and infers
the lock when the countdown reaches zero. Only `--refresh`, or an action you took, costs a
call. The countdown is a floor rather than a promise: any `pass-cli` command in any terminal
resets the server's timer without telling us, so an inferred lock says so.

**The code never touches `argv` or the environment.** It goes to `pass-cli` down a pipe, or
down a pty with echo off for a release that insists on a terminal — both paths are implemented
and both are tested, because which one a given `pass-cli` needs is a property of the release.

**Every face takes the code where you are looking, and none of them passes it as an
argument.** It travels on stdin: GJS writes to the subprocess directly, and the Plasma widget
pipes it (`printf | protonpass-tray --code-stdin`), because QML can neither write a file nor
open a socket. That last one leaves the code in the argv of one short-lived shell, for a
fraction of a second — a deliberate trade, not an oversight. The alternative is for the engine
to raise a dialog of its own, which is a second window for something that belongs in the one
you already opened; and this lock defends against a person at your keyboard, not against a
process already running as you, which could read the keyring, ptrace the shell or keylog any
dialog in any case. What never happens is the code becoming an argument of `protonpass-tray`,
where `ps` would hold it for the whole run. `tests/test_frontends.py` asserts that, the
quoting, and that each field is cleared the moment it is sent.

## About the icon

Where the Proton Pass application is installed, the Qt tray and the Plasma widget use **its**
icon as their base — dimmed when the session is locked, with a small badge in the corner, so
the state reads across a panel at 22 pixels. Without it they fall back to a plain padlock from
your icon theme.

That icon is *referenced* from your own installation and never shipped here: this repository
carries no Proton artwork, and nothing that represents the project — the application icon, the
entry in Plasma's widget chooser, a store listing — uses their mark. A third-party tool may say
which service a status is about; it may not dress up as it. `tests/test_frontends.py` asserts
the first half of that.

The GNOME extension stays monochrome instead, because GNOME's panel is symbolic by convention
and a full-colour app icon with a badge looks like a mistake there.

## Publishing the two desktop faces

Both stores are manual uploads — neither has a usable API — so a tag builds the artefacts and
you upload exactly what the tag contains:

```bash
just plasmoid-zip gnome-zip     # also produced by the release workflow
```

**KDE.** Upload `dist/*.plasmoid` at [store.kde.org](https://store.kde.org/) (a KDE Identity
account, category *Plasma 6 Add-Ons → Plasma Widgets*, with screenshots). It then appears in
Plasma's **Add Widgets → Get New Widgets**, which is the KNewStuff client for that store, and
in Discover.

**GNOME.** Upload `dist/*.shell-extension.zip` at
[extensions.gnome.org/upload](https://extensions.gnome.org/upload/). A person reviews it.
`shell-version` in `metadata.json` must list the GNOME releases it has actually been tried on,
so **a new GNOME release means a new upload**, not just a new tag.

**The engine's own discoverability** is a packaging question rather than a store one: the
AppStream metainfo in `data/` is what GNOME Software and KDE Discover read, and it only
reaches them once the engine is in a distro (AUR, COPR, OBS, Nixpkgs — see `just install-data`
for what a package should install).

## What the lock actually does, measured

Proton's own documentation contradicts itself about the timer, so it was measured on a live
session on 2026-11-11 with a 600-second lock. One of the two answers is settled; the other
turned out to be the opposite of what the first run suggested.

**Settled: a locked session does not take your SSH keys away.** The documentation says only
that the agent "will not refresh the keys automatically … if the session has a lock", which
left open whether a locked session would empty it — the binary does contain `Returning empty
identity list: agent is locked` and `Refusing to sign: agent is locked`. It does not: `ssh`
connected normally and `ssh-add -l` listed every key, run in **both** states, so the result is
not an accident of timing. Those strings belong to the SSH agent's own lock (`ssh-add -x`),
which a session lock does not engage. The warning means what it says and no more: no *refresh*,
so a key added in the app will not appear until you unlock.

**What the timeout counts, settled.** pass-cli's help for `--idle-timeout` says "time before
the session **auto-unlocks**", which read literally would make the lock protection for one
timeout rather than a standing gate. Every other string in the binary says the opposite, and
they outnumber it — all four are its own, read with `strings` on 2.5.1:

| Where | What it says |
| --- | --- |
| `pass-cli info` | `- Session has lock: yes (locks after …)` |
| `pass-cli info` | `Session will lock after <n> seconds of inactivity` |
| the session field | `session_lock_after_seconds` |
| `--idle-timeout --help` | `Time in seconds before the session auto-unlocks (min 30, max 900)` |

So the timeout counts **inactivity** and the session **locks** when it runs out. The
"auto-unlocks" line is a slip in one help string, not a description of the behaviour.

**The lock is a standing gate — and `info` is not to be believed about it.** Observed over one
day on a live session:

| Time | What happened |
| --- | --- |
| 10:44:18 | lock created, 600s timeout, session unlocked |
| by 11:01:12 | a server-side check found it **locked** — so it does lock itself when idle |
| 11:01:40 | locked again by hand, after an unlock |
| 11:12:32 | `pass-cli info` reported **no lock at all** — 10m52s later, with nothing touching it |
| 22:08:18 | `session create-lock` refused: `Session is locked. Please unlock your session and try again.` — while `info` still denied any lock |
| 23:17:37 | the code set at 10:44 **unlocked it**, 12.5 hours later |

That last row settles it. The lock was in place the whole time, through many idle periods, so
it does **not** remove itself: the 11:12:32 reading was `info` misreporting the lock on a
session that was locked at the time, which the 22:08 row shows directly.

So **`info`'s `Session has lock:` line is not evidence while the session is locked.** Ask an
ordinary API call instead — this is why `refresh()` always probes with `vault list` rather than
returning early on `info`. Believing it cost an evening: every face published "no lock
configured", so each offered to create one, and pass-cli refuses `create-lock` on a locked
session too. The only control on screen was the only one that could not work, and no face
could recover by itself.

**The one case still untested** is an agent *restart* while the session is locked — a reboot,
or a systemd unit's `Restart=on-failure`. It would have to fetch the keys from the API, which a
locked session refuses, so expect an empty agent until you unlock.

## Status

0.2.0 is the engine, the Qt tray, the Plasma widget and the GNOME extension. The whole lock
cycle — create with a code, lock itself when idle, unlock with the code, lock on demand — has
been exercised end to end on KDE Plasma 6 against a real Proton Pass session. The GNOME
extension has not yet run in a live shell.

## Development

```bash
just test          # the whole suite
just test -k lock  # or by name, passed through verbatim
just lint          # ruff, as CI runs it
```

The recipes are in `justfile` ([just](https://github.com/casey/just) — `apt install just`,
`brew install just`, or a release binary). Nothing in it is needed to *use* the project.

No dependencies, no network, no account. `tests/stub_pass_cli.py` stands in for the real
binary and reproduces its wording, because the wording is the interface this code reads. The
Qt tests are skipped where PyQt6 is absent, and say so rather than passing quietly.

The QML cannot be checked in CI — resolving the Plasma imports needs Plasma installed — but on
a machine that has it:

```bash
just plasmoid-check      # loads the widget offscreen, fails on any QML error
just plasmoid-install    # then: right-click a panel -> Add Widgets
```

`plasmoid-check` shadows the engine on `PATH` with a stub, because the widget refreshes at
start-up and a check must not reach a real Proton Pass session. It exercises the package, the
imports and the full view; the compact icon is only instantiated by a real panel.

## Licence

MIT — see [LICENSE](LICENSE).
