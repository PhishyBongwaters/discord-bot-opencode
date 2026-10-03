"""Voicebox keep-warm: status parsing, routing, cooldown, and gating."""

import asyncio
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tests  # noqa: F401
import bot


def status_obj(*entries):
    return {"models": [
        {"model_name": name, "loaded": loaded}
        for name, loaded in entries]}


class WhisperStatusParse(unittest.TestCase):
    def test_loaded_whisper_detected(self):
        self.assertTrue(bot._whisper_loaded_from_status(status_obj(
            ("qwen-tts-1.7B", True), ("whisper-base", True))))

    def test_no_loaded_whisper(self):
        self.assertFalse(bot._whisper_loaded_from_status(status_obj(
            ("qwen-tts-1.7B", True), ("whisper-base", False))))

    def test_no_whisper_models(self):
        self.assertFalse(bot._whisper_loaded_from_status(status_obj(
            ("qwen-tts-1.7B", True),)))

    def test_malformed_payloads(self):
        for bad in (None, {}, {"models": None}, {"models": "x"},
                    {"models": [{"model_name": "whisper-base"}]}):
            self.assertFalse(bot._whisper_loaded_from_status(bad))


class TtsLoadedParse(unittest.TestCase):
    def test_true_false_unknown(self):
        with mock.patch.object(bot, "_voicebox_get_json",
                               return_value={"model_loaded": True}):
            self.assertTrue(bot.voicebox_tts_loaded())
        with mock.patch.object(bot, "_voicebox_get_json",
                               return_value={"model_loaded": False}):
            self.assertFalse(bot.voicebox_tts_loaded())
        with mock.patch.object(bot, "_voicebox_get_json",
                               return_value=None):
            self.assertIsNone(bot.voicebox_tts_loaded())
        with mock.patch.object(bot, "_voicebox_get_json",
                               return_value={"other": 1}):
            self.assertIsNone(bot.voicebox_tts_loaded())

    def test_whisper_loaded_unknown(self):
        with mock.patch.object(bot, "_voicebox_get_json",
                               return_value=None):
            self.assertIsNone(bot.voicebox_whisper_loaded())
        with mock.patch.object(
                bot, "_voicebox_get_json",
                return_value=status_obj(("whisper-base", True))):
            self.assertTrue(bot.voicebox_whisper_loaded())


class WarmCooldown(unittest.TestCase):
    def test_due_after_mark(self):
        old = dict(bot.VOICE_WARM_LAST)
        try:
            bot.VOICE_WARM_LAST["t"] = time.monotonic() - 3600
            self.assertTrue(bot.warm_due())
            with mock.patch.object(bot, "VOICEBOX_WARM_COOLDOWN_S", 60.0):
                bot.VOICE_WARM_LAST["t"] = time.monotonic()
                self.assertFalse(bot.warm_due())
        finally:
            bot.VOICE_WARM_LAST.update(old)

    def test_trigger_respects_cooldown(self):
        old = dict(bot.VOICE_WARM_LAST)
        try:
            bot.VOICE_WARM_LAST["t"] = time.monotonic()
            with mock.patch.object(bot, "VOICEBOX_URL", "http://x"), \
                 mock.patch.object(bot, "ensure_voice_models") as ensured:
                bot.trigger_voice_warm("test")
                ensured.assert_not_called()
        finally:
            bot.VOICE_WARM_LAST.update(old)

    def test_trigger_fires_when_due(self):
        old = dict(bot.VOICE_WARM_LAST)
        try:
            bot.VOICE_WARM_LAST["t"] = time.monotonic() - 3600
            with mock.patch.object(bot, "VOICEBOX_URL", "http://x"), \
                 mock.patch.object(bot, "asyncio") as aio:
                bot.trigger_voice_warm("test")
                aio.ensure_future.assert_called_once()
        finally:
            bot.VOICE_WARM_LAST.update(old)


class KeepaliveGate(unittest.TestCase):
    def _wanted(self, keepalive_s, url, voice_mode):
        with mock.patch.object(bot, "VOICEBOX_KEEPALIVE_S", keepalive_s), \
             mock.patch.object(bot, "VOICEBOX_URL", url), \
             mock.patch.object(bot, "voice_mode_on",
                               return_value=voice_mode):
            return bot.keepalive_wanted()

    def test_needs_all_three(self):
        self.assertTrue(self._wanted(300, "http://x", True))
        self.assertFalse(self._wanted(0, "http://x", True))
        self.assertFalse(self._wanted(300, "", True))
        self.assertFalse(self._wanted(300, "http://x", False))

    def test_probe_short_circuits_without_url(self):
        with mock.patch.object(bot, "VOICEBOX_URL", ""):
            self.assertEqual(bot.voicebox_probe_stt(), (False, 0.0))
        with mock.patch.object(bot, "VOICEBOX_URL", ""):
            self.assertFalse(bot.voicebox_load_tts())


class EnsureRouting(unittest.TestCase):
    def run_ensure(self, tts_loaded, whisper_loaded):
        with mock.patch.object(bot, "voicebox_tts_loaded",
                               return_value=tts_loaded), \
             mock.patch.object(bot, "voicebox_whisper_loaded",
                               return_value=whisper_loaded), \
             mock.patch.object(bot, "voicebox_load_tts",
                               return_value=True) as load, \
             mock.patch.object(bot, "voicebox_probe_stt",
                               return_value=(True, 0.1)) as probe:
            return asyncio.run(bot.ensure_voice_models("test")), load, probe

    def test_all_warm_loads_nothing(self):
        (tts_ok, stt_ok, acted), load, probe = self.run_ensure(True, True)
        self.assertEqual((tts_ok, stt_ok, acted), (True, True, []))
        load.assert_not_called()
        probe.assert_not_called()

    def test_cold_models_reloaded(self):
        (tts_ok, stt_ok, acted), load, probe = self.run_ensure(False, False)
        self.assertEqual((tts_ok, stt_ok, acted), (True, True, ["tts", "stt"]))
        load.assert_called_once()
        probe.assert_called_once()

    def test_unreachable_is_not_ok(self):
        (tts_ok, stt_ok, acted), load, probe = self.run_ensure(None, None)
        self.assertEqual((tts_ok, stt_ok, acted), (False, False, []))
        load.assert_not_called()
        probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
