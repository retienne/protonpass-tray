/*
 * The GNOME face of protonpass-tray. GNOME has no system tray at all, so this is not a second
 * copy of the Qt tray - it is the only way to have an indicator here, and every decision is
 * still made by the `protonpass-tray` command this only ever calls.
 *
 * THREE THINGS WORTH KNOWING BEFORE CHANGING THIS FILE
 *
 * 1. The state comes from the engine's cache FILE, never from asking the API. The Proton Pass
 *    lock is an idle timer, so an indicator that polled for its icon would keep the session
 *    unlocked for ever, and be right. Reading
 *    $XDG_RUNTIME_DIR/protonpass-tray/state.json costs nothing and carries the countdown.
 *
 * 2. The code goes down a pipe. Unlike QML, GJS can write to a subprocess's stdin, so the PIN
 *    typed into the menu entry below reaches `protonpass-tray unlock --code-stdin` without
 *    ever appearing in argv or in the environment, where any process of this user could read
 *    it. If you replace the entry with anything, keep that property.
 *
 * 3. This code runs INSIDE gnome-shell. Everything is asynchronous for that reason, and
 *    disable() must leave nothing behind - no timer, no subprocess, no indicator - because
 *    what leaks here leaks into the user's desktop session and not into an app they can quit.
 */

import GLib from 'gi://GLib';
import Gio from 'gi://Gio';
import GObject from 'gi://GObject';
import St from 'gi://St';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

const ENGINE = 'protonpass-tray';
const TICK_SECONDS = 2;

// Symbolic, and deliberately NOT the Proton Pass application icon the way the Qt tray and the
// Plasma widget use it. GNOME's panel is monochrome by convention, a full-colour app icon with
// a badge on it looks like a mistake there, and extensions.gnome.org reviewers expect symbolic.
// The state therefore has to be carried by the shape alone, so these are three clearly
// different shapes rather than two padlocks and a variation.
// The wording is this file's own. The cache carries a state name and two numbers, not a
// sentence - a face that printed parsed['description'] showed the user the string "no-lock".
const WORDS = {
    'locked': 'Locked - enter your code',
    'unlocked': 'Unlocked',
    'no-lock': 'No lock set up',
    'no-session': 'Not signed in to Proton Pass',
    'unknown': 'Checking…',
};

const ICONS = {
    'locked': 'changes-prevent-symbolic',
    'unlocked': 'changes-allow-symbolic',
    'no-lock': 'changes-allow-symbolic',
    'no-session': 'dialog-warning-symbolic',
    'unknown': 'dialog-question-symbolic',
};

function statePath() {
    const runtime = GLib.getenv('XDG_RUNTIME_DIR') || `/run/user/${GLib.get_user_name()}`;
    return GLib.build_filenamev([runtime, 'protonpass-tray', 'state.json']);
}

/** Run the engine, optionally writing a code to its stdin. Never blocks the shell. */
function runEngine(args, code = null) {
    return new Promise((resolve) => {
        let proc;
        try {
            proc = Gio.Subprocess.new(
                [ENGINE, ...args],
                Gio.SubprocessFlags.STDOUT_PIPE |
                    Gio.SubprocessFlags.STDERR_PIPE |
                    (code !== null ? Gio.SubprocessFlags.STDIN_PIPE : 0)
            );
        } catch (e) {
            // The store can only ever deliver this half of the program, so a missing engine
            // is an ordinary state to be in and not an error to log.
            resolve({ok: false, missing: true, stderr: String(e)});
            return;
        }
        proc.communicate_utf8_async(code !== null ? `${code}\n` : null, null, (source, result) => {
            try {
                const [, stdout, stderr] = source.communicate_utf8_finish(result);
                resolve({
                    ok: source.get_exit_status() === 0,
                    status: source.get_exit_status(),
                    stdout: stdout ?? '',
                    stderr: stderr ?? '',
                });
            } catch (e) {
                resolve({ok: false, stderr: String(e)});
            }
        });
    });
}

