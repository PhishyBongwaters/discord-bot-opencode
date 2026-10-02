"""Inbox pruning, safe_key, and session-state persistence."""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tests  # noqa: F401
import bot


class SafeKey(unittest.TestCase):
    def test_sanitizes(self):
        self.assertEqual(bot.safe_key("dm:1234/ab c"), "dm_1234_ab_c")
        self.assertEqual(bot.safe_key("guild_1-chan_2"), "guild_1-chan_2")


class PruneInbox(unittest.TestCase):
    def test_count_cap_keeps_newest(self):
        inbox = Path(tempfile.mkdtemp(prefix="dbot-inbox-"))
        now = time.time()
        for i in range(5):
            p = inbox / f"f{i}.bin"
            p.write_bytes(b"x")
            os.utime(p, (now - (5 - i) * 10, now - (5 - i) * 10))
        with mock.patch.object(bot, "ATTACH_KEEP_FILES", 3), \
             mock.patch.object(bot, "ATTACH_KEEP_MB", 0):
            bot.prune_inbox(inbox, key="t")
        remaining = sorted(p.name for p in inbox.iterdir())
        self.assertEqual(remaining, ["f2.bin", "f3.bin", "f4.bin"])

    def test_zero_disables(self):
        inbox = Path(tempfile.mkdtemp(prefix="dbot-inbox-"))
        for i in range(4):
            (inbox / f"f{i}").write_bytes(b"x")
        with mock.patch.object(bot, "ATTACH_KEEP_FILES", 0), \
             mock.patch.object(bot, "ATTACH_KEEP_MB", 0):
            bot.prune_inbox(inbox, key="t")
        self.assertEqual(len(list(inbox.iterdir())), 4)

    def test_missing_dir_is_noop(self):
        bot.prune_inbox(Path(tempfile.mkdtemp()) / "nope", key="t")  # no raise


class RememberSession(unittest.TestCase):
    def setUp(self):
        self._known = {k: list(v) for k, v in bot.KNOWN.items()}

    def tearDown(self):
        bot.KNOWN.clear()
        bot.KNOWN.update(self._known)

    def test_dedup_moves_to_end(self):
        bot.KNOWN.clear()
        bot.remember_session("k", "s1")
        bot.remember_session("k", "s2")
        bot.remember_session("k", "s1")
        self.assertEqual(bot.KNOWN["k"], ["s2", "s1"])

    def test_caps_at_30(self):
        bot.KNOWN.clear()
        for i in range(35):
            bot.remember_session("k", f"s{i}")
        self.assertEqual(len(bot.KNOWN["k"]), 30)
        self.assertEqual(bot.KNOWN["k"][0], "s5")

    def test_empty_sid_ignored(self):
        bot.KNOWN.clear()
        bot.remember_session("k", "")
        self.assertNotIn("k", bot.KNOWN)


class LoadSaveState(unittest.TestCase):
    def test_legacy_flat_migration(self):
        tmp = Path(tempfile.mkdtemp()) / "sessions.json"
        tmp.write_text(json.dumps({"k1": "sid-1", "k2": "sid-2"}))
        with mock.patch.object(bot, "STATE_FILE", tmp):
            state = bot.load_state()
        self.assertEqual(state["active"], {"k1": "sid-1", "k2": "sid-2"})
        self.assertEqual(state["known"], {"k1": ["sid-1"], "k2": ["sid-2"]})
        self.assertEqual(state["models"], {})
        self.assertEqual(state["voice"], {})

    def test_missing_file_defaults(self):
        tmp = Path(tempfile.mkdtemp()) / "nope.json"
        with mock.patch.object(bot, "STATE_FILE", tmp):
            state = bot.load_state()
        self.assertEqual(state,
                         {"active": {}, "known": {}, "models": {}, "voice": {}})

    def test_save_roundtrip(self):
        tmp = Path(tempfile.mkdtemp()) / "sub" / "sessions.json"
        with mock.patch.object(bot, "STATE_FILE", tmp), \
             mock.patch.object(bot, "SESSIONS", {"k": "sid"}), \
             mock.patch.object(bot, "KNOWN", {"k": ["sid"]}), \
             mock.patch.object(bot, "MODEL_OVERRIDES", {"k": "m"}), \
             mock.patch.object(bot, "VOICE_OVERRIDES", {"k": True}):
            bot.save_state()
            state = bot.load_state()
        self.assertEqual(state["active"], {"k": "sid"})
        self.assertEqual(state["voice"], {"k": True})


if __name__ == "__main__":
    unittest.main()
