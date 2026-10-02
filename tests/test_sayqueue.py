"""say-queue targeting + consume/hold/speak/backoff contract."""

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


class SayTargets(unittest.TestCase):
    def test_guild_prefix(self):
        self.assertEqual(bot.say_targets("123456_hello.txt"), [123456])

    def test_plain_broadcasts(self):
        self.assertEqual(bot.say_targets("hello.txt"), [])
        self.assertEqual(bot.say_targets("no-prefix-here.txt"), [])


class ProcessSayFile(unittest.TestCase):
    def setUp(self):
        self._fail_at = dict(bot.SAY_FAIL_AT)
        bot.SAY_FAIL_AT.clear()
        self.saydir = Path(tempfile.mkdtemp(prefix="dbot-say-"))

    def tearDown(self):
        bot.SAY_FAIL_AT.clear()
        bot.SAY_FAIL_AT.update(self._fail_at)

    def _write(self, name, data):
        p = self.saydir / name
        p.write_bytes(data)
        return p

    def test_oversize_consumed_without_tts(self):
        p = self._write("big.txt", b"x" * (bot.SAY_MAX_BYTES + 1))
        with mock.patch.object(bot, "tts_wav",
                               side_effect=AssertionError("must not TTS")):
            self.assertTrue(asyncio.run(bot.process_say_file(p)))

    def test_empty_consumed(self):
        p = self._write("empty.txt", b"   \n")
        with mock.patch.object(bot, "tts_wav",
                               side_effect=AssertionError("must not TTS")):
            self.assertTrue(asyncio.run(bot.process_say_file(p)))

    def test_hold_when_nobody_in_vc(self):
        p = self._write("hold.txt", b"hello there")
        with mock.patch.object(bot, "guild_voice_client", return_value=None):
            self.assertFalse(asyncio.run(bot.process_say_file(p)))
        self.assertTrue(p.exists())  # held, not consumed

    def test_speak_when_live(self):
        # "<guildid>_" prefix targets that guild directly (no client.guilds
        # needed under the stub).
        p = self._write("123_speak.txt", b"hello there")
        queued = []

        async def fake_vc_say(gid, wav, inbox, epoch=None):
            queued.append((gid, wav))
            return True

        with mock.patch.object(bot, "guild_voice_client",
                               return_value=FakeVC()), \
             mock.patch.object(bot, "tts_wav", return_value=b"WAVBYTES"), \
             mock.patch.object(bot, "vc_say", fake_vc_say):
            self.assertTrue(asyncio.run(bot.process_say_file(p)))
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0][1], b"WAVBYTES")

    def test_tts_failure_backs_off(self):
        # Contract: every scan still attempts TTS, but the failure is
        # recorded in SAY_FAIL_AT and the warning is throttled — an
        # immediate retry keeps the original timestamp (no log spam).
        p = self._write("123_retry.txt", b"hello there")
        calls = []
        with mock.patch.object(bot, "guild_voice_client",
                               return_value=FakeVC()), \
             mock.patch.object(bot, "tts_wav",
                               side_effect=lambda t: calls.append(t) or None):
            self.assertFalse(asyncio.run(bot.process_say_file(p)))
            first_at = bot.SAY_FAIL_AT[str(p)]
            self.assertGreater(first_at, 0)
            self.assertFalse(asyncio.run(bot.process_say_file(p)))
            self.assertEqual(bot.SAY_FAIL_AT[str(p)], first_at)
        self.assertEqual(len(calls), 2)  # TTS attempted each scan


if __name__ == "__main__":
    unittest.main()
