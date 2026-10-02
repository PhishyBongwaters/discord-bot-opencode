"""Prompt assembly: bridge note, model-arg resolution, usage extraction."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tests  # noqa: F401
import bot


class FakeChannel:
    def __init__(self, guild=None):
        self.guild = guild


class FakeGuild:
    id = 123


class FakeVC:
    def __init__(self, connected=True):
        self._c = connected

    def is_connected(self):
        return self._c


class BridgeNote(unittest.TestCase):
    def setUp(self):
        self._overrides = dict(bot.VOICE_OVERRIDES)
        bot.VOICE_OVERRIDES.clear()

    def tearDown(self):
        bot.VOICE_OVERRIDES.clear()
        bot.VOICE_OVERRIDES.update(self._overrides)

    def test_voice_off_returns_base_note(self):
        self.assertEqual(bot.bridge_note_for("k", FakeChannel()),
                         bot.BRIDGE_NOTE)

    def test_voice_on_no_vc_mentions_audio_file(self):
        bot.VOICE_OVERRIDES["k"] = True
        note = bot.bridge_note_for("k", FakeChannel())
        self.assertIn("as an attached audio file", note)
        self.assertTrue(note.startswith(bot.BRIDGE_NOTE))

    def test_voice_on_connected_vc(self):
        bot.VOICE_OVERRIDES["k"] = True
        with mock.patch.object(bot, "guild_voice_client",
                               return_value=FakeVC(True)):
            note = bot.bridge_note_for("k", FakeChannel(FakeGuild()))
        self.assertIn("live in the voice channel", note)


class ResolveModelArg(unittest.TestCase):
    ITEMS = ["openai/gpt-5", "anthropic/claude-opus-4-6"]

    def test_empty_shows(self):
        self.assertEqual(bot.resolve_model_arg("", self.ITEMS),
                         ("show", None))

    def test_clear(self):
        self.assertEqual(bot.resolve_model_arg("clear", self.ITEMS),
                         ("clear", None))
        self.assertEqual(bot.resolve_model_arg("Default", self.ITEMS),
                         ("clear", None))

    def test_set_known(self):
        self.assertEqual(bot.resolve_model_arg("openai/gpt-5", self.ITEMS),
                         ("set", "openai/gpt-5"))

    def test_unknown_with_list_errors(self):
        action, value = bot.resolve_model_arg("nope/x", self.ITEMS)
        self.assertEqual(action, "error")
        self.assertIn("unknown model", value)

    def test_no_list_requires_provider_slash(self):
        action, value = bot.resolve_model_arg("gpt-5", [])
        self.assertEqual(action, "error")
        self.assertIn("provider/model", value)
        self.assertEqual(bot.resolve_model_arg("x/y", []), ("set", "x/y"))


class ExtractUsage(unittest.TestCase):
    def test_sums_step_finish_events(self):
        ndjson = (
            '{"part": {"type": "step-start"}}\n'
            'not json at all\n'
            '{"part": {"type": "step-finish", "tokens": {"input": 10, '
            '"output": 5}, "cost": 0.01}}\n'
            '{"part": {"type": "step-finish", "tokens": {"input": 3}, '
            '"cost": 0.02}}\n'
            '{"no part": true}\n'
        )
        tin, tout, cost = bot.extract_usage(ndjson)
        self.assertEqual((tin, tout), (13, 5))
        self.assertAlmostEqual(cost, 0.03)

    def test_empty(self):
        self.assertEqual(bot.extract_usage(""), (0, 0, 0.0))


if __name__ == "__main__":
    unittest.main()
