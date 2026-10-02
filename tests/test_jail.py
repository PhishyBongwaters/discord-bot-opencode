"""FILE_JAIL confinement contract (issue #4, locked in by #11)."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tests  # noqa: F401
import bot


class Jail(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="dbot-jail-"))
        self.jail = self.root / "jail"
        self.jail.mkdir()
        (self.jail / "sub").mkdir()
        self._jail = bot.FILE_JAIL
        self._dir = bot.OPENCODE_DIR
        bot.FILE_JAIL = self.jail
        bot.OPENCODE_DIR = str(self.root)

    def tearDown(self):
        bot.FILE_JAIL = self._jail
        bot.OPENCODE_DIR = self._dir

    def test_inside_allowed(self):
        self.assertTrue(bot.jailed(self.jail / "sub" / "f.txt"))
        self.assertTrue(bot.jailed(self.jail))

    def test_outside_refused(self):
        self.assertFalse(bot.jailed(self.root / "escape.txt"))
        self.assertFalse(bot.jailed(Path(tempfile.gettempdir()) / "x"))

    def test_dotdot_escape_refused(self):
        raw, p = bot._resolve_candidate(str(self.jail / "sub" / ".." / ".."
                                            / "escape.txt"))
        self.assertFalse(bot.jailed(p))

    def test_resolve_target_dir(self):
        p = bot.resolve_target_dir(str(self.jail / "sub"))
        self.assertEqual(p, (self.jail / "sub").resolve())

    def test_resolve_target_dir_outside_raises(self):
        with self.assertRaises(bot.JailViolation):
            bot.resolve_target_dir(str(self.root / "nope"))

    def test_resolve_outbound_quotes_stripped(self):
        p = bot.resolve_outbound(f'"{self.jail}/sub"')
        self.assertEqual(p, (self.jail / "sub").resolve())

    def test_relative_anchored_under_opencode_dir(self):
        # relative paths anchor under OPENCODE_DIR; "jail/rel.txt"
        # resolves inside the jail
        (self.jail / "rel.txt").write_text("x")
        p = bot.resolve_outbound("jail/rel.txt")
        self.assertTrue(bot.jailed(p))
        self.assertEqual(p, (self.jail / "rel.txt").resolve())


if __name__ == "__main__":
    unittest.main()
