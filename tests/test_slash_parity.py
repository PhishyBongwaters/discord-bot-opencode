"""Slash parity (issue #12): the shared handle_text_command dispatch."""

import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tests  # noqa: F401
import bot


class FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, text):
        self.sent.append(text)


class FakeAuthor:
    def __init__(self, id):
        self.id = id


class FakeMessage:
    def __init__(self, author_id="1"):
        self.author = FakeAuthor(author_id)
        self.channel = FakeChannel()
        self.guild = None
        self.attachments = []


class Dispatch(unittest.TestCase):
    def setUp(self):
        self._admin = bot.ADMIN_USER_IDS
        self._sessions = dict(bot.SESSIONS)
        bot.ADMIN_USER_IDS = {"1"}

    def tearDown(self):
        bot.ADMIN_USER_IDS = self._admin
        bot.SESSIONS.clear()
        bot.SESSIONS.update(self._sessions)

    def test_help(self):
        m = FakeMessage()
        self.assertTrue(asyncio.run(bot.handle_text_command(m, "!help", "k")))
        self.assertTrue(any("!new" in s for s in m.channel.sent))
        self.assertTrue(any("/say" in s for s in m.channel.sent))

    def test_new_admin(self):
        m = FakeMessage("1")
        bot.SESSIONS["k"] = "sid-x"
        self.assertTrue(asyncio.run(bot.handle_text_command(m, "!new", "k")))
        self.assertNotIn("k", bot.SESSIONS)
        self.assertTrue(any("fresh opencode session" in s
                            for s in m.channel.sent))

    def test_new_denied(self):
        m = FakeMessage("9")
        self.assertTrue(asyncio.run(bot.handle_text_command(m, "!new", "k")))
        self.assertTrue(any("not permitted" in s for s in m.channel.sent))

    def test_unknown_returns_false(self):
        m = FakeMessage()
        self.assertFalse(
            asyncio.run(bot.handle_text_command(m, "!frobnicate", "k")))
        self.assertEqual(m.channel.sent, [])

    def test_status_builds(self):
        m = FakeMessage()
        self.assertTrue(
            asyncio.run(bot.handle_text_command(m, "!status", "k")))
        self.assertTrue(any("voice" in s for s in m.channel.sent))

    def test_voiceprofile_shows_current(self):
        m = FakeMessage()
        self.assertTrue(
            asyncio.run(bot.handle_text_command(m, "!voiceprofile", "k")))
        self.assertTrue(any("Computer" in s for s in m.channel.sent))


class SlashAdapter(unittest.TestCase):
    def _interaction(self, user_id="1"):
        inter = mock.Mock()
        inter.user = FakeAuthor(user_id)
        inter.guild = None
        inter.channel = None
        inter.response.defer = self._defer
        inter.followup.send = self._followup_send
        return inter

    def setUp(self):
        self.deferred = []
        self.sent = []

        async def _defer(ephemeral=False):
            self.deferred.append(ephemeral)

        async def _followup_send(text, ephemeral=False):
            self.sent.append((text, ephemeral))

        self._defer = _defer
        self._followup_send = _followup_send
        self._admin = bot.ADMIN_USER_IDS
        bot.ADMIN_USER_IDS = {"1"}

    def tearDown(self):
        bot.ADMIN_USER_IDS = self._admin

    def test_message_shape(self):
        inter = self._interaction()
        msg = bot._SlashMessage(inter)
        self.assertIs(msg.author, inter.user)
        self.assertIsNone(msg.guild)
        self.assertEqual(msg.attachments, [])
        asyncio.run(msg.channel.send("hi"))
        self.assertEqual(self.sent, [("hi", True)])

    def test_run_slash_defers_then_dispatches(self):
        inter = self._interaction()
        asyncio.run(bot._run_slash(inter, "!help"))
        self.assertEqual(self.deferred, [True])
        self.assertTrue(any("!new" in s for s, _ in self.sent))
        self.assertTrue(all(ephemeral for _, ephemeral in self.sent))

    def test_new_cmd_end_to_end(self):
        inter = self._interaction("1")
        asyncio.run(bot.new_cmd(inter))
        self.assertTrue(any("fresh opencode session" in s
                            for s, _ in self.sent))

    def test_denied_is_ephemeral(self):
        inter = self._interaction("9")
        asyncio.run(bot.say_cmd(inter, "hello"))
        self.assertTrue(any("not permitted" in s for s, _ in self.sent))
        self.assertTrue(all(ephemeral for _, ephemeral in self.sent))


if __name__ == "__main__":
    unittest.main()
