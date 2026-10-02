"""Proactive text outbox (issue #13)."""

import asyncio
import collections
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


class FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, text):
        self.sent.append(text)


class TestTextTarget(unittest.TestCase):
    def test_parses(self):
        self.assertEqual(bot.text_target(Path("12345_nightly.txt")), 12345)

    def test_no_underscore(self):
        self.assertIsNone(bot.text_target(Path("12345.txt")))

    def test_bad_id(self):
        self.assertIsNone(bot.text_target(Path("abc_label.txt")))


class TestProcessTextFile(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self._rate = dict(bot.TEXT_RATE)
        self._fail = dict(bot.TEXT_FAIL_AT)
        bot.TEXT_RATE.clear()
        bot.TEXT_FAIL_AT.clear()

    def tearDown(self):
        bot.TEXT_RATE.clear()
        bot.TEXT_FAIL_AT.clear()
        bot.TEXT_RATE.update(self._rate)
        bot.TEXT_FAIL_AT.update(self._fail)
        self.tmp.cleanup()

    def _drop(self, name, data):
        p = self.dir / name
        p.write_bytes(data)
        return p

    def _client(self, channel=None, fetch=None):
        stub = mock.MagicMock()
        stub.get_channel.return_value = channel
        if fetch is not None:
            stub.fetch_channel = mock.AsyncMock(return_value=fetch)
        else:
            stub.fetch_channel = mock.AsyncMock(
                side_effect=RuntimeError("404"))
        return stub

    async def test_delivers_and_consumes(self):
        p = self._drop("12345_note.txt", b"hello there")
        ch = FakeChannel()
        with mock.patch.object(bot, "client", self._client(ch)):
            self.assertTrue(await bot.process_text_file(p))
        self.assertEqual(ch.sent, ["hello there"])

    async def test_fetch_channel_fallback(self):
        p = self._drop("99_note.txt", b"hi")
        ch = FakeChannel()
        with mock.patch.object(bot, "client", self._client(None, ch)):
            self.assertTrue(await bot.process_text_file(p))
        self.assertEqual(ch.sent, ["hi"])

    async def test_bad_name_consumed(self):
        p = self._drop("nope.txt", b"hello")
        ch = FakeChannel()
        with mock.patch.object(bot, "client", self._client(ch)):
            self.assertTrue(await bot.process_text_file(p))
        self.assertEqual(ch.sent, [])

    async def test_empty_consumed(self):
        p = self._drop("12345_note.txt", b"   \n")
        ch = FakeChannel()
        with mock.patch.object(bot, "client", self._client(ch)):
            self.assertTrue(await bot.process_text_file(p))
        self.assertEqual(ch.sent, [])

    async def test_truncates_instead_of_skipping(self):
        p = self._drop("12345_note.txt", b"x" * 100)
        ch = FakeChannel()
        with mock.patch.object(bot, "client", self._client(ch)), \
             mock.patch.object(bot, "TEXT_MAX_BYTES", 10):
            self.assertTrue(await bot.process_text_file(p))
        self.assertEqual(len(ch.sent), 1)
        self.assertTrue(ch.sent[0].endswith("(truncated, file exceeded TEXT_MAX_BYTES)"))
        self.assertIn("xxxxxxxxxx", ch.sent[0])

    async def test_rate_cap_holds_excess(self):
        ch = FakeChannel()
        with mock.patch.object(bot, "client", self._client(ch)), \
             mock.patch.object(bot, "TEXT_MAX_PER_MINUTE", 1):
            p1 = self._drop("7_a.txt", b"one")
            p2 = self._drop("7_b.txt", b"two")
            self.assertTrue(await bot.process_text_file(p1))
            self.assertFalse(await bot.process_text_file(p2))
        self.assertEqual(ch.sent, ["one"])

    async def test_rate_window_slides(self):
        bot.TEXT_RATE[7] = collections.deque([time.monotonic() - 61])
        ch = FakeChannel()
        with mock.patch.object(bot, "client", self._client(ch)), \
             mock.patch.object(bot, "TEXT_MAX_PER_MINUTE", 1):
            p = self._drop("7_a.txt", b"one")
            self.assertTrue(await bot.process_text_file(p))
        self.assertEqual(ch.sent, ["one"])

    async def test_backoff_after_failure(self):
        p = self._drop("12345_note.txt", b"hello")
        bot.TEXT_FAIL_AT[str(p)] = time.monotonic()
        ch = FakeChannel()
        with mock.patch.object(bot, "client", self._client(ch)):
            self.assertFalse(await bot.process_text_file(p))
        self.assertEqual(ch.sent, [])

    async def test_unreachable_channel_backs_off(self):
        p = self._drop("12345_note.txt", b"hello")
        with mock.patch.object(bot, "client", self._client(None, None)):
            self.assertFalse(await bot.process_text_file(p))
        self.assertIn(str(p), bot.TEXT_FAIL_AT)


if __name__ == "__main__":
    unittest.main()
