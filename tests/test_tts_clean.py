import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tests  # noqa: F401
import bot


class CleanForTts(unittest.TestCase):
    def test_markers_stripped(self):
        # attach/react markers are stripped; [[say:]] markers are NOT
        # (deliver_reply splits those out before TTS ever sees the text)
        out = bot.clean_for_tts("hi [[attach:/tmp/a.png]] [[react:👍]] there")
        self.assertEqual(out, "hi there")
        out = bot.clean_for_tts("hi [[say:aside]] there")
        self.assertEqual(out, "hi [[say:aside]] there")

    def test_fenced_code(self):
        out = bot.clean_for_tts("run this:\n```python\nprint(1)\n```\ndone")
        self.assertEqual(out, "run this: print(1) done")

    def test_inline_code(self):
        self.assertEqual(bot.clean_for_tts("use `foo()` now"), "use foo() now")

    def test_links_keep_label(self):
        self.assertEqual(bot.clean_for_tts("see [docs](http://x) ok"),
                         "see docs ok")
        self.assertEqual(bot.clean_for_tts("pic ![alt](http://x/u.png) end"),
                         "pic alt end")

    def test_whitespace_collapsed(self):
        self.assertEqual(bot.clean_for_tts("a\n\nb\t\tc"), "a b c")

    def test_empty(self):
        self.assertEqual(bot.clean_for_tts(""), "")
        self.assertEqual(bot.clean_for_tts("[[attach:/x]]"), "")

    def test_truncation_prefers_sentence_end(self):
        text = ("First sentence ends here. " * 4 +
                "This last bit has no terminator at all " * 6)
        with mock.patch.object(bot, "VOICEBOX_MAX_CHARS", 60):
            out = bot.clean_for_tts(text)
        self.assertLessEqual(len(out), 60)
        # cut at the last sentence boundary past the halfway mark
        self.assertTrue(out.endswith("here."), out)

    def test_truncation_mid_word_fallback(self):
        text = "a" * 200  # no separators at all
        with mock.patch.object(bot, "VOICEBOX_MAX_CHARS", 60):
            out = bot.clean_for_tts(text)
        self.assertEqual(out, "a" * 60)

    def test_no_truncation_when_disabled(self):
        with mock.patch.object(bot, "VOICEBOX_MAX_CHARS", 0):
            self.assertEqual(bot.clean_for_tts("x" * 5000), "x" * 5000)


if __name__ == "__main__":
    unittest.main()