const Indicator = GObject.registerClass(
class Indicator extends PanelMenu.Button {
    _init() {
        super._init(0.5, 'Proton Pass session lock');

        this._lock = 'unknown';
        this._idleTimeout = 0;
        this._since = 0;
        this._missing = false;
        this._busy = false;

        this._icon = new St.Icon({
            icon_name: ICONS['unknown'],
            style_class: 'system-status-icon',
        });
        this.add_child(this._icon);

        this._status = new PopupMenu.PopupMenuItem('', {reactive: false});
        this.menu.addMenuItem(this._status);

        // The code is typed here and piped to the engine; see (2) in the file header.
        this._entryItem = new PopupMenu.PopupBaseMenuItem({reactive: false, can_focus: false});
        this._entry = new St.Entry({
            hint_text: 'Lock code',
            can_focus: true,
            x_expand: true,
        });
        this._entry.clutter_text.set_password_char('●');
        this._entry.clutter_text.connect('activate', () => this._unlock());
        this._entryItem.add_child(this._entry);
        this.menu.addMenuItem(this._entryItem);

        this._unlockItem = this._item('Unlock', () => this._unlock());
        this._lockItem = this._item('Lock now', () => this._act(['lock']));
        this._setupItem = this._item('Create a lock (10 minutes)', () =>
            this._act(['setup', '--idle-timeout', '600']));
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._checkItem = this._item('Check now', () => this._act(['status', '--refresh']));

        this.menu.connect('open-state-changed', (menu, open) => {
            if (open) {
                // Opening the menu re-checks. Not a poll: a deliberate act by someone looking
                // at the indicator, who expects it to be current. Without it a lock created in
                // a terminal would stay invisible until the next login.
                this._act(['status', '--refresh']);
                if (this._lock === 'locked')
                    this._entry.grab_key_focus();
            } else {
                this._entry.set_text('');
            }
        });

        this._readState();
        // The one call made without being asked: at start-up nothing is known.
        this._act(['status', '--refresh']);
    }

    _item(label, callback) {
        const item = new PopupMenu.PopupMenuItem(label);
        item.connect('activate', callback);
        this.menu.addMenuItem(item);
        return item;
    }

    async _act(args, code = null) {
        if (this._busy)
            return;
        this._busy = true;
        this._draw();
        const result = await runEngine(args, code);
        this._missing = Boolean(result.missing);
        this._busy = false;
        this._readState();
        // 10 locked, 11 no lock, 12 not signed in: the engine's way of saying what the state
        // is, not that something broke. Notifying about those turns an ordinary answer into an
        // alert - and the state is what this extension's own menu already shows.
        const stateCode = [0, 10, 11, 12].includes(result.status);
        if (!result.ok && !result.missing && !stateCode && result.stderr)
            Main.notify('Proton Pass', result.stderr.trim().split('\n')[0]);
    }

    _unlock() {
        const code = this._entry.get_text();
        this._entry.set_text('');
        if (code === '')
            return;
        this.menu.close();
        this._act(['unlock', '--code-stdin'], code);
    }

    _readState() {
        const file = Gio.File.new_for_path(statePath());
        file.load_contents_async(null, (source, result) => {
            let text = '';
            try {
                const [, contents] = source.load_contents_finish(result);
                text = new TextDecoder().decode(contents);
            } catch {
                // No cache yet is an ordinary state, not a failure.
                this._draw();
                return;
            }
            try {
                const parsed = JSON.parse(text);
                this._lock = parsed['lock'] ?? 'unknown';
                this._idleTimeout = parsed['idle_timeout'] ?? 0;
                this._since = parsed['since'] ?? 0;
            } catch {
                this._lock = 'unknown';
            }
            this._draw();
        });
    }

    _expiresIn() {
        if (this._lock !== 'unlocked' || this._idleTimeout <= 0 || this._since <= 0)
            return -1;
        const left = Math.floor(this._since + this._idleTimeout - Date.now() / 1000);
        return left > 0 ? left : 0;
    }

    /** Local only: age the countdown, and infer the lock when it runs out. No API call. */
    tick() {
        this._readState();
        if (this._lock === 'unlocked' && this._expiresIn() === 0) {
            this._lock = 'locked';
            this._draw();
        }
    }

    _draw() {
        this._icon.icon_name = ICONS[this._lock] ?? ICONS['unknown'];
        this._icon.opacity = this._busy ? 128 : 255;

        const left = this._expiresIn();
        let text = WORDS[this._lock] ?? WORDS['unknown'];
        if (this._missing)
            text = 'protonpass-tray is not installed';
        else if (left > 0)
            text = `Unlocked - locks itself in ${Math.floor(left / 60)}m ${left % 60}s`;
        this._status.label.text = text;

        const known = !this._missing;
        this._entryItem.visible = known && (this._lock === 'locked' || this._lock === 'unknown');
        this._unlockItem.visible = this._entryItem.visible;
        this._lockItem.visible = known && this._lock === 'unlocked';
        this._setupItem.visible = known && this._lock === 'no-lock';
        this._checkItem.visible = known;
    }
});

export default class ProtonPassLockExtension extends Extension {
    enable() {
        this._indicator = new Indicator();
        Main.panel.addToStatusArea(this.uuid, this._indicator);
        this._timer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, TICK_SECONDS, () => {
            this._indicator?.tick();
            return GLib.SOURCE_CONTINUE;
        });
    }

    disable() {
        // Nothing may outlive the extension: this runs in the shell's own process, and on
        // GNOME it also runs every time the screen locks.
        if (this._timer) {
            GLib.Source.remove(this._timer);
            this._timer = null;
        }
        this._indicator?.destroy();
        this._indicator = null;
    }
}
