"""A throwaway machine for each test: a stub pass-cli, its state, and an empty runtime dir."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

STUB = Path(__file__).resolve().parent / "stub_pass_cli.py"


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.state_file = tmp / "stub-state.json"
        self.log = tmp / "stub.log"
        self.runtime = tmp / "runtime"
        self.runtime.mkdir()

        self.env = {
            "STUB_STATE": str(self.state_file),
            "STUB_LOG": str(self.log),
            "XDG_RUNTIME_DIR": str(self.runtime),
            "PROTONPASS_TRAY_PASS_CLI": str(STUB),
        }
        self._saved = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)

        # Nothing is reloaded: every module here reads its environment when called, not when
        # imported, so one import serves every test. Reloading would also hand each test its
        # own copy of the enums, and `assertIs` against them would quietly stop meaning
        # anything.
        from protonpass_tray import cli, passcli, prompt, state

        self.passcli = passcli
        self.state = state
        self.prompt = prompt
        self.cli = cli
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self._restore)

    def _restore(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # --- helpers ---------------------------------------------------------------------
    def set_stub(self, **fields):
        base = {"has_lock": False, "locked": False, "idle_timeout": 0}
        base.update(fields)
        self.state_file.write_text(json.dumps(base))

    def stub_calls(self):
        try:
            return [line for line in self.log.read_text().splitlines() if line]
        except OSError:
            return []

    def run_cli(self, *argv):
        """Run the command line and keep its output out of the test report.

        What it printed is kept in self.out / self.err, so a test can assert on the words a
        user would read instead of only on the exit code.
        """
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.cli.main(list(argv))
        self.out, self.err = out.getvalue(), err.getvalue()
        return code
