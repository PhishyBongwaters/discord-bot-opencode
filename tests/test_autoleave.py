"""Auto-leave VC when alone (issue #8)."""

import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tests  # noqa: F401
import bot


class FakeMember:
    def __init__(self, id, bot=False):
        self.id = id
        self.bot = bot


class FakeChannel:
    def __init__(self, id, members=(), guild=None):
        self.id = id
        self.members = list(members)
        self.guild = guild


class FakeGuild:
    def __init__(self, id):
        self.id = id


class FakeVC:
    def __init__(self, channel=None, connected=True):
        self.channel = channel
        self._connected = connected
        self.disconnected = False
        self.stopped = False

    def is_connected(self):
        return self._connected

    def stop(self):
        self.stopped = True

    async def disconnect(self):
        self.disconnected = True
        self._connected = False


class FakeVS:
    def __init__(self, channel):
        self.channel = channel


BOT = FakeMember(0, bot=True)


class AutoLeave(unittest.TestCase):
    def setUp(self):
        self._tasks = dict(bot.VC_AUTOLEAVE_TASKS)
        self._at = dict(bot.VC_AUTOLEAVE_AT)
        self._bye = set(bot.VC_EXPECTED_BYE)
        bot.VC_AUTOLEAVE_TASKS.clear()
        bot.VC_AUTOLEAVE_AT.clear()

    def tearDown(self):
        for t in list(bot.VC_AUTOLEAVE_TASKS.values()):
            if not t.done():
                t.cancel()
        bot.VC_AUTOLEAVE_TASKS.clear()
        bot.VC_AUTOLEAVE_AT.clear()
        bot.VC_AUTOLEAVE_TASKS.update(self._tasks)
        bot.VC_AUTOLEAVE_AT.update(self._at)
        bot.VC_EXPECTED_BYE.clear()
        bot.VC_EXPECTED_BYE.update(self._bye)

    def test_human_count(self):
        chan = FakeChannel(1, members=[BOT, FakeMember(2), FakeMember(3)])
        self.assertEqual(bot._vc_human_count(chan), 2)
        self.assertEqual(bot._vc_human_count(FakeChannel(1, members=[BOT])), 0)

    def test_arm_keeps_original_timer(self):
        async def go():
            bot._arm_autoleave(7, 70)
            t1 = bot.VC_AUTOLEAVE_TASKS[7]
            self.assertIn(7, bot.VC_AUTOLEAVE_AT)
            bot._arm_autoleave(7, 70)  # already running: not replaced
            self.assertIs(bot.VC_AUTOLEAVE_TASKS[7], t1)
            bot._cancel_autoleave(7)
            self.assertNotIn(7, bot.VC_AUTOLEAVE_TASKS)
            self.assertNotIn(7, bot.VC_AUTOLEAVE_AT)
        asyncio.run(go())

    def test_refresh_arms_when_alone(self):
        chan = FakeChannel(90, members=[BOT])
        vc = FakeVC(channel=chan)
        with mock.patch.object(bot, "guild_voice_client", return_value=vc):
            async def go():
                bot._refresh_autoleave(9)
                self.assertIn(9, bot.VC_AUTOLEAVE_TASKS)
            asyncio.run(go())

    def test_refresh_cancels_when_humans_present(self):
        chan = FakeChannel(90, members=[BOT])
        vc = FakeVC(channel=chan)
        with mock.patch.object(bot, "guild_voice_client", return_value=vc):
            async def go():
                bot._refresh_autoleave(9)
                self.assertIn(9, bot.VC_AUTOLEAVE_TASKS)
                chan.members.append(FakeMember(5))  # someone joins
                bot._refresh_autoleave(9)
                self.assertNotIn(9, bot.VC_AUTOLEAVE_TASKS)
            asyncio.run(go())

    def test_refresh_disabled(self):
        chan = FakeChannel(90, members=[BOT])
        vc = FakeVC(channel=chan)
        with mock.patch.object(bot, "guild_voice_client", return_value=vc), \
             mock.patch.object(bot, "VC_AUTOLEAVE_MINUTES", 0):
            async def go():
                bot._refresh_autoleave(9)
                self.assertNotIn(9, bot.VC_AUTOLEAVE_TASKS)
            asyncio.run(go())

    def test_countdown_fires_and_leaves(self):
        chan = FakeChannel(110, members=[BOT])
        vc = FakeVC(channel=chan)
        with mock.patch.object(bot, "guild_voice_client",
                               return_value=vc), \
             mock.patch.object(bot, "vc_drop") as drop:
            asyncio.run(bot._autoleave_countdown(11, 110, 0))
        self.assertTrue(vc.stopped)
        self.assertTrue(vc.disconnected)
        self.assertIn(11, bot.VC_EXPECTED_BYE)  # autorejoin won't fight it
        drop.assert_called_once_with(11)

    def test_countdown_noop_when_human_joined(self):
        chan = FakeChannel(110, members=[BOT, FakeMember(5)])
        vc = FakeVC(channel=chan)
        with mock.patch.object(bot, "guild_voice_client", return_value=vc):
            asyncio.run(bot._autoleave_countdown(11, 110, 0))
        self.assertFalse(vc.disconnected)
        self.assertNotIn(11, bot.VC_EXPECTED_BYE)

    def test_countdown_noop_when_moved_channel(self):
        chan = FakeChannel(111, members=[BOT])  # different channel id
        vc = FakeVC(channel=chan)
        with mock.patch.object(bot, "guild_voice_client", return_value=vc):
            asyncio.run(bot._autoleave_countdown(11, 110, 0))
        self.assertFalse(vc.disconnected)

    def test_voice_state_update_arms_on_leave(self):
        guild = FakeGuild(13)
        chan = FakeChannel(130, members=[BOT], guild=guild)
        vc = FakeVC(channel=chan)
        leaver = FakeMember(999)
        with mock.patch.object(bot, "guild_voice_client", return_value=vc):
            async def go():
                await bot.on_voice_state_update(
                    leaver, FakeVS(chan), FakeVS(None))
                # asserted inside the loop: asyncio.run cancels the
                # pending countdown task on close
                self.assertIn(13, bot.VC_AUTOLEAVE_TASKS)
            asyncio.run(go())

    def test_bot_own_disconnect_cancels_timer(self):
        guild = FakeGuild(14)
        chan = FakeChannel(140, members=[BOT], guild=guild)

        async def go():
            bot._arm_autoleave(14, 140)
            self.assertIn(14, bot.VC_AUTOLEAVE_TASKS)
            with mock.patch.object(bot, "guild_voice_client",
                                   return_value=FakeVC(channel=None,
                                                       connected=False)):
                await bot.on_voice_state_update(
                    FakeMember(0, bot=True), FakeVS(chan), FakeVS(None))
            self.assertNotIn(14, bot.VC_AUTOLEAVE_TASKS)
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
