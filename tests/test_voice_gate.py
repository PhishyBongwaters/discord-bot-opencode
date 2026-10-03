"""Guild gate: voice notes count as addressed without mention/prefix."""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tests  # noqa: F401
import bot


def make_msg(*, text="", attachments=(), mentioned=False):
    mentions = [bot.client.user] if mentioned else []
    return SimpleNamespace(
        content=text,
        attachments=list(attachments),
        mentions=mentions,
    )


def audio(name="voice-message.ogg", ctype="audio/ogg"):
    return SimpleNamespace(filename=name, content_type=ctype)


def image(name="pic.png", ctype="image/png"):
    return SimpleNamespace(filename=name, content_type=ctype)


class GuildGate(unittest.TestCase):
    def test_voice_note_bypasses_gate(self):
        msg = make_msg(text="", attachments=[audio()])
        with mock.patch.object(bot, "VOICEBOX_TRANSCRIBE", True):
            self.assertTrue(bot.guild_message_addressed(msg, ""))

    def test_voice_note_blocked_when_transcribe_off(self):
        msg = make_msg(text="", attachments=[audio()])
        with mock.patch.object(bot, "VOICEBOX_TRANSCRIBE", False):
            self.assertFalse(bot.guild_message_addressed(msg, ""))

    def test_image_still_needs_mention_or_prefix(self):
        msg = make_msg(text="", attachments=[image()])
        with mock.patch.object(bot, "VOICEBOX_TRANSCRIBE", True):
            self.assertFalse(bot.guild_message_addressed(msg, ""))

    def test_plain_text_still_dropped(self):
        msg = make_msg(text="hello")
        with mock.patch.object(bot, "VOICEBOX_TRANSCRIBE", True):
            self.assertFalse(bot.guild_message_addressed(msg, "hello"))

    def test_prefix_and_mention_still_work(self):
        with mock.patch.object(bot, "VOICEBOX_TRANSCRIBE", False):
            self.assertTrue(
                bot.guild_message_addressed(
                    make_msg(text="!oc hi"), "!oc hi"))
            self.assertTrue(
                bot.guild_message_addressed(
                    make_msg(text="hi", mentioned=True), "hi"))


if __name__ == "__main__":
    unittest.main()
