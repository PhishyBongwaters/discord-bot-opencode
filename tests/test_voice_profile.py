"""Per-chat voice profile (issue #10)."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tests  # noqa: F401
import bot

PROFILES = [("id-computer", "Computer"), ("id-other", "Other Voice")]


class EffectiveProfile(unittest.TestCase):
    def setUp(self):
        self._o = dict(bot.VOICE_PROFILE_OVERRIDES)
        bot.VOICE_PROFILE_OVERRIDES.clear()

    def tearDown(self):
        bot.VOICE_PROFILE_OVERRIDES.clear()
        bot.VOICE_PROFILE_OVERRIDES.update(self._o)

    def test_global_default(self):
        self.assertEqual(bot.effective_voice_profile("k"), "Computer")

    def test_override_wins(self):
        bot.VOICE_PROFILE_OVERRIDES["k"] = "Other Voice"
        self.assertEqual(bot.effective_voice_profile("k"), "Other Voice")
        self.assertEqual(bot.effective_voice_profile("other"), "Computer")


class ResolveProfileName(unittest.TestCase):
    def setUp(self):
        self._c = dict(bot._VOICE_PROFILE_CACHE)
        bot._VOICE_PROFILE_CACHE.clear()

    def tearDown(self):
        bot._VOICE_PROFILE_CACHE.clear()
        bot._VOICE_PROFILE_CACHE.update(self._c)

    def test_by_id(self):
        with mock.patch.object(bot, "list_voicebox_profiles",
                               return_value=PROFILES):
            self.assertEqual(bot._resolve_profile_name("id-other"), "id-other")

    def test_by_name_case_insensitive(self):
        with mock.patch.object(bot, "list_voicebox_profiles",
                               return_value=PROFILES):
            self.assertEqual(bot._resolve_profile_name("other voice"),
                             "id-other")

    def test_unknown_returns_none(self):
        with mock.patch.object(bot, "list_voicebox_profiles",
                               return_value=PROFILES):
            self.assertIsNone(bot._resolve_profile_name("nope"))

    def test_cached_no_refetch(self):
        calls = []
        def _list():
            calls.append(1)
            return PROFILES
        with mock.patch.object(bot, "list_voicebox_profiles", _list):
            bot._resolve_profile_name("Computer")
            bot._resolve_profile_name("Computer")
        self.assertEqual(len(calls), 1)

    def test_empty_name(self):
        with mock.patch.object(bot, "list_voicebox_profiles",
                               side_effect=AssertionError("no fetch")):
            self.assertIsNone(bot._resolve_profile_name(""))


class GetProfileId(unittest.TestCase):
    def setUp(self):
        self._o = dict(bot.VOICE_PROFILE_OVERRIDES)
        self._c = dict(bot._VOICE_PROFILE_CACHE)
        bot.VOICE_PROFILE_OVERRIDES.clear()
        bot._VOICE_PROFILE_CACHE.clear()

    def tearDown(self):
        bot.VOICE_PROFILE_OVERRIDES.clear()
        bot.VOICE_PROFILE_OVERRIDES.update(self._o)
        bot._VOICE_PROFILE_CACHE.clear()
        bot._VOICE_PROFILE_CACHE.update(self._c)

    def test_uses_override(self):
        bot.VOICE_PROFILE_OVERRIDES["k"] = "Other Voice"
        with mock.patch.object(bot, "list_voicebox_profiles",
                               return_value=PROFILES):
            self.assertEqual(bot.get_voicebox_profile_id("k"), "id-other")

    def test_falls_back_to_global(self):
        with mock.patch.object(bot, "list_voicebox_profiles",
                               return_value=PROFILES):
            self.assertEqual(bot.get_voicebox_profile_id("k"), "id-computer")
            self.assertEqual(bot.get_voicebox_profile_id(None), "id-computer")


if __name__ == "__main__":
    unittest.main()
