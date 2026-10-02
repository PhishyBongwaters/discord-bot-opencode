"""Readiness counter / voice auto-off contract."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tests  # noqa: F401
import bot


class ReadinessCounter(unittest.TestCase):
    def setUp(self):
        self._n = bot.VOICEBOX_FAILS["n"]
        bot.VOICEBOX_FAILS["n"] = 0

    def tearDown(self):
        bot.VOICEBOX_FAILS["n"] = self._n

    def test_three_failures_trip_auto_off_once(self):
        tripped = []
        with mock.patch.object(bot, "voice_mode_on", return_value=True), \
             mock.patch.object(bot, "voice_mode_off",
                               side_effect=lambda r: tripped.append(r) or True):
            bot.note_voicebox_result(False)
            bot.note_voicebox_result(False)
            self.assertEqual(tripped, [])          # limit is 3
            bot.note_voicebox_result(False)
            self.assertEqual(len(tripped), 1)     # tripped exactly once
            self.assertIn("3x", tripped[0])

    def test_success_resets_counter(self):
        with mock.patch.object(bot, "voice_mode_on", return_value=True), \
             mock.patch.object(bot, "voice_mode_off", return_value=True):
            bot.note_voicebox_result(False)
            bot.note_voicebox_result(False)
            bot.note_voicebox_result(True)
            self.assertEqual(bot.VOICEBOX_FAILS["n"], 0)

    def test_no_trip_when_voice_mode_already_off(self):
        with mock.patch.object(bot, "voice_mode_on", return_value=False), \
             mock.patch.object(bot, "voice_mode_off") as off:
            for _ in range(5):
                bot.note_voicebox_result(False)
            off.assert_not_called()


if __name__ == "__main__":
    unittest.main()
