"""The two desktop front-ends, checked as data.

Neither can be exercised here - one needs a Plasma panel, the other runs inside gnome-shell -
so what is testable is the part that silently rots: the metadata three files have to agree
about, and the two security properties that are only properties as long as nobody adds a
convenience to the QML.
"""

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLASMOID = ROOT / "plasmoid"
GNOME = ROOT / "gnome-extension"
APPSTREAM_ID = "io.github.retienne.protonpass-tray"


def strip_comments(source: str) -> str:
    """Enough comment stripping for QML and JS to look at the CODE alone.

    A line comment is only taken as one where it is not part of a URL, so that the `file://`
    in the QML survives - this is a test helper, not a parser, and it only has to avoid
    hiding something from the assertion.
    """
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"(?<!:)//[^\n]*", "", source)


def package_version() -> str:
    text = (ROOT / "pyproject.toml").read_text()
    return re.search(r'^version = "([^"]+)"', text, re.M).group(1)


class Versions(unittest.TestCase):
    """One release, four files. They drift the moment nothing compares them."""

    def test_the_python_package_and_its_module_agree(self):
        module = (ROOT / "src" / "protonpass_tray" / "__init__.py").read_text()
        self.assertIn(f'__version__ = "{package_version()}"', module)

    def test_the_plasmoid_agrees(self):
        meta = json.loads((PLASMOID / "metadata.json").read_text())
        self.assertEqual(meta["KPlugin"]["Version"], package_version())

    def test_the_gnome_extension_agrees(self):
        meta = json.loads((GNOME / "metadata.json").read_text())
        self.assertEqual(meta["version-name"], package_version())


class PlasmoidMetadata(unittest.TestCase):
    def setUp(self):
        self.meta = json.loads((PLASMOID / "metadata.json").read_text())

    def test_it_is_a_plasma_6_applet(self):
        self.assertEqual(self.meta["KPackageStructure"], "Plasma/Applet")
        self.assertIn("X-Plasma-API-Minimum-Version", self.meta)

    def test_the_id_matches_the_appstream_component(self):
        self.assertEqual(self.meta["KPlugin"]["Id"], APPSTREAM_ID)

    def test_it_declares_the_licence(self):
        self.assertEqual(self.meta["KPlugin"]["License"], "MIT")

    def test_the_main_qml_is_where_plasma_looks(self):
        self.assertTrue((PLASMOID / "contents" / "ui" / "main.qml").is_file())


class GnomeMetadata(unittest.TestCase):
    def setUp(self):
        self.meta = json.loads((GNOME / "metadata.json").read_text())

    def test_the_uuid_is_reverse_dns_and_matches_nothing_else(self):
        self.assertEqual(self.meta["uuid"], "protonpass-tray@retienne.github.io")

    def test_shell_versions_are_strings_of_plausible_releases(self):
        versions = self.meta["shell-version"]
        self.assertTrue(versions)
        for version in versions:
            self.assertIsInstance(version, str)
            self.assertTrue(version.isdigit() or "." in version, version)
            self.assertGreaterEqual(int(version.split(".")[0]), 45, "ESM extensions are 45+")

    def test_the_description_says_what_is_needed(self):
        # The store delivers this file and nothing else, so the description is the only place
        # a user learns that a command has to be installed too.
        self.assertIn("protonpass-tray", self.meta["description"])

    def test_it_is_an_esm_extension(self):
        source = (GNOME / "extension.js").read_text()
        self.assertIn("export default class", source)
        self.assertNotIn("imports.ui", source, "the pre-45 import system was removed")


