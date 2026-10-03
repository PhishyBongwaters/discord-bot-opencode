"""Guild voice-note gate bypass (STT)."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tests  # noqa: F401
import bot


class FakeAttachment:
    def __init__(self, filename, content_type=None):
        self.filename = filename
        self.content_type = content_type


class FakeMessage:
    def __init__(self, attachments):
        self.attachments = attachments


class TranscribableAudio(unittest.TestCase):
    def _msg(self, *atts):
        return FakeMessage(list(atts))

    def test_voice_note_bypasses(self):
        m = self._msg(FakeAttachment("voice-message.ogg", "audio/ogg"))
        with mock.patch.object(bot, "VOICEBOX_TRANSCRIBE", True):
            self.assertTrue(bot._has_transcribable_audio(m))

    def test_extension_match_without_content_type(self):
        m = self._msg(FakeAttachment("note.mp3"))
        with mock.patch.object(bot, "VOICEBOX_TRANSCRIBE", True):
            self.assertTrue(bot._has_transcribable_audio(m))

    def test_stt_disabled_no_bypass(self):
        m = self._msg(FakeAttachment("voice-message.ogg", "audio/ogg"))
        with mock.patch.object(bot, "VOICEBOX_TRANSCRIBE", False):
            self.assertFalse(bot._has_transcribable_audio(m))

    def test_non_audio_no_bypass(self):
        m = self._msg(FakeAttachment("photo.png", "image/png"))
        with mock.patch.object(bot, "VOICEBOX_TRANSCRIBE", True):
            self.assertFalse(bot._has_transcribable_audio(m))

    def test_video_no_bypass(self):
        m = self._msg(FakeAttachment("clip.mp4", "video/mp4"))
        with mock.patch.object(bot, "VOICEBOX_TRANSCRIBE", True):
            self.assertFalse(bot._has_transcribable_audio(m))

    def test_no_attachments_no_bypass(self):
        with mock.patch.object(bot, "VOICEBOX_TRANSCRIBE", True):
            self.assertFalse(bot._has_transcribable_audio(self._msg()))

    def test_mixed_attachments_bypass(self):
        m = self._msg(FakeAttachment("photo.png", "image/png"),
                      FakeAttachment("voice-message.ogg", "audio/ogg"))
        with mock.patch.object(bot, "VOICEBOX_TRANSCRIBE", True):
            self.assertTrue(bot._has_transcribable_audio(m))


if __name__ == "__main__":
    unittest.main()
