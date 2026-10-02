"""Review round-2 fixes: red-first regression tests."""

import asyncio
import collections
import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tests  # noqa: F401
import bot


def _load_discord_send():
    path = Path(__file__).resolve().parent.parent / "discord-send.py"
    spec = importlib.util.spec_from_loader("discord_send",
                                          importlib.util.spec_from_file_location(
                                              "discord_send", path).loader)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeUser:
    def __init__(self, id, bot=False):
        self.id = id
        self.bot = bot


class FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, file=None):
        self.sent.append((content, file))
        return FakeSentMessage()


class FakeSentMessage:
    _next = 10 ** 6

    def __init__(self):
        FakeSentMessage._next += 1
        self.id = FakeSentMessage._next


class FakeMessage:
    def __init__(self, id, author, channel=None):
        self.id = id
        self.author = author
        self.channel = channel or FakeChannel()
        self.replied = []
        self.reacted = []

    async def reply(self, content):
        self.replied.append(content)
        return FakeSentMessage()

    async def add_reaction(self, emoji):
        self.reacted.append(emoji)


class FakeInteraction:
    def __init__(self, user_id, guild=None):
        self.user = FakeUser(user_id)
        self.guild = guild
        self.channel = None  # DM-style interaction
        self.channel_id = 555
        self.deferred = False
        self.followups = []

    class _Response:
        def __init__(self, outer):
            self._outer = outer

        async def defer(self, ephemeral=False):
            self._outer.deferred = True

    class _Followup:
        def __init__(self, outer):
            self._outer = outer

        async def send(self, content, ephemeral=False):
            self._outer.followups.append((content, ephemeral))

    @property
    def response(self):
        return FakeInteraction._Response(self)

    @property
    def followup(self):
        return FakeInteraction._Followup(self)


class ReplyRecordText(unittest.TestCase):
    def test_full_text_recorded_not_placeholder(self):
        full = "x" * 5000
        recorded = bot._reply_record_text([], full)
        self.assertEqual(recorded, full)
        self.assertNotIn("reply too long", recorded)

    def test_say_lines_folded_in(self):
        self.assertEqual(bot._reply_record_text(["say this"], "body"),
                         "say this\nbody")

    def test_record_reply_author_override(self):
        sent = FakeSentMessage()
        try:
            bot._record_reply(sent, "k", [FakeMessage(1, FakeUser(7))],
                              "text", author_id=999)
            self.assertEqual(bot.REPLY_MSGS[sent.id]["author_id"], 999)
        finally:
            bot.REPLY_MSGS.pop(sent.id, None)

    def test_record_reply_falls_back_to_message_author(self):
        sent = FakeSentMessage()
        try:
            bot._record_reply(sent, "k", [FakeMessage(1, FakeUser(7))], "text")
            self.assertEqual(bot.REPLY_MSGS[sent.id]["author_id"], 7)
        finally:
            bot.REPLY_MSGS.pop(sent.id, None)


