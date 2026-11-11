# Working on protonpass-tray. Nothing here is needed to USE it
# (`pipx install protonpass-tray`): these are the steps for the test suite, for trying the two
# desktop front-ends without going through a store, and for installing the data files the way
# a distro package should.
#
# A justfile and not a Makefile. Packaging does not depend on it either way - a Python project
# is built through PEP 517, off pyproject.toml, which is what AUR, COPR and Nixpkgs all drive -
# so the only readers are contributors, and quoting that survives is worth more here than a
# runner that is already installed. `just test -k lock` passes its argument through verbatim;
# the make equivalent needed ARGS="..." across an expansion layer that ate quotes.
#
# `just` with no arguments lists every recipe, from the doc comments below.

prefix := env_var_or_default("PREFIX", "/usr")
destdir := env_var_or_default("DESTDIR", "")
python := env_var_or_default("PYTHON", "python3")

gnome_uuid := "protonpass-tray@retienne.github.io"
plasmoid_id := "io.github.retienne.protonpass-tray"
# Read once per invocation. `just --list` evaluates no assignment, so this costs nothing there.
version := `sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml`

default:
    @just --list

# Extra arguments reach unittest verbatim, so `just test -k lock` selects by name.
# Run the whole suite (no network, no Proton account, no Qt needed)
test *args:
    PYTHONPATH=src:tests {{ python }} -m unittest discover -s tests -t tests -v {{ args }}

# ruff check + format --check, as CI runs them
lint:
    ruff check .
    ruff format --check .

# Reformat
format:
    ruff format .

# Build the sdist, the wheel, the .plasmoid and the GNOME zip
dist:
    {{ python }} -m build
    just plasmoid-zip gnome-zip

# --- the Plasma widget -----------------------------------------------------------------

# Install the widget for this user (then add it to a panel)
plasmoid-install:
    kpackagetool6 --type Plasma/Applet --install plasmoid || \
      kpackagetool6 --type Plasma/Applet --upgrade plasmoid
    @echo "Added. Right-click a panel -> Add Widgets -> 'Proton Pass session lock'."
    @echo "After a change: just plasmoid-install && systemctl --user restart plasma-plasmashell"

# Load the widget offscreen and fail on a QML error (needs Plasma installed)
plasmoid-check:
    #!/usr/bin/env bash
    set -uo pipefail
    mkdir -p .check
    # The shadow engine on PATH is the point: the widget refreshes at start-up, and a check
    # must not reach the real pass-cli or the real Proton Pass session.
    cat > .check/protonpass-tray <<'STUB'
    #!/bin/sh
    case "$*" in
      *--json*) printf '{"lock":"unknown","since":0,"idle_timeout":0,"source":"check","expires_in":null,"description":"State unknown"}\n' ;;
    esac
    exit 0
    STUB
    chmod +x .check/protonpass-tray
    # Exit 124 is the timeout killing a widget that stayed up, which is what success looks
    # like here. Matched FOR, not filtered: a blocklist of known noise hid the XMLHttpRequest
    # refusal that made the widget's state permanently "unknown" - the first thing this caught.
    PATH="{{ justfile_directory() }}/.check:$PATH" QT_QPA_PLATFORM=offscreen \
      timeout -k 2 8 plasmawindowed {{ plasmoid_id }} 2>&1 \
      | grep -Ei 'error|warning|XMLHttpRequest|is not a|not defined|Unable to assign|Cannot assign|disabled by default' \
      | grep -Ev 'kf\.windowsystem|KWindowShadow|propagateSizeHints' > .check/out || true
    # The verdict is whether that file has anything in it, never the pipeline's own status:
    # the last command in it is a grep that exits 1 when it finds nothing, which is success.
    if [ -s .check/out ]; then
        echo "QML complained:"
        cat .check/out
        rm -rf .check
        exit 1
    fi
    echo "the widget loads clean"
    rm -rf .check

plasmoid-uninstall:
    kpackagetool6 --type Plasma/Applet --remove {{ plasmoid_id }}

# Build the .plasmoid for store.kde.org
plasmoid-zip:
    @mkdir -p dist
    cd plasmoid && zip -qr ../dist/protonpass-tray-{{ version }}.plasmoid . -x '.*'
    @echo "dist/protonpass-tray-{{ version }}.plasmoid"

# --- the GNOME extension ---------------------------------------------------------------

# Install the extension for this user (log out and in afterwards)
gnome-install:
    mkdir -p "$HOME/.local/share/gnome-shell/extensions/{{ gnome_uuid }}"
    cp gnome-extension/metadata.json gnome-extension/extension.js \
       "$HOME/.local/share/gnome-shell/extensions/{{ gnome_uuid }}/"
    @echo "Installed. Log out and in (Wayland), then: gnome-extensions enable {{ gnome_uuid }}"

gnome-uninstall:
    rm -rf "$HOME/.local/share/gnome-shell/extensions/{{ gnome_uuid }}"

# Build the zip to upload to extensions.gnome.org
gnome-zip:
    @mkdir -p dist
    cd gnome-extension && zip -qr ../dist/{{ gnome_uuid }}.shell-extension.zip \
       metadata.json extension.js
    @echo "dist/{{ gnome_uuid }}.shell-extension.zip"

# --- for packagers ---------------------------------------------------------------------

# PREFIX and DESTDIR are read from the environment, as a packaging recipe expects.
# Install the desktop entry, AppStream metadata, icons and user unit
install-data:
    install -Dm644 data/{{ plasmoid_id }}.desktop \
      "{{ destdir }}{{ prefix }}/share/applications/{{ plasmoid_id }}.desktop"
    install -Dm644 data/{{ plasmoid_id }}.metainfo.xml \
      "{{ destdir }}{{ prefix }}/share/metainfo/{{ plasmoid_id }}.metainfo.xml"
    install -Dm644 data/protonpass-tray.service \
      "{{ destdir }}{{ prefix }}/lib/systemd/user/protonpass-tray.service"
    install -Dm644 src/protonpass_tray/icons/protonpass-tray-locked.svg \
      "{{ destdir }}{{ prefix }}/share/icons/hicolor/scalable/apps/{{ plasmoid_id }}.svg"
