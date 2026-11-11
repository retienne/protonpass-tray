# GNOME Shell extension

GNOME removed system tray icons, so this is the only way to have a Proton Pass lock indicator
in the GNOME panel. It calls the `protonpass-tray` command for everything; install that first
(`pipx install protonpass-tray`).

## Try it without installing from a store

```bash
(cd .. && just gnome-install)   # or, by hand:
cp -r . ~/.local/share/gnome-shell/extensions/protonpass-tray@retienne.github.io
gnome-extensions enable protonpass-tray@retienne.github.io
```

Then log out and in on Wayland (there is no way to reload the shell in place), or `Alt+F2`,
`r`, Enter on X11.

Read what it is doing with `journalctl -f -o cat /usr/bin/gnome-shell`.

## Packaging it for extensions.gnome.org

```bash
gnome-extensions pack --force gnome-extension
```

and upload the resulting zip at <https://extensions.gnome.org/upload/>. Uploads are reviewed
by a person, and `shell-version` in `metadata.json` must list the GNOME releases it has
actually been tried on - so a new GNOME means a new upload, not just a new tag.