class SafeInboxName(unittest.TestCase):
    def test_table(self):
        cases = {
            "photo.jpg": ("photo", ".jpg"),
            "..\\..\\evil.txt": ("evil", ".txt"),
            "../../evil.txt": ("evil", ".txt"),
            "C:\\a\\b.png": ("b", ".png"),
            "/etc/passwd": ("passwd", ""),
            "a/b/c.mp3": ("c", ".mp3"),
            "...": ("file", ""),
            "": ("file", ""),
            ".hidden": ("hidden", ""),
            "noext": ("noext", ""),
            "sp ace.pdf": ("sp ace", ".pdf"),
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(bot._safe_inbox_name(raw), want)

    def test_result_never_escapes(self):
        for raw in ("..\\..\\x", "/abs/y", "C:\\z", "..."):
            stem, suffix = bot._safe_inbox_name(raw)
            self.assertNotIn("..", stem)
            self.assertNotIn("/", stem)
            self.assertNotIn("\\", stem)


class SlashAllowList(unittest.TestCase):
    def test_denied_user_gets_ephemeral_denial(self):
        inter = FakeInteraction(99999)
        with mock.patch.object(bot, "ALLOWED", {"12345"}), \
                mock.patch.object(bot, "handle_text_command") as htc:
            asyncio.run(bot._run_slash(inter, "!new"))
        htc.assert_not_called()
        self.assertTrue(inter.deferred)
        self.assertEqual(len(inter.followups), 1)
        content, ephemeral = inter.followups[0]
        self.assertTrue(ephemeral)
        self.assertIn("allow-list", content)

    def test_allowed_user_passes_through(self):
        inter = FakeInteraction(12345)
        with mock.patch.object(bot, "ALLOWED", {"12345"}), \
                mock.patch.object(bot, "handle_text_command",
                                  new=mock.AsyncMock()) as htc:
            asyncio.run(bot._run_slash(inter, "!new"))
        htc.assert_called_once()
        self.assertEqual(inter.followups, [])

    def test_open_mode_allows_anyone(self):
        inter = FakeInteraction(99999)
        with mock.patch.object(bot, "ALLOWED", set()), \
                mock.patch.object(bot, "handle_text_command",
                                  new=mock.AsyncMock()) as htc:
            asyncio.run(bot._run_slash(inter, "!new"))
        htc.assert_called_once()


class FakeProc:
    def __init__(self, pid=4242):
        self.pid = pid
        self.calls = []

    def terminate(self):
        self.calls.append("terminate")

    def kill(self):
        self.calls.append("kill")

    def wait(self, timeout=None):
        self.calls.append("wait")
        return 0


class KillProcTree(unittest.TestCase):
    def test_posix_terminate_then_wait(self):
        proc = FakeProc()
        with mock.patch.object(bot.os, "name", "posix"):
            bot._kill_proc_tree(proc, "k")
        self.assertEqual(proc.calls, ["terminate", "wait"])

    def test_windows_uses_taskkill_tree(self):
        proc = FakeProc()
        with mock.patch.object(bot.os, "name", "nt"), \
                mock.patch.object(bot.subprocess, "run") as run:
            bot._kill_proc_tree(proc, "k")
        run.assert_called_once_with(
            ["taskkill", "/PID", "4242", "/T", "/F"],
            capture_output=True, timeout=15)
        self.assertEqual(proc.calls, [])

    def test_windows_taskkill_failure_falls_back(self):
        proc = FakeProc()
        with mock.patch.object(bot.os, "name", "nt"), \
                mock.patch.object(bot.subprocess, "run",
                                  side_effect=OSError("nope")):
            bot._kill_proc_tree(proc, "k")  # must not raise
        self.assertEqual(proc.calls, ["terminate", "wait"])


class OpencodeStdin(unittest.TestCase):
    def test_prompt_goes_on_stdin_not_argv(self):
        prompt = "P" * 40000  # past the Windows 32767 argv cap
        seen = {}

        class PopenFake:
            def __init__(self, cmd, **kw):
                seen["cmd"] = cmd
                seen["kw"] = kw
                self.returncode = 0

            def communicate(self, input=None, timeout=None):
                seen["input"] = input
                seen["timeout"] = timeout
                return "", ""

            def poll(self):
                return 0

        with mock.patch.object(bot.subprocess, "Popen", PopenFake):
            text, _sid = bot.run_opencode(prompt, "k-test-stdin")
        self.assertNotIn(prompt, seen["cmd"])
        self.assertTrue(seen["kw"].get("stdin") is bot.subprocess.PIPE)
        self.assertEqual(seen["input"], prompt)
        self.assertEqual(text, "(opencode returned no text)")


class FakeVC:
    def __init__(self, channel_id, connected=True):
        self.channel = FakeVCChannel(channel_id)
        self._connected = connected
        self.disconnected = False

    def is_connected(self):
        return self._connected

    def stop(self):
        pass

    async def disconnect(self):
        self.disconnected = True


class FakeVCChannel:
    def __init__(self, id):
        self.id = id


class AutoleaveMove(unittest.TestCase):
    def test_move_rearms_against_new_channel(self):
        gid = 777001
        vc = FakeVC(channel_id=222)  # moved: armed for 111, now in 222
        sentinel = object()
        bot.VC_AUTOLEAVE_TASKS[gid] = sentinel
        try:
            with mock.patch.object(bot, "guild_voice_client",
                                   return_value=vc), \
                    mock.patch.object(bot, "_refresh_autoleave") as refresh:
                asyncio.run(bot._autoleave_countdown(gid, 111, 0))
            refresh.assert_called_once_with(gid)
            # stale registration cleared so the re-arm isn't a no-op
            self.assertIsNot(bot.VC_AUTOLEAVE_TASKS.get(gid), sentinel)
        finally:
            bot.VC_AUTOLEAVE_TASKS.pop(gid, None)
            bot.VC_AUTOLEAVE_AT.pop(gid, None)

    def test_same_channel_still_leaves(self):
        gid = 777002
        vc = FakeVC(channel_id=111)
        bot.VC_AUTOLEAVE_TASKS[gid] = asyncio.sleep(0)  # placeholder
        try:
            with mock.patch.object(bot, "guild_voice_client",
                                   return_value=vc), \
                    mock.patch.object(bot, "_vc_human_count",
                                      return_value=0), \
                    mock.patch.object(bot, "vc_drop"):
                asyncio.run(bot._autoleave_countdown(gid, 111, 0))
            self.assertTrue(vc.disconnected)
        finally:
            bot.VC_AUTOLEAVE_TASKS.pop(gid, None)
            bot.VC_AUTOLEAVE_AT.pop(gid, None)
            bot.VC_EXPECTED_BYE.discard(gid)


class SampleEnv(unittest.TestCase):
    def test_proactive_text_settings_present(self):
        path = Path(__file__).resolve().parent.parent / "sample.env"
        text = path.read_text(encoding="utf-8")
        for var in ("TEXT_DIR=", "TEXT_POLL=", "TEXT_MAX_BYTES=",
                    "TEXT_MAX_PER_MINUTE="):
            self.assertIn(var, text, f"sample.env missing {var.rstrip('=')}")


class TextRateCleanup(unittest.TestCase):
    def _run_file(self, target, body):
        d = Path(tempfile.mkdtemp(prefix="textrate-"))
        p = d / f"{target}_label.txt"
        p.write_text(body, encoding="utf-8")

        class FakeChan:
            def __init__(self):
                self.sent = []

            async def send(self, content):
                self.sent.append(content)

        chan = FakeChan()
        stub = mock.MagicMock()
        stub.get_channel.return_value = chan
        with mock.patch.object(bot, "client", stub):
            result = asyncio.run(bot.process_text_file(p))
        return result, chan

    def test_expired_bucket_is_dropped(self):
        target = 888001
        old = time.monotonic() - 120
        bot.TEXT_RATE[target] = collections.deque([old, old + 1])
        try:
            result, chan = self._run_file(target, "hello")
            self.assertTrue(result)
            self.assertEqual(len(chan.sent), 1)
            # fresh bucket: only the new stamp, old ones gone
            self.assertEqual(len(bot.TEXT_RATE[target]), 1)
            self.assertGreater(bot.TEXT_RATE[target][0], old + 60)
        finally:
            bot.TEXT_RATE.pop(target, None)

    def test_over_rate_hold_keeps_bucket(self):
        target = 888002
        now = time.monotonic()
        bot.TEXT_RATE[target] = collections.deque(
            [now - i for i in range(10)])
        try:
            with mock.patch.object(bot, "TEXT_MAX_PER_MINUTE", 10):
                result, chan = self._run_file(target, "hello")
            self.assertFalse(result)  # held, not sent
            self.assertEqual(chan.sent, [])
            self.assertEqual(len(bot.TEXT_RATE[target]), 10)
        finally:
            bot.TEXT_RATE.pop(target, None)


class DiscordSendPartial(unittest.TestCase):
    def test_failure_reports_partial_progress(self):
        mod = _load_discord_send()
        calls = []

        def fake_api(token, method, path, payload):
            calls.append(payload["content"])
            if len(calls) == 3:
                return 500, "boom"
            return 200, {}

        with mock.patch.object(mod, "api", side_effect=fake_api), \
                mock.patch.object(mod, "split_smart",
                                  side_effect=lambda t, n: [f"p{i}"
                                                            for i in range(5)]):
            err = io.StringIO()
            with contextlib.redirect_stderr(err), \
                    self.assertRaises(SystemExit) as cm:
                mod.send_chunks("tok", "123", "text")
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("posted 2 of 5 chunks", err.getvalue())

    def test_success_unchanged(self):
        mod = _load_discord_send()
        with mock.patch.object(mod, "api", return_value=(200, {})), \
                mock.patch.object(mod, "split_smart",
                                  side_effect=lambda t, n: ["a", "b"]):
            self.assertEqual(mod.send_chunks("tok", "123", "text"), 2)


class RegenCosmetics(unittest.TestCase):
    def test_regen_reply_posts_plain_not_threaded(self):
        bot_msg = FakeMessage(5, FakeUser(1, bot=True))
        channel = FakeChannel()
        inbox = Path(tempfile.mkdtemp(prefix="regen-"))
        asyncio.run(bot.deliver_reply("k", channel, [bot_msg], "hello",
                                      inbox, None))
        self.assertEqual(len(channel.sent), 1)
        self.assertEqual(bot_msg.replied, [])  # not threaded under itself

    def test_no_self_reactions(self):
        bot_msg = FakeMessage(6, FakeUser(1, bot=True))
        asyncio.run(bot.react(bot_msg, "✅"))
        asyncio.run(bot.swap_react(bot_msg, "⏳", "✅"))
        self.assertEqual(bot_msg.reacted, [])

    def test_user_messages_still_reacted(self):
        user_msg = FakeMessage(7, FakeUser(9))
        asyncio.run(bot.react(user_msg, "✅"))
        self.assertEqual(user_msg.reacted, ["✅"])


if __name__ == "__main__":
    unittest.main()
