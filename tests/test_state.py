"""The cache: that it survives nonsense, and that the countdown is a floor and not a claim."""

import time
import unittest

from common import Harness

from protonpass_tray.state import Lock, State


class Countdown(Harness):
    def test_unlocked_counts_down(self):
        st = State(lock="unlocked", since=time.time(), idle_timeout=600)
        left = st.expires_in()
        self.assertIsNotNone(left)
        self.assertTrue(590 <= left <= 600, left)
        self.assertIn("locks itself", st.describe())

    def test_never_negative(self):
        st = State(lock="unlocked", since=time.time() - 10_000, idle_timeout=600)
        self.assertEqual(st.expires_in(), 0)

    def test_no_countdown_without_a_timeout(self):
        self.assertIsNone(State(lock="unlocked", since=time.time()).expires_in())

    def test_no_countdown_when_locked(self):
        st = State(lock="locked", since=time.time(), idle_timeout=600)
        self.assertIsNone(st.expires_in())
        self.assertIn("code", st.describe())


class Persistence(Harness):
    def test_round_trip(self):
        self.state.record(Lock.LOCKED, source="a test", idle_timeout=600)
        again = self.state.load()
        self.assertIs(again.status, Lock.LOCKED)
        self.assertEqual(again.idle_timeout, 600)

    def test_the_timeout_survives_a_state_change(self):
        self.state.record(Lock.UNLOCKED, source="setup", idle_timeout=600)
        self.state.record(Lock.LOCKED, source="lock")
        self.assertEqual(self.state.load().idle_timeout, 600)

    def test_a_corrupt_cache_is_unknown_not_a_crash(self):
        path = self.runtime / "protonpass-tray" / "state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        self.assertIs(self.state.load().status, Lock.UNKNOWN)

    def test_a_field_from_a_future_version_is_ignored(self):
        path = self.runtime / "protonpass-tray" / "state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"lock": "locked", "invented_later": 7}')
        self.assertIs(self.state.load().status, Lock.LOCKED)

    def test_missing_runtime_dir_does_not_raise(self):
        self.state.record(Lock.UNLOCKED, source="a test")
        self.assertIsNotNone(self.state.load())


if __name__ == "__main__":
    unittest.main()
