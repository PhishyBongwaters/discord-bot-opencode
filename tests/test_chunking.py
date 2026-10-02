import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tests  # noqa: F401
import bot
import chunking


class SplitSmart(unittest.TestCase):
    def test_short_and_empty(self):
        self.assertEqual(chunking.split_smart("hi"), ["hi"])
        self.assertEqual(chunking.split_smart(""), [])

    def test_exact_boundary(self):
        self.assertEqual(chunking.split_smart("a" * 2000), ["a" * 2000])
        self.assertEqual(len(chunking.split_smart("a" * 2001, 2000)), 2)

    def test_fences_stay_balanced(self):
        code = "\n".join(f"line {i:04d} xxxxxxxxxxxxxxxxxxxx"
                        for i in range(40))
        text = "intro\n```python\n" + code + "\n```\noutro"
        chunks = chunking.split_smart(text, 300)
        self.assertGreater(len(chunks), 2)
        for c in chunks:
            self.assertLessEqual(len(c), 300)
            fences = [ln for ln in c.split("\n")
                      if chunking._parse_fence(ln) is not None]
            self.assertEqual(len(fences) % 2, 0, c)

    def test_fence_content_preserved(self):
        code = "\n".join(f"line {i}" for i in range(60))
        text = "```js\n" + code + "\n```"
        chunks = chunking.split_smart(text, 200)

        def strip(s):
            return "\n".join(ln for ln in s.split("\n")
                            if chunking._parse_fence(ln) is None)

        self.assertEqual(strip("\n".join(chunks)), strip(text))
        # reopened chunk keeps the info string
        self.assertTrue(any(c.split("\n")[0] == "```js" for c in chunks[1:]))

    def test_long_line_hard_split(self):
        line = "word " * 2000
        chunks = chunking.split_smart(line.strip(), 500)
        self.assertTrue(all(len(c) <= 500 for c in chunks))
        self.assertEqual("".join(c.strip() for c in chunks).replace(" ", ""),
                         line.strip().replace(" ", ""))


class SplitSentences(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(bot.split_sentences(""), [])
        self.assertEqual(bot.split_sentences("   "), [])

    def test_sentence_packing(self):
        chunks = bot.split_sentences(
            "Hello world. This is a test. Short.", max_len=20)
        self.assertTrue(all(len(c) <= 20 for c in chunks))
        self.assertEqual(" ".join(chunks),
                         "Hello world. This is a test. Short.")

    def test_overlong_sentence_splits(self):
        long_one = "word " * 100 + "."
        chunks = bot.split_sentences(long_one, max_len=50)
        self.assertTrue(all(len(c) <= 50 for c in chunks))
        self.assertEqual(" ".join(chunks).replace("  ", " "),
                         long_one.strip())

    def test_default_max_len(self):
        self.assertEqual(bot.split_sentences("a b", max_len=0),
                         bot.split_sentences("a b"))


if __name__ == "__main__":
    unittest.main()
