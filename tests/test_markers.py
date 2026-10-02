import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tests  # noqa: F401  (harness: discord stub + env pinning + bot import)
import bot


class SplitAttachMarkers(unittest.TestCase):
    def test_basic(self):
        clean, paths = bot.split_attach_markers(
            "here is your file [[attach:/tmp/a.png]] enjoy")
        self.assertEqual(paths, ["/tmp/a.png"])
        self.assertEqual(clean, "here is your file  enjoy")

    def test_quotes_stripped(self):
        _, paths = bot.split_attach_markers('[[attach:"/tmp/a b.png"]]')
        self.assertEqual(paths, ["/tmp/a b.png"])
        _, paths = bot.split_attach_markers("[[attach:'/tmp/c.png']]")
        self.assertEqual(paths, ["/tmp/c.png"])

    def test_case_insensitive(self):
        clean, paths = bot.split_attach_markers("x [[ATTACH:/tmp/a.png]] y")
        self.assertEqual(paths, ["/tmp/a.png"])
        self.assertNotIn("ATTACH", clean)

    def test_windows_path(self):
        _, paths = bot.split_attach_markers(
            r"[[attach:D:\files\clip.mp4]]")
        self.assertEqual(paths, [r"D:\files\clip.mp4"])

    def test_blank_lines_collapsed(self):
        clean, _ = bot.split_attach_markers(
            "a\n\n\n\n[[attach:/x]]\n\n\n\nb")
        self.assertEqual(clean, "a\n\nb")

    def test_no_markers(self):
        clean, paths = bot.split_attach_markers("plain text")
        self.assertEqual((clean, paths), ("plain text", []))


class SplitReactMarkers(unittest.TestCase):
    def test_basic_and_cap(self):
        reply = "nice " + " ".join(f"[[react:e{i}]]" for i in range(7))
        clean, emojis = bot.split_react_markers(reply)
        self.assertEqual(emojis, [f"e{i}" for i in range(5)])  # MAX_MODEL_REACTS
        self.assertEqual(clean, "nice")

    def test_empty_filtered(self):
        clean, emojis = bot.split_react_markers("x [[react: ]] y")
        self.assertEqual(emojis, [])
        self.assertEqual(clean, "x  y")

    def test_case_insensitive(self):
        _, emojis = bot.split_react_markers("[[REACT:👍]]")
        self.assertEqual(emojis, ["👍"])


class SplitSayMarkers(unittest.TestCase):
    def test_basic_and_cap(self):
        reply = "text " + " ".join(f"[[say:line {i}]]" for i in range(5))
        clean, lines = bot.split_say_markers(reply)
        self.assertEqual(lines, [f"line {i}" for i in range(3)])  # MAX_MODEL_SAYS
        self.assertEqual(clean, "text")

    def test_empty_filtered(self):
        _, lines = bot.split_say_markers("[[say:]]")
        self.assertEqual(lines, [])


if __name__ == "__main__":
    unittest.main()
