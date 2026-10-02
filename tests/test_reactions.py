"""Reaction controls (issue #7)."""

import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tests  # noqa: F401
import bot


class FakeUser:
    def __init__(self, id, bot=False, roles=None):
        self.id = id
        self.bot = bot
        if roles is not None:
            self.roles = roles


class FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, file=None):
        self.sent.append((content, file))


class FakeMessage:
    def __init__(self, id, author, channel=None, guild=None):
        self.id = id
        self.author = author
        self.channel = channel or FakeChannel()
        self.guild = guild


class FakeGuild:
    def __init__(self, id):
        self.id = id


class FakeReaction:
    def __init__(self, emoji, message):
        self.emoji = emoji
        self.message = message


class FakeVC:
    def __init__(self, connected=True):
        self._c = connected

    def is_connected(self):
        return self._c


class ReactionAllowed(unittest.TestCase):
    def test_author(self):
        self.assertTrue(bot._reaction_allowed(FakeUser(7), 7))

    def test_dj(self):
        with mock.patch.object(bot, "ADMIN_USER_IDS", {"1"}), \
             mock.patch.object(bot, "DJ_USER_IDS", {"8"}):
            self.assertTrue(bot._reaction_allowed(FakeUser(8), 7))

    def test_denied(self):
        with mock.patch.object(bot, "ADMIN_USER_IDS", {"1"}):
            self.assertFalse(bot._reaction_allowed(FakeUser(9), 7))


class RecordReply(unittest.TestCase):
    def setUp(self):
        self._r = dict(bot.REPLY_MSGS)
        bot.REPLY_MSGS.clear()

    def tearDown(self):
        bot.REPLY_MSGS.clear()
        bot.REPLY_MSGS.update(self._r)

    def test_records(self):
        sent = FakeMessage(100, FakeUser(0, bot=True))
        prompt_msg = FakeMessage(50, FakeUser(7))
        bot.REGEN_PROMPTS["k"] = "do thing"
        try:
            bot._record_reply(sent, "k", [prompt_msg], "the reply")
        finally:
            bot.REGEN_PROMPTS.pop("k", None)
        info = bot.REPLY_MSGS[100]
        self.assertEqual(info["key"], "k")
        self.assertEqual(info["author_id"], 7)
        self.assertEqual(info["text"], "the reply")
        self.assertEqual(info["prompt"], "do thing")

    def test_cap(self):
        for i in range(55):
            bot._record_reply(FakeMessage(i, FakeUser(0, bot=True)),
                              "k", [], "r")
        self.assertEqual(len(bot.REPLY_MSGS), 50)
        self.assertNotIn(0, bot.REPLY_MSGS)
        self.assertIn(54, bot.REPLY_MSGS)


class DropPromptMsgs(unittest.TestCase):
    def setUp(self):
        self._p = dict(bot.PROMPT_MSGS)
        bot.PROMPT_MSGS.clear()

    def tearDown(self):
        bot.PROMPT_MSGS.clear()
        bot.PROMPT_MSGS.update(self._p)

    def test_drops_only_key(self):
        bot.PROMPT_MSGS[1] = {"key": "a", "author_id": 1}
        bot.PROMPT_MSGS[2] = {"key": "b", "author_id": 2}
        bot._drop_prompt_msgs("a")
        self.assertNotIn(1, bot.PROMPT_MSGS)
        self.assertIn(2, bot.PROMPT_MSGS)


