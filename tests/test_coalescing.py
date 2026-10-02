"""Coalescing contract: arrivals during a running turn merge into ONE
follow-up turn (a single batch), never N queued runs."""

import asyncio
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tests  # noqa: F401
import bot


class FakeTyping:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeChannel:
    def typing(self):
        return FakeTyping()


class Coalescing(unittest.TestCase):
    def test_buffer_drains_as_single_followup(self):
        delivered = []  # [(messages, reply)]

        async def fake_deliver(key, channel, messages, reply, inbox, status):
            delivered.append((list(messages), reply))

        async def noop(*a, **k):
            return None

        key = "coalesce-test"
        q = bot.get_queue(key)
        q.running = True
        q.buffer = [("m2", "prompt two", []), ("m3", "prompt three", [])]
        inbox = tempfile.mkdtemp(prefix="dbot-inbox-")

        async def run():
            with mock.patch.object(bot, "acquire_turn_slot", noop), \
                 mock.patch.object(bot, "release_turn_slot", noop), \
                 mock.patch.object(bot, "update_presence", noop), \
                 mock.patch.object(bot, "deliver_reply", fake_deliver), \
                 mock.patch.object(bot, "run_opencode",
                                   lambda *a, **k: ("reply text", "sid-9")):
                await bot.run_batches(key, FakeChannel(), q,
                                      [("m1", "prompt one", [])],
                                      inbox, None)

        asyncio.run(run())

        # first turn + exactly one follow-up batch carrying both arrivals
        self.assertEqual(len(delivered), 2)
        self.assertEqual(delivered[0][0], ["m1"])
        self.assertEqual(delivered[1][0], ["m2", "m3"])
        self.assertFalse(q.running)
        self.assertEqual(q.buffer, [])

    def test_idle_queue_returns_same_instance(self):
        self.assertIs(bot.get_queue("same-key"), bot.get_queue("same-key"))

    def test_followup_combines_prompts(self):
        seen = {}

        def sync_run(prompt, session_key, files=None, model=None):
            seen["prompt"] = prompt
            return "ok", "sid-1"

        async def fake_deliver(key, channel, messages, reply, inbox, status):
            pass

        async def noop(*a, **k):
            return None

        key = "coalesce-combine"
        q = bot.get_queue(key)
        q.running = True
        q.buffer = [("m2", "second", []), ("m3", "third", [])]

        async def run():
            with mock.patch.object(bot, "acquire_turn_slot", noop), \
                 mock.patch.object(bot, "release_turn_slot", noop), \
                 mock.patch.object(bot, "update_presence", noop), \
                 mock.patch.object(bot, "deliver_reply", fake_deliver), \
                 mock.patch.object(bot, "run_opencode", sync_run):
                await bot.run_batches(key, FakeChannel(), q,
                                      [("m1", "first", [])],
                                      tempfile.mkdtemp(), None)

        asyncio.run(run())
        # follow-up prompt joins the buffered prompts with the separator
        self.assertIn("second\n\n---\n\nthird", seen["prompt"])


if __name__ == "__main__":
    unittest.main()