class HowEachFaceHandsOverTheCode(unittest.TestCase):
    """Each face does it the only safe way its toolkit allows, and the difference between them
    is the whole reason these are asserted rather than trusted to stay put."""

    def test_the_plasma_widget_never_puts_the_code_in_an_argument(self):
        """The widget takes the code in its own popup, which is where a person expects to
        type it. QML can neither write a file nor open a socket, so it reaches the engine on
        stdin through a pipe - `printf | protonpass-tray --code-stdin`. That leaves it in the
        argv of one short-lived shell, which is a deliberate trade (see the file header); what
        must never happen is the code becoming an argument of protonpass-tray itself, where it
        would sit in `ps` for the whole run and in any shell history that captured it.
        """
        qml = (PLASMOID / "contents" / "ui" / "main.qml").read_text()
        self.assertIn("--code-stdin", qml)
        self.assertIn("printf '%s' ", qml)
        self.assertNotIn("--code-file", qml)
        # The code is interpolated exactly once, into the printf, and quoted there.
        self.assertEqual(qml.count("root.quoted(code)"), 1)

    def test_the_plasma_widget_quotes_the_code(self):
        """A code containing a single quote would otherwise end the quoting and run as a
        command, in a shell this widget invoked. Asserted as a shape rather than as an exact
        escape sequence: what matters is that one function does the quoting and that every
        interpolation goes through it."""
        qml = (PLASMOID / "contents" / "ui" / "main.qml").read_text()
        body = qml[qml.index("function quoted(") :]
        body = body[: body.index("\n    }")]
        self.assertIn("split", body)
        self.assertIn("join", body)
        self.assertIn("\\", body, "nothing in it escapes anything")
        # No other path may build a command out of a code.
        self.assertEqual(qml.count("quoted("), 2, "declared once, used once")

    def test_the_plasma_widget_clears_the_field(self):
        qml = (PLASMOID / "contents" / "ui" / "main.qml").read_text()
        for field in ("unlockField", "pinField", "pinAgain"):
            self.assertIn(f'{field}.text = "";', qml, field)

    def test_the_gnome_extension_pipes_the_code(self):
        """GJS can write to stdin, so there the code is typed in the panel and piped."""
        source = (GNOME / "extension.js").read_text()
        self.assertIn("--code-stdin", source)
        self.assertIn("set_password_char", source)
        self.assertNotIn("--code-file", source)

    def test_no_front_end_spawns_anything_but_the_engine(self):
        """A face that ran pass-cli directly would be a second implementation of the
        classifying - the part that has the tests - and the two would agree only at first.

        Checked at the CALL SITES rather than by searching the file, because both faces
        legitimately say "pass-cli" to the user: telling someone to run `pass-cli login` is
        the correct advice when there is no session, and an earlier version of this test
        failed on that sentence.
        """
        qml = (PLASMOID / "contents" / "ui" / "main.qml").read_text()
        for call in re.findall(r"shell\.exec\((.*?)\);", qml, re.S):
            cleaned = call.replace("root.engine", "").replace("protonpass-tray", "")
            self.assertNotIn("pass-cli", cleaned, f"this runs pass-cli: {call.strip()[:60]}")

        js = (GNOME / "extension.js").read_text()
        spawned = re.findall(r"Gio\.Subprocess\.new\(\s*\[([^\]]*)\]", js, re.S)
        self.assertTrue(spawned, "the spawn call moved; this test no longer checks anything")
        for argv in spawned:
            self.assertTrue(
                argv.strip().startswith("ENGINE"), f"spawns something else: {argv.strip()}"
            )


if __name__ == "__main__":
    unittest.main()


class SharedIcons(unittest.TestCase):
    """The badges exist twice, because the widget is distributed on its own and cannot reach
    into a Python package for them. Twice is fine; silently different is not."""

    def test_the_plasmoid_badges_match_the_package_ones(self):
        for name in ("locked", "unlocked", "alert"):
            engine = ROOT / "src" / "protonpass_tray" / "icons" / f"emblem-{name}.svg"
            widget = PLASMOID / "contents" / "icons" / f"emblem-{name}.svg"
            self.assertTrue(widget.is_file(), widget)
            self.assertEqual(engine.read_bytes(), widget.read_bytes(), name)

    def test_no_proton_artwork_is_shipped(self):
        """The Proton icon is referenced from the user's own installation, never vendored -
        this project must not carry someone else's mark, least of all into two stores."""
        for path in ROOT.rglob("*"):
            if path.is_file() and ".git/" not in str(path) and "proton-pass" in path.name:
                self.assertNotIn(
                    path.suffix, (".png", ".svg", ".ico", ".jpg"), f"{path} looks vendored"
                )
