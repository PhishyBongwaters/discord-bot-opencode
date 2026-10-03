"""TTS hash cache contract (issue #9)."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tests  # noqa: F401
import bot


class FakeResp:
    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n=-1):
        return self._body


def fake_tts(body=b"WAV" * 500):
    calls = []

    def _urlopen(req, timeout=None):
        calls.append(req)
        return FakeResp(body)

    return calls, _urlopen


class CacheKey(unittest.TestCase):
    def test_deterministic(self):
        self.assertEqual(bot._tts_cache_key("p", "hi"),
                         bot._tts_cache_key("p", "hi"))

    def test_sensitive_to_profile_and_text(self):
        self.assertNotEqual(bot._tts_cache_key("p1", "hi"),
                            bot._tts_cache_key("p2", "hi"))
        self.assertNotEqual(bot._tts_cache_key("p", "hi"),
                            bot._tts_cache_key("p", "hi "))


class CacheGetPut(unittest.TestCase):
    def setUp(self):
        self.cdir = tempfile.mkdtemp(prefix="dbot-ttscache-")
        self._dir = bot.VOICEBOX_CACHE_DIR
        self._files = bot.VOICEBOX_CACHE_FILES
        self._mb = bot.VOICEBOX_CACHE_MB
        bot.VOICEBOX_CACHE_DIR = self.cdir

    def tearDown(self):
        bot.VOICEBOX_CACHE_DIR = self._dir
        bot.VOICEBOX_CACHE_FILES = self._files
        bot.VOICEBOX_CACHE_MB = self._mb

    def test_miss_then_hit(self):
        self.assertIsNone(bot._tts_cache_get("p", "hello"))
        bot._tts_cache_put("p", "hello", b"WAV" * 500)
        self.assertEqual(bot._tts_cache_get("p", "hello"), b"WAV" * 500)

    def test_corrupt_entry_deleted_never_served(self):
        bot._tts_cache_put("p", "hello", b"WAV" * 500)
        # corrupt it in place (shorter than the not-audio guard)
        path = next(Path(self.cdir).glob("*.wav"))
        path.write_bytes(b"junk")
        self.assertIsNone(bot._tts_cache_get("p", "hello"))
        self.assertFalse(path.exists())

    def test_disabled_cache_is_noop(self):
        bot.VOICEBOX_CACHE_DIR = ""
        bot._tts_cache_put("p", "hello", b"WAV" * 500)  # no raise, no write
        self.assertIsNone(bot._tts_cache_get("p", "hello"))

    def test_put_prunes_to_file_cap(self):
        bot.VOICEBOX_CACHE_FILES = 2
        bot.VOICEBOX_CACHE_MB = 0
        for i in range(3):
            bot._tts_cache_put("p", f"text {i}", b"WAV" * 500)
        remaining = list(Path(self.cdir).glob("*.wav"))
        self.assertEqual(len(remaining), 2)
        # oldest ("text 0") pruned
        self.assertIsNone(bot._tts_cache_get("p", "text 0"))
        self.assertIsNotNone(bot._tts_cache_get("p", "text 2"))


class TtsWavCleanCache(unittest.TestCase):
    def setUp(self):
        self.cdir = tempfile.mkdtemp(prefix="dbot-ttscache-")
        self._dir = bot.VOICEBOX_CACHE_DIR
        self._url = bot.VOICEBOX_URL
        bot.VOICEBOX_CACHE_DIR = self.cdir
        bot.VOICEBOX_URL = "http://voicebox.test"

    def tearDown(self):
        bot.VOICEBOX_CACHE_DIR = self._dir
        bot.VOICEBOX_URL = self._url

    def test_second_identical_call_skips_voicebox(self):
        calls, urlopen = fake_tts()
        with mock.patch.object(bot, "get_voicebox_profile_id",
                               return_value="prof-1"), \
             mock.patch.object(bot, "get_voicebox_profile_engine",
                               return_value=None), \
             mock.patch("urllib.request.urlopen", urlopen):
            first = bot.tts_wav_clean("hello there")
            second = bot.tts_wav_clean("hello there")
        self.assertEqual(len(calls), 1)  # second call was a cache hit
        self.assertEqual(first, second)
        self.assertEqual(first, b"WAV" * 500)

    def test_different_text_regenerates(self):
        calls, urlopen = fake_tts()
        with mock.patch.object(bot, "get_voicebox_profile_id",
                               return_value="prof-1"), \
             mock.patch.object(bot, "get_voicebox_profile_engine",
                               return_value=None), \
             mock.patch("urllib.request.urlopen", urlopen):
            bot.tts_wav_clean("one")
            bot.tts_wav_clean("two")
        self.assertEqual(len(calls), 2)

    def test_different_profile_regenerates(self):
        calls, urlopen = fake_tts()
        profiles = ["prof-1", "prof-2"]
        with mock.patch.object(bot, "get_voicebox_profile_id",
                               side_effect=profiles), \
             mock.patch.object(bot, "get_voicebox_profile_engine",
                               return_value=None), \
             mock.patch("urllib.request.urlopen", urlopen):
            bot.tts_wav_clean("same text")
            bot.tts_wav_clean("same text")
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
