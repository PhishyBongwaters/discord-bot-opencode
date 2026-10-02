"""Video thumbnails via ffmpeg (issue #6)."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tests  # noqa: F401
import bot


def _make_video(tmpdir, name="clip.mp4", size=1024):
    p = Path(tmpdir) / name
    p.write_bytes(b"\x00" * size)
    return p


class FakeCompleted:
    def __init__(self, returncode=0):
        self.returncode = returncode
        self.stdout = b""
        self.stderr = b""


def fake_run_ok(cmd, **kwargs):
    # ffmpeg writes its output file (last arg)
    Path(cmd[-1]).write_bytes(b"FAKEJPEG" * 100)
    return FakeCompleted(0)


class ExtractThumbs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dbot-thumbs-")
        self._frames = bot.VIDEO_THUMB_FRAMES
        self._every = bot.VIDEO_THUMB_EVERY_S
        self._maxmb = bot.VIDEO_THUMB_MAX_MB
        bot.VIDEO_THUMB_FRAMES = 4
        bot.VIDEO_THUMB_EVERY_S = 5
        bot.VIDEO_THUMB_MAX_MB = 200

    def tearDown(self):
        bot.VIDEO_THUMB_FRAMES = self._frames
        bot.VIDEO_THUMB_EVERY_S = self._every
        bot.VIDEO_THUMB_MAX_MB = self._maxmb

    def test_disabled_returns_empty(self):
        bot.VIDEO_THUMB_FRAMES = 0
        p = _make_video(self.tmp)
        self.assertEqual(bot.extract_video_thumbs(str(p)), [])

    def test_no_ffmpeg_returns_empty(self):
        p = _make_video(self.tmp)
        with mock.patch.object(bot, "FFMPEG_BIN", ""):
            self.assertEqual(bot.extract_video_thumbs(str(p)), [])

    def test_ffmpeg_failure_returns_empty(self):
        p = _make_video(self.tmp)
        with mock.patch.object(bot.subprocess, "run",
                               return_value=FakeCompleted(1)):
            self.assertEqual(bot.extract_video_thumbs(str(p)), [])

    def test_ffmpeg_exception_returns_empty(self):
        p = _make_video(self.tmp)
        with mock.patch.object(bot.subprocess, "run",
                               side_effect=OSError("nope")):
            self.assertEqual(bot.extract_video_thumbs(str(p)), [])

    def test_success_extracts_frames(self):
        p = _make_video(self.tmp)
        with mock.patch.object(bot.subprocess, "run", fake_run_ok):
            outs = bot.extract_video_thumbs(str(p), key="t")
        self.assertEqual(len(outs), 4)
        for o in outs:
            self.assertTrue(Path(o).exists())
        self.assertEqual(Path(outs[0]).name, "clip_thumb1.jpg")

    def test_oversize_caps_frames(self):
        bot.VIDEO_THUMB_MAX_MB = 0  # any file counts as oversize
        p = _make_video(self.tmp)
        with mock.patch.object(bot.subprocess, "run", fake_run_ok):
            outs = bot.extract_video_thumbs(str(p), key="t")
        self.assertEqual(len(outs), 2)

    def test_uniquifies_existing(self):
        p = _make_video(self.tmp)
        (Path(self.tmp) / "clip_thumb1.jpg").write_bytes(b"old")
        with mock.patch.object(bot.subprocess, "run", fake_run_ok):
            outs = bot.extract_video_thumbs(str(p), key="t")
        names = [Path(o).name for o in outs]
        self.assertIn("clip_thumb1(1).jpg", names)


if __name__ == "__main__":
    unittest.main()
