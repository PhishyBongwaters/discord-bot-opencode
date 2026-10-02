"""Import harness for the bot test-suite.

bot.py does `sys.exit()` when `discord` is not importable, and reads its
config from the environment at import time. This module:

1. pins the environment to throwaway values (tmp state/attach dirs,
   voice + say-queue disabled) BEFORE bot.py is imported;
2. installs a minimal `discord` stub (Client, Intents, app_commands,
   ui.View, opus, ...) into sys.modules so the import succeeds with
   stdlib only — no discord.py, no token, no network.

Importing this package (`import tests`) is what makes `import bot`
work in every test module. Run the suite from the repo root:

    python -m unittest discover -s tests
"""

import os
import sys
import tempfile
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_TMP = tempfile.mkdtemp(prefix="dbot-tests-")

# --- pin config before bot.py reads it at import ---------------------------
os.environ["DISCORD_BOT_TOKEN"] = "test-token"
os.environ["ALLOWED_USER_IDS"] = "12345"  # silence the empty-allow warning
os.environ["OPENCODE_DIR"] = _TMP
os.environ["OPENCODE_BIN"] = "opencode"
os.environ["STATE_FILE"] = os.path.join(_TMP, "state", "sessions.json")
os.environ["ATTACH_DIR"] = os.path.join(_TMP, "attachments")
os.environ["FILE_JAIL"] = os.path.join(_TMP, "jail")
os.environ["SAY_DIR"] = ""          # disable the say-queue watcher
os.environ["VOICEBOX_URL"] = ""     # disable voice entirely
os.environ["VOICEBOX_VOICE"] = "0"
os.environ["VC_AUTOJOIN"] = ""
os.environ["VC_AUTOREJOIN"] = "0"
os.environ["VOICEBOX_WARMUP"] = "0"


def _build_discord_stub():
    discord = types.ModuleType("discord")
    app_commands = types.ModuleType("discord.app_commands")
    ui = types.ModuleType("discord.ui")

    class ActivityType:
        listening = 2

    class Status:
        dnd = "dnd"
        online = "online"
        idle = "idle"

    class Activity:
        def __init__(self, type=None, name=None):
            self.type = type
            self.name = name

    class Game:
        def __init__(self, name=None):
            self.name = name

    class DMChannel:
        pass

    class Interaction:
        pass

    class File:
        def __init__(self, fp, filename=None):
            self.fp = fp
            self.filename = filename

    class FFmpegPCMAudio:
        def __init__(self, source, executable="ffmpeg"):
            self.source = source

    class _Opus:
        @staticmethod
        def is_loaded():
            return False

        @staticmethod
        def load_opus(name):
            raise RuntimeError("opus stub: not available in tests")

    class Intents:
        def __init__(self):
            self.message_content = False

        @staticmethod
        def default():
            return Intents()

    class _FakeUser:
        id = 0

    class Client:
        def __init__(self, intents=None):
            self.user = _FakeUser()
            self.guilds = []
            self.loop = None

        def event(self, coro):
            return coro

        def get_guild(self, gid):
            return None

        def is_ready(self):
            return False

        async def change_presence(self, **kwargs):
            pass

        def run(self, token):
            raise RuntimeError("discord stub: refusing to connect")

    class View:
        def __init__(self, timeout=None):
            self.timeout = timeout

        def add_item(self, item):
            pass

    class CommandTree:
        def __init__(self, client):
            self.client = client
            self._cmds = {}

        def command(self, name=None, description=None):
            def deco(fn):
                self._cmds[name or fn.__name__] = fn
                return fn
            return deco

        def copy_global_to(self, guild=None):
            pass

        async def sync(self, guild=None):
            return []

    def describe(**kwargs):
        def deco(fn):
            return fn
        return deco

    def autocomplete(**kwargs):
        def deco(fn):
            return fn
        return deco

    class Choice:
        def __init__(self, name=None, value=None):
            self.name = name
            self.value = value

    ui.View = View
    app_commands.CommandTree = CommandTree
    app_commands.describe = describe
    app_commands.autocomplete = autocomplete
    app_commands.Choice = Choice

    discord.Activity = Activity
    discord.ActivityType = ActivityType
    discord.Client = Client
    discord.DMChannel = DMChannel
    discord.FFmpegPCMAudio = FFmpegPCMAudio
    discord.File = File
    discord.Game = Game
    discord.Intents = Intents
    discord.Interaction = Interaction
    discord.Status = Status
    discord.opus = _Opus
    discord.ui = ui
    discord.app_commands = app_commands
    # `discord.py` dotted name seen in a grep of bot.py is just the
    # distribution name in a comment/string, not an attribute.

    sys.modules["discord"] = discord
    sys.modules["discord.app_commands"] = app_commands
    sys.modules["discord.ui"] = ui
    return discord


_build_discord_stub()

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import bot  # noqa: E402  (imported after stub + env pinning)
