"""Say-queue voice selection: <profile>__ file prefix."""

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tests  # noqa: F401
import bot


class FakeVC:
    def is_connected(self):
        return True


class SayVoicePrefix(unittest.TestCase):
    def test_profile_prefix(self):
        self.assertEqual(bot.say_voice_profile("nicole__hey.txt"), "nicole")
        self.assertEqual(bot.say_voice_profile("Nicole__hey.txt"), "Nicole")

    def test_guild_plus_profile(self):
        self.assertEqual(bot.say_voice_profile("123_nicole__hey.txt"),
                         "nicole")

    def test_plain_names_default(self):
        for name in ("hello.txt", "123_hello.txt", "123__x.txt",
                     "nicole_hey.txt", "12345__.txt"):
            self.assertIsNone(bot.say_voice_profile(name), name)


class SayVoiceFallback(unittest.TestCase):
    def test_unknown_profile_uses_default(self):
        with mock.patch.object(bot, "_resolve_profile_name",
                               return_value=None), \
             mock.patch.object(bot, "get_voicebox_profile_id",
                               return_value="default-pid"), \
             mock.patch.object(bot, "tts_wav_clean_pid",
                               return_value=b"WAV") as synth:
            self.assertEqual(bot.tts_wav_profile("hi", "nosuchvoice"), b"WAV")
            synth.assert_called_once_with("hi", "default-pid")

    def test_named_profile_used(self):
        with mock.patch.object(bot, "_resolve_profile_name",
                               return_value="nicole-pid"), \
             mock.patch.object(bot, "tts_wav_clean_pid",
                               return_value=b"WAV") as synth:
            self.assertEqual(bot.tts_wav_profile("hi", "Nicole"), b"WAV")
            synth.assert_called_once_with("hi", "nicole-pid")


class SayFileVoice(unittest.TestCase):
    def setUp(self):
        self.saydir = Path(tempfile.mkdtemp(prefix="dbot-sayvoice-"))

    def test_profile_file_spoken_as_profile(self):
        p = self.saydir / "123_nicole__line.txt"
        p.write_bytes(b"hello there")
        queued = []

        async def fake_vc_say(gid, wav, inbox, epoch=None):
            queued.append((gid, wav))
            return True

        with mock.patch.object(bot, "guild_voice_client",
                               return_value=FakeVC()), \
             mock.patch.object(bot, "tts_wav_profile",
                               return_value=b"WAVBYTES") as tts, \
             mock.patch.object(bot, "tts_wav",
                               side_effect=AssertionError("default used")), \
             mock.patch.object(bot, "vc_say", fake_vc_say):
            self.assertTrue(asyncio.run(bot.process_say_file(p)))
        tts.assert_called_once_with("hello there", "nicole")
        self.assertEqual(queued, [(123, b"WAVBYTES")])


if __name__ == "__main__":
    unittest.main()