class OnReactionAdd(unittest.TestCase):
    def setUp(self):
        self._p = dict(bot.PROMPT_MSGS)
        self._r = dict(bot.REPLY_MSGS)
        self._admin = bot.ADMIN_USER_IDS
        bot.PROMPT_MSGS.clear()
        bot.REPLY_MSGS.clear()
        bot.ADMIN_USER_IDS = {"1"}

    def tearDown(self):
        bot.PROMPT_MSGS.clear()
        bot.REPLY_MSGS.clear()
        bot.PROMPT_MSGS.update(self._p)
        bot.REPLY_MSGS.update(self._r)
        bot.ADMIN_USER_IDS = self._admin
        bot.QUEUES.pop("k", None)

    def test_bot_own_reaction_ignored(self):
        async def go():
            with mock.patch.object(bot, "cancel_turn",
                                   new_callable=mock.AsyncMock,
                                   side_effect=AssertionError("nope")):
                await bot.on_reaction_add(
                    FakeReaction("❌", FakeMessage(1, FakeUser(2))),
                    FakeUser(0, bot=True))
        asyncio.run(go())

    def test_cancel_prompt_message(self):
        channel = FakeChannel()
        msg = FakeMessage(11, FakeUser(7), channel)
        bot.PROMPT_MSGS[11] = {"key": "k", "author_id": 7}
        calls = []

        async def fake_cancel(key, ch):
            calls.append((key, ch))

        async def go():
            with mock.patch.object(bot, "cancel_turn", fake_cancel):
                await bot.on_reaction_add(
                    FakeReaction("❌", msg), FakeUser(7))
        asyncio.run(go())
        self.assertEqual(calls, [("k", channel)])

    def test_cancel_denied_for_stranger(self):
        msg = FakeMessage(11, FakeUser(7))
        bot.PROMPT_MSGS[11] = {"key": "k", "author_id": 7}

        async def go():
            with mock.patch.object(bot, "cancel_turn",
                                   new_callable=mock.AsyncMock,
                                   side_effect=AssertionError("nope")):
                await bot.on_reaction_add(
                    FakeReaction("❌", msg), FakeUser(9))
        asyncio.run(go())  # no raise = cancel_turn never called

    def test_cancel_unknown_message_ignored(self):
        async def go():
            with mock.patch.object(bot, "cancel_turn",
                                   new_callable=mock.AsyncMock,
                                   side_effect=AssertionError("nope")):
                await bot.on_reaction_add(
                    FakeReaction("❌", FakeMessage(999, FakeUser(7))),
                    FakeUser(7))
        asyncio.run(go())

    def test_replay_in_vc(self):
        channel = FakeChannel()
        msg = FakeMessage(21, FakeUser(0, bot=True), channel, guild=FakeGuild(5))
        bot.REPLY_MSGS[21] = {"key": "k", "author_id": 7,
                              "text": "hello", "prompt": "p"}
        said = []

        async def fake_vc_say(gid, wav, inbox, epoch=None):
            said.append((gid, wav))
            return True

        async def go():
            with mock.patch.object(bot, "tts_wav", return_value=b"WAVBYTES"), \
                 mock.patch.object(bot, "guild_voice_client",
                                   return_value=FakeVC(True)), \
                 mock.patch.object(bot, "vc_say", fake_vc_say):
                await bot.on_reaction_add(
                    FakeReaction("🔊", msg), FakeUser(7))
        asyncio.run(go())
        self.assertEqual(len(said), 1)
        self.assertEqual(said[0][1], b"WAVBYTES")
        self.assertEqual(channel.sent, [])

    def test_replay_no_vc_attaches_file(self):
        channel = FakeChannel()
        msg = FakeMessage(21, FakeUser(0, bot=True), channel, guild=FakeGuild(5))
        bot.REPLY_MSGS[21] = {"key": "k", "author_id": 7,
                              "text": "hello", "prompt": "p"}

        async def go():
            with mock.patch.object(bot, "tts_wav", return_value=b"WAVBYTES"), \
                 mock.patch.object(bot, "guild_voice_client",
                                   return_value=None):
                await bot.on_reaction_add(
                    FakeReaction("🔊", msg), FakeUser(7))
        asyncio.run(go())
        self.assertEqual(len(channel.sent), 1)
        content, file = channel.sent[0]
        self.assertIsNone(content)
        self.assertEqual(file.filename, "replay.wav")

    def test_regen_starts_turn(self):
        channel = FakeChannel()
        msg = FakeMessage(21, FakeUser(0, bot=True), channel)
        bot.REPLY_MSGS[21] = {"key": "k", "author_id": 7,
                              "text": "hello", "prompt": "do thing"}
        calls = []

        async def fake_batches(key, ch, q, batch, inbox, status):
            calls.append((key, batch))

        async def go():
            with mock.patch.object(bot, "run_batches", fake_batches):
                await bot.on_reaction_add(
                    FakeReaction("🔁", msg), FakeUser(7))
        asyncio.run(go())
        self.assertEqual(len(calls), 1)
        key, batch = calls[0]
        self.assertEqual(key, "k")
        self.assertEqual(batch, [(msg, "do thing", [])])


if __name__ == "__main__":
    unittest.main()
