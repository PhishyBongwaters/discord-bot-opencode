"""Role-based permissions (issue #5)."""

import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tests  # noqa: F401
import bot


class FakeRole:
    def __init__(self, id):
        self.id = id


class FakeAuthor:
    def __init__(self, id, roles=None):
        self.id = id
        if roles is not None:
            self.roles = roles  # Member-like; plain Users lack .roles


class FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, text):
        self.sent.append(text)


class FakeMessage:
    def __init__(self, author):
        self.author = author
        self.channel = FakeChannel()


class RoleTier(unittest.TestCase):
    def setUp(self):
        self._admin = bot.ADMIN_USER_IDS
        self._dj_u = bot.DJ_USER_IDS
        self._dj_r = bot.DJ_ROLE_IDS
        self._adm_r = bot.ADMIN_ROLE_IDS

    def tearDown(self):
        bot.ADMIN_USER_IDS = self._admin
        bot.DJ_USER_IDS = self._dj_u
        bot.DJ_ROLE_IDS = self._dj_r
        bot.ADMIN_ROLE_IDS = self._adm_r

    def _cfg(self, admin, dj_u=(), dj_r=(), adm_r=()):
        bot.ADMIN_USER_IDS = set(admin) if admin is not None else None
        bot.DJ_USER_IDS = set(dj_u)
        bot.DJ_ROLE_IDS = set(dj_r)
        bot.ADMIN_ROLE_IDS = set(adm_r)

    def test_default_single_operator(self):
        # tests/__init__ pins ALLOWED_USER_IDS=12345, so ADMIN defaults to it
        self.assertEqual(bot.role_tier("12345"), bot.TIER_ADMIN)
        self.assertEqual(bot.role_tier("999"), bot.TIER_EVERYONE)

    def test_dj_user_id(self):
        self._cfg({"1"}, dj_u={"2"})
        self.assertEqual(bot.role_tier("2"), bot.TIER_DJ)
        self.assertEqual(bot.role_tier("1"), bot.TIER_ADMIN)

    def test_role_ids(self):
        self._cfg({"1"}, dj_r={"10"}, adm_r={"20"})
        dj = FakeAuthor("5", roles=[FakeRole(10)])
        adm = FakeAuthor("6", roles=[FakeRole(20)])
        self.assertEqual(bot.role_tier("5", dj), bot.TIER_DJ)
        self.assertEqual(bot.role_tier("6", adm), bot.TIER_ADMIN)
        # admin implies DJ
        self.assertGreaterEqual(bot.role_tier("6", adm), bot.TIER_DJ)

    def test_open_mode_disables_tiers(self):
        self._cfg(None)
        self.assertEqual(bot.role_tier("anyone"), bot.TIER_ADMIN)

    def test_member_without_roles_attr(self):
        self._cfg({"1"}, dj_u={"2"})
        self.assertEqual(bot.role_tier("2", FakeAuthor("2")), bot.TIER_DJ)


class MessageTier(unittest.TestCase):
    def test_dm_no_roles(self):
        m = FakeMessage(FakeAuthor("2"))
        with mock.patch.object(bot, "DJ_USER_IDS", {"2"}), \
             mock.patch.object(bot, "ADMIN_USER_IDS", {"1"}):
            self.assertEqual(bot.message_tier(m), bot.TIER_DJ)

    def test_guild_member_roles(self):
        m = FakeMessage(FakeAuthor("5", roles=[FakeRole(10)]))
        with mock.patch.object(bot, "DJ_ROLE_IDS", {"10"}), \
             mock.patch.object(bot, "ADMIN_USER_IDS", {"1"}):
            self.assertEqual(bot.message_tier(m), bot.TIER_DJ)

    def test_interaction_tier(self):
        inter = mock.Mock()
        inter.user = FakeAuthor("5", roles=[FakeRole(10)])
        with mock.patch.object(bot, "DJ_ROLE_IDS", {"10"}), \
             mock.patch.object(bot, "ADMIN_USER_IDS", {"1"}):
            self.assertEqual(bot.interaction_tier(inter), bot.TIER_DJ)


class RequireTier(unittest.TestCase):
    def test_allowed_passes_silently(self):
        m = FakeMessage(FakeAuthor("1"))
        with mock.patch.object(bot, "ADMIN_USER_IDS", {"1"}):
            async def go():
                self.assertTrue(
                    await bot.require_tier(m, bot.TIER_DJ, "!say"))
            asyncio.run(go())
        self.assertEqual(m.channel.sent, [])

    def test_denied_replies_not_permitted(self):
        m = FakeMessage(FakeAuthor("9"))
        with mock.patch.object(bot, "ADMIN_USER_IDS", {"1"}):
            async def go():
                self.assertFalse(
                    await bot.require_tier(m, bot.TIER_DJ, "!say"))
            asyncio.run(go())
        self.assertEqual(len(m.channel.sent), 1)
        self.assertIn("not permitted", m.channel.sent[0])


if __name__ == "__main__":
    unittest.main()
