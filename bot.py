#!/usr/bin/env python3
"""opencode-discord bot: two-way DM chat between Discord and opencode.

- DMs the bot -> forwarded to `opencode run`; the reply comes back to the DM.
- In guilds: reacts to @mentions or `!oc` prefix (per-channel sessions).
- `!new` resets your session, `!help` shows help.
- Direct file drops ("place this in <dir>" + attachments) move files
  without involving opencode, so model content moderation never applies.
- Direct sends ("send me <path>") upload without involving opencode.
- Rapid messages coalesce: arrivals during a running turn merge into a
  single follow-up turn per session instead of queueing N runs.
- Global cap: at most MAX_CONCURRENT_TURNS opencode runs at once
  (default 2, 0 = unlimited); extra turns wait FIFO, still show DND,
  and appear in `!status`.
- Presence: Listening when idle, DND "working..." while any turn runs.
- Other attachments go to opencode via --file (text/code/images incl.
  gif) or path-only (video); `[[attach:path]]` markers come back as files.
- No Hermes, no gateway besides this bot. discord-send (sibling script)
  covers the other direction for one-shot sends from shell/opencode.

Config via env (or .env in cwd / ~/.config/opencode-discord/):
    DISCORD_BOT_TOKEN   required
    ALLOWED_USER_IDS    comma-separated Discord user ids; empty = anyone (warns)
    OPENCODE_BIN        default "opencode"
    OPENCODE_DIR        working dir for opencode runs (default cwd)
    FILE_JAIL         allowlist root for drop targets + send sources
                      (default OPENCODE_DIR; empty also fails closed to
                      OPENCODE_DIR). DMs and guilds share the same rule.
    OPENCODE_MODEL      optional, passed as -m
    OPENCODE_AGENT      optional, passed as --agent
    OPENCODE_AUTO       "1" to pass --auto (auto-approve tools). Default "0".
                        Only enable for users you trust; it lets the agent
                        run shell commands and edit files unattended.
    OPENCODE_TIMEOUT    seconds per run (default 600)
    MAX_CONCURRENT_TURNS  max simultaneous opencode runs across all chats
                        (default 2; extra turns wait FIFO, see `!status`;
                        0 = unlimited)
    STATE_FILE          default ./state/sessions.json
    GUILD_PREFIX        default "!oc"
    ATTACH_DIR          default ./attachments (inbound inbox)
    MAX_ATTACH_MB       default 25 (in/out file size cap)
    ATTACH_KEEP_FILES   default 50 (inbox prune: newest N kept, 0 = unlimited)
    ATTACH_KEEP_MB      default 500 (inbox prune: total MB cap, 0 = unlimited)
    LOG_LEVEL           default INFO (e.g. DEBUG for verbose)
    REPLY_AS_FILE_LIMIT default 4000 (longer replies sent as reply.md; 0 disables)
    REACT_START/DONE/ERROR  default hourglass/check/cross (empty disables)
    VOICEBOX_URL        default http://127.0.0.1:17493 (Voicebox TTS; empty disables voice)
    VOICEBOX_PROFILE    default "Computer" (voice profile name or id)
    VOICEBOX_VOICE      "1" = attach a spoken reply to every turn (default "1");
                        "0" = text-only unless a chat opts in with `!voice on`
    VOICEBOX_TIMEOUT    seconds per TTS request (default 120)
    VOICEBOX_MAX_CHARS  max chars sent to TTS per turn (default 1200, rest stays text-only)
    VOICEBOX_CACHE_DIR  content-hash cache for TTS wav output (default
                        "tts_cache"; empty disables). A hit skips Voicebox
                        entirely - repeats (!say reruns, greetings, status
                        phrases) cost nothing.
    VOICEBOX_CACHE_FILES default 500 (cache prune: newest N kept, 0 = unlimited)
    VOICEBOX_CACHE_MB   default 200 (cache prune: total MB cap, 0 = unlimited)
    VC_CHUNK_CHARS      max chars per spoken chunk when streaming to VC
                        (default 400; smaller = first audio sooner)
    VOICEBOX_TRANSCRIBE "1" = transcribe inbound voice notes/audio via Voicebox
                        Whisper before opencode sees them (default "1"); "0" keeps
                        the old behavior (raw audio passed with --file)
    VOICEBOX_STT_MODEL  optional Whisper size (base/small/medium/large/turbo);
                        empty = server default
    VOICEBOX_LANGUAGE   optional STT language hint (e.g. "en"); empty = auto
    FFMPEG_BIN          ffmpeg executable for voice-channel playback (default "ffmpeg")
    OPUS_LIB            optional explicit path to the Opus DLL (default: looks for
                        libopus-0.x64.dll / opus.dll next to bot.py, then system "opus")
    SAY_DIR             dir watched for *.txt drop-ins the bot speaks in VC (default
                        "say_queue"). "<guildid>_*.txt" targets one server, plain
                        "*.txt" plays in every connected VC. Empty disables.
    SAY_POLL            seconds between say-queue scans (default 2.0)
    SAY_MAX_BYTES       largest say file accepted (default 8192; bigger is skipped)
    VC_AUTOJOIN         voice channel id to join on startup (empty disables)
    VC_AUTOREJOIN       "1" = rejoin the autojoin channel if disconnected
                        unexpectedly (default "1"; `!leave` still sticks)
    VC_AUTOLEAVE_MINUTES minutes of sitting alone in a VC before the bot
                        disconnects itself (default 5.0; 0 disables).
                        `!join` afterwards works normally.
    VOICEBOX_WARMUP     "1" = one silent TTS at startup so the first real reply
                        doesn't pay model-load cost (default "1")
    VOICE_USER_ID       your Discord user id; used by `!voiceready` to check
                        you're actually sitting in the VC (empty disables that check)

    Voice channels (guilds only, text stays the input):
    `!join` pulls the bot into your current voice channel; every voice-enabled
    reply is then spoken there automatically instead of attaching reply.wav.
    `!leave` disconnects. DMs have no voice channel, so reply.wav attaches as before.

Required Discord privileged intent: Message Content (toggle in the
Developer Portal -> Bot -> Privileged Gateway Intents).
"""

import asyncio
import collections
import hashlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from chunking import split_smart

try:
    import discord
    from discord import app_commands
except ImportError:
    sys.exit("discord.py not installed: pip install -r requirements.txt")

# Windows consoles default to cp1252, which mangles opencode's UTF-8 output
# (emoji becomes ðŸ‘‹-style mojibake) and can crash logging on real emoji.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

MAX_DISCORD = 2000


def load_dotenv():
    for path in (".env",
                 os.path.expanduser("~/.config/opencode-discord/.env")):
        p = Path(path)
        if p.is_file():
            for line in p.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip("'\""))


load_dotenv()


def _env_int(name, default):
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        print(f"WARNING: {name} invalid ({os.environ.get(name)!r}) - "
              f"using {default}", file=sys.stderr)
        return default


def _env_float(name, default):
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        print(f"WARNING: {name} invalid ({os.environ.get(name)!r}) - "
              f"using {default}", file=sys.stderr)
        return default


TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
ALLOWED = {u.strip() for u in os.environ.get("ALLOWED_USER_IDS", "").split(",")
           if u.strip()}
OPENCODE_BIN = os.environ.get("OPENCODE_BIN", "opencode")
OPENCODE_DIR = os.environ.get("OPENCODE_DIR", os.getcwd())
FILE_JAIL_RAW = os.environ.get("FILE_JAIL", "").strip()
# Fail-closed: unset or empty FILE_JAIL jails to OPENCODE_DIR. There is
# deliberately no unlimited mode.
try:
    FILE_JAIL = Path(FILE_JAIL_RAW or OPENCODE_DIR).expanduser().resolve()
except OSError:
    FILE_JAIL = Path(os.path.abspath(
        os.path.expanduser(FILE_JAIL_RAW or OPENCODE_DIR)))
if not FILE_JAIL_RAW:
    print(f"FILE_JAIL empty/unset - jailing file drop/send "
          f"to OPENCODE_DIR ({FILE_JAIL})", file=sys.stderr)
OPENCODE_MODEL = os.environ.get("OPENCODE_MODEL", "").strip()
OPENCODE_AGENT = os.environ.get("OPENCODE_AGENT", "").strip()
OPENCODE_AUTO = os.environ.get("OPENCODE_AUTO", "0") == "1"
OPENCODE_TIMEOUT = _env_int("OPENCODE_TIMEOUT", 600)
MAX_CONCURRENT_TURNS = _env_int("MAX_CONCURRENT_TURNS", 2)
if MAX_CONCURRENT_TURNS < 0:
    print(f"WARNING: MAX_CONCURRENT_TURNS invalid ({MAX_CONCURRENT_TURNS}) - "
          "using 2", file=sys.stderr)
    MAX_CONCURRENT_TURNS = 2
STATE_FILE = Path(os.environ.get("STATE_FILE", "state/sessions.json"))
GUILD_PREFIX = os.environ.get("GUILD_PREFIX", "!oc")
ATTACH_DIR = Path(os.environ.get("ATTACH_DIR", "attachments"))
MAX_ATTACH_MB = _env_float("MAX_ATTACH_MB", 25)
ATTACH_KEEP_FILES = _env_int("ATTACH_KEEP_FILES", 50)
ATTACH_KEEP_MB = _env_float("ATTACH_KEEP_MB", 500)
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
REPLY_AS_FILE_LIMIT = _env_int("REPLY_AS_FILE_LIMIT", 4000)
REACT_START = os.environ.get("REACT_START", "\u23f3")
REACT_DONE = os.environ.get("REACT_DONE", "\u2705")
REACT_ERROR = os.environ.get("REACT_ERROR", "\u274c")
VOICEBOX_URL = os.environ.get("VOICEBOX_URL", "http://127.0.0.1:17493").strip().rstrip("/")
VOICEBOX_PROFILE = os.environ.get("VOICEBOX_PROFILE", "Computer").strip()
VOICEBOX_VOICE = os.environ.get("VOICEBOX_VOICE", "1") == "1"
VOICEBOX_TIMEOUT = _env_int("VOICEBOX_TIMEOUT", 120)
VOICEBOX_MAX_CHARS = _env_int("VOICEBOX_MAX_CHARS", 1200)
VOICEBOX_CACHE_DIR = os.environ.get("VOICEBOX_CACHE_DIR", "tts_cache").strip()
VOICEBOX_CACHE_FILES = _env_int("VOICEBOX_CACHE_FILES", 500)
VOICEBOX_CACHE_MB = _env_int("VOICEBOX_CACHE_MB", 200)
VOICEBOX_TRANSCRIBE = os.environ.get("VOICEBOX_TRANSCRIBE", "1") == "1"
VOICEBOX_STT_MODEL = os.environ.get("VOICEBOX_STT_MODEL", "").strip()
VOICEBOX_LANGUAGE = os.environ.get("VOICEBOX_LANGUAGE", "").strip()
FFMPEG_BIN = os.environ.get("FFMPEG_BIN", "ffmpeg").strip() or "ffmpeg"
OPUS_LIB = os.environ.get("OPUS_LIB", "").strip()
SAY_DIR = Path(os.environ.get("SAY_DIR", "say_queue").strip() or ".bot-voice-disabled")
SAY_DIR_ENABLED = os.environ.get("SAY_DIR", "say_queue").strip() != ""
SAY_POLL = _env_float("SAY_POLL", 2.0) or 2.0
SAY_MAX_BYTES = _env_int("SAY_MAX_BYTES", 8192)
VC_AUTOJOIN = os.environ.get("VC_AUTOJOIN", "").strip()
VC_AUTOREJOIN = os.environ.get("VC_AUTOREJOIN", "1") == "1"
VC_AUTOLEAVE_MINUTES = _env_float("VC_AUTOLEAVE_MINUTES", 5.0)
VOICEBOX_WARMUP = os.environ.get("VOICEBOX_WARMUP", "1") == "1"
VOICE_USER_ID = os.environ.get("VOICE_USER_ID", "").strip()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("discord-bot")
log.info("file jail: %s", FILE_JAIL)

# Explicit drop: "place/put/save/drop/move/copy this in(to) <path>"
# Handled directly by the bot (filesystem move) without involving opencode,
# so content moderation of the file bytes never comes into play.
DROP_RE = re.compile(
    r"^(?:place|put|save|drop|move|copy)\s+"
    r"(?:this|these|it|them|that|the\s+files?(?:\s+here)?)?\s*"
    r"(?:in(?:to)?|to)\s+(.+?)\s*$",
    re.IGNORECASE,
)

# Explicit send: "send me <path>" / "send <path>"
# Handled directly by the bot (discord.File upload) without involving
# opencode, so the model never judges the filename/content.
SEND_RE = re.compile(
    r"^send\s+(?:me\s+)?(.+?)\s*$",
    re.IGNORECASE,
)


class JailViolation(Exception):
    """A user/model-specified path resolves outside FILE_JAIL."""
    def __init__(self, raw, resolved):
        self.raw = raw
        self.resolved = resolved
        super().__init__(f"path outside file jail: {raw!r} -> {resolved}")


def _strip_extended_prefix(s):
    # \\?\C:\x -> C:\x ; \\?\UNC\host\share -> \\host\share
    if s.startswith("\\\\?\\"):
        rest = s[4:]
        if rest[:4].upper() == "UNC\\":
            return "\\\\" + rest[4:]
        return rest
    return s


def _resolve_candidate(path_str):
    """Expand ~, anchor relative paths under OPENCODE_DIR, resolve .. and
    symlinks/junctions as far as the filesystem allows (resolve() handles
    non-existent tails lexically). Returns (raw, resolved)."""
    raw = path_str.strip().strip("'\"")
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = Path(OPENCODE_DIR) / p
    try:
        resolved = p.resolve()
    except OSError:
        resolved = Path(os.path.abspath(str(p)))
    return raw, resolved


def jailed(path: Path) -> bool:
    """True when a resolved path is inside FILE_JAIL. Fail-closed: any
    resolution error returns False. Windows comparison is case-insensitive
    with both separators via normcase; extended-path prefixes stripped."""
    try:
        rp = path.resolve()
    except OSError:
        try:
            rp = Path(os.path.abspath(str(path)))
        except OSError:
            return False
    if os.name == "nt":
        rs = _strip_extended_prefix(os.path.normcase(str(rp)))
        js = _strip_extended_prefix(os.path.normcase(str(FILE_JAIL)))
        js = js.rstrip("\\")
        return rs == js or rs.startswith(js + "\\")
    try:
        return rp.is_relative_to(FILE_JAIL)
    except AttributeError:  # python <3.9
        try:
            rp.relative_to(FILE_JAIL)
            return True
        except ValueError:
            return False


def jail_error_message(raw):
    return (f"refused: `{raw}` is outside the file jail (`{FILE_JAIL}`). "
            "Drop targets and send sources must stay inside it.")


def resolve_target_dir(path_str):
    raw, p = _resolve_candidate(path_str)
    if not jailed(p):
        raise JailViolation(raw, p)
    return p


# Marker the model emits to send a file back: [[attach:D:\files\clip.mp4]]
ATTACH_RE = re.compile(r"\[\[attach:(.+?)\]\]", re.IGNORECASE)

# Marker the model emits to react to the user's message: [[react:EMOJI]]
# Literal unicode emoji (or custom <:name:id>); shortcodes won't resolve.
REACT_RE = re.compile(r"\[\[react:(.+?)\]\]", re.IGNORECASE)
MAX_MODEL_REACTS = 5

# Marker the model emits for a spoken-only aside: [[say:Deploy complete.]]
# Stripped from text, spoken in VC when live (else folded into reply.wav).
SAY_RE = re.compile(r"\[\[say:(.+?)\]\]", re.IGNORECASE)
MAX_MODEL_SAYS = 3

# Video is path-only: saved to disk, never passed via --file.
# The model can't usefully inline video bytes; it just needs the path
# so shell/file tools can move, copy, or re-send it.
# NOTE: .gif stays as --file (Discord/opencode treat image/gif as an image).
PATH_ONLY_EXTS = {
    ".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".mpg", ".mpeg",
    ".wmv", ".flv",
}


def use_file_flag(filename, content_type):
    ext = Path(filename).suffix.lower()
    if ext in PATH_ONLY_EXTS:
        return False
    if (content_type or "").split(";")[0].strip().lower().startswith("video/"):
        return False
    return True


BRIDGE_NOTE = (
    "[Discord bridge: you are chatting through Discord. Use emojis freely "
    "in your replies, and react to the user's messages with personality - "
    "match the vibe. To react, put [[react:EMOJI]] on its own line with "
    "a literal emoji (custom <:name:id> also works, "
    "shortcodes like :+1: do NOT). Max 5 per turn. "
    "Text/code/images (incl. gif) the user attaches are passed with --file "
    "AND saved locally; video files (mp4/mov/etc) are NOT inlined - they "
    "are only saved locally, use shell/file tools on the saved path. "
    "Voice/audio notes the user records are transcribed before you see them - "
    "the transcript appears as [Voice message NAME (Ns): text]; treat it as "
    "what the user said. "
    "To send a file back to the user, put [[attach:FULL_PATH]] on its own "
    "line, e.g. [[attach:D:\\files\\clip.mp4]]. Use absolute paths.]"
)

BRIDGE_VOICE_NOTE = (
    "[Voice output: this reply is ALSO spoken aloud {where}. Write to be "
    "heard: conclusion first, conversational, short - code/logs go in "
    "[[attach:]] files, never inline (inline code is read aloud literally "
    "and sounds terrible). For a spoken-only aside that stays out of the "
    "text, put [[say:your line]] on its own line (max 3 per turn).]"
)


def bridge_note_for(key, channel):
    """Base bridge note + voice paragraph when this chat is heard."""
    if not voice_enabled(key):
        return BRIDGE_NOTE
    guild = getattr(channel, "guild", None)
    vc = guild_voice_client(guild.id) if guild is not None else None
    if vc is not None and vc.is_connected():
        where = "live in the voice channel"
    else:
        where = "as an attached audio file"
    return f"{BRIDGE_NOTE}\n{BRIDGE_VOICE_NOTE.format(where=where)}"

if not TOKEN:
    _TOKEN_ERROR = "DISCORD_BOT_TOKEN is not set (env or .env file)"
else:
    _TOKEN_ERROR = None
if not ALLOWED:
    print("WARNING: ALLOWED_USER_IDS empty - any Discord user can DM the bot.",
          file=sys.stderr)


def load_state():
    """State shape: {"active": {...}, "known": {...}, "models": {...},
    "voice": {...}, "profiles": {...}}."""
    try:
        raw = json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {"active": {}, "known": {}, "models": {}, "voice": {},
                "profiles": {}}
    if isinstance(raw, dict) and isinstance(raw.get("active"), dict):
        known = raw.get("known")
        models = raw.get("models")
        voice = raw.get("voice")
        profiles = raw.get("profiles")
        return {"active": raw["active"],
                "known": known if isinstance(known, dict) else {},
                "models": models if isinstance(models, dict) else {},
                "voice": voice if isinstance(voice, dict) else {},
                "profiles": profiles if isinstance(profiles, dict) else {}}
    # legacy flat {key: sid}
    act = ({k: v for k, v in raw.items() if isinstance(v, str)}
           if isinstance(raw, dict) else {})
    return {"active": act,
            "known": {k: [v] for k, v in act.items()},
            "models": {}, "voice": {}, "profiles": {}}


def save_state():
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(
        {"active": SESSIONS, "known": KNOWN, "models": MODEL_OVERRIDES,
         "voice": VOICE_OVERRIDES, "profiles": VOICE_PROFILE_OVERRIDES},
        indent=2))


_STATE = load_state()
SESSIONS = _STATE["active"]  # key -> opencode session id
KNOWN = _STATE["known"]  # key -> [opencode session ids the bot has used]
MODEL_OVERRIDES = _STATE["models"]  # key -> "provider/model"
VOICE_OVERRIDES = _STATE["voice"]  # key -> bool (True = voice on)
VOICE_PROFILE_OVERRIDES = _STATE["profiles"]  # key -> profile name-or-id


def effective_voice_profile(key):
    """Per-chat voice profile override wins, else the global default."""
    return VOICE_PROFILE_OVERRIDES.get(key) or VOICEBOX_PROFILE


def remember_session(key, sid):
    if not sid:
        return
    lst = KNOWN.setdefault(key, [])
    if sid in lst:
        lst.remove(sid)
    lst.append(sid)
    del lst[:-30]
USAGE = {}  # key -> {"in": int, "out": int, "cost": float, "turns": int}


def extract_usage(ndjson_text):
    """Sum tokens/cost across step-finish events. Returns (in, out, cost)."""
    tin = tout = 0
    cost = 0.0
    for line in ndjson_text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        part = obj.get("part") if isinstance(obj, dict) else None
        if not isinstance(part, dict) or part.get("type") != "step-finish":
            continue
        tok = part.get("tokens") or {}
        try:
            tin += int(tok.get("input", 0) or 0)
            tout += int(tok.get("output", 0) or 0)
            cost += float(part.get("cost", 0) or 0)
        except (ValueError, TypeError):
            continue
    return tin, tout, cost


class TurnQueue:
    """Per-session coalescing queue: one runner at a time, late arrivals
    merge into a single follow-up turn instead of queueing N runs."""
    __slots__ = ("guard", "running", "buffer")

    def __init__(self):
        self.guard = asyncio.Lock()
        self.running = False
        self.buffer = []  # [(message, prompt, files)]


QUEUES = {}


def get_queue(key):
    q = QUEUES.get(key)
    if q is None:
        q = TurnQueue()
        QUEUES[key] = q
    return q


class TurnCancelled(Exception):
    """Raised inside run_opencode when !cancel kills the in-flight run."""
    pass


TURN_LOCK = threading.Lock()
TURN_STATE = {}  # key -> {"proc": Popen|None, "cancelled": bool}


ACTIVE_TURNS = 0
ACTIVE_GUARD = asyncio.Lock()

# Global turn gate: at most MAX_CONCURRENT_TURNS opencode runs at once.
# Explicit FIFO waiter queue (not a bare Semaphore) so wake order is
# structural, not an implementation detail of Semaphore wake-ups. Message
# intake is NOT gated: sessions keep accepting/coalescing while their turn
# waits. The slot covers the whole run_batches call (run + deliver_reply,
# voice TTS included) — one acquire/release pair, so a session's text+voice
# delivery stays atomic; slow TTS can briefly hold a slot, accepted
# deliberately for auditability. Waiting turns count in ACTIVE_TURNS, so
# presence stays DND while turns are queued. 0 = unlimited: the gate is
# bypassed entirely (no acquire/release, zero behavior change).
TURN_GATE_GUARD = asyncio.Lock()
TURN_SLOTS_HELD = 0
TURN_WAITERS = collections.deque()  # FIFO of {"key","event","cancelled","granted"}
TURN_WAITER_BY_KEY = {}  # key -> waiter entry while queued (for !cancel)
TREE_SYNCED = False

IDLE_ACTIVITY = discord.Activity(
    type=discord.ActivityType.listening, name="DMs | !oc in servers")
BUSY_ACTIVITY = discord.Game("working on your request...")


async def update_presence():
    try:
        if ACTIVE_TURNS > 0:
            await client.change_presence(
                status=discord.Status.dnd, activity=BUSY_ACTIVITY)
        else:
            await client.change_presence(
                status=discord.Status.online, activity=IDLE_ACTIVITY)
    except Exception as e:
        log.warning("presence update failed: %s", e)


async def acquire_turn_slot(key):
    """Take a global run slot in FIFO order.

    Raises TurnCancelled when !cancel fires while queued — the waiter then
    consumes no slot and posts nothing. Bypassed when unlimited.
    """
    global TURN_SLOTS_HELD
    if MAX_CONCURRENT_TURNS <= 0:
        return
    async with TURN_GATE_GUARD:
        if TURN_SLOTS_HELD < MAX_CONCURRENT_TURNS and not TURN_WAITERS:
            TURN_SLOTS_HELD += 1
            log.debug("[%s] turn slot granted at once (%d/%d)",
                      key, TURN_SLOTS_HELD, MAX_CONCURRENT_TURNS)
            return
        entry = {"key": key, "event": asyncio.Event(),
                 "cancelled": False, "granted": False}
        TURN_WAITERS.append(entry)
        TURN_WAITER_BY_KEY[key] = entry
        log.info("[%s] waiting for a turn slot (%d waiting)",
                 key, len(TURN_WAITERS))
    await entry["event"].wait()
    # Woken by release_turn_slot (granted) or cancel_waiting_turn. The map
    # pop is guard-free: dict ops between awaits can't interleave, and the
    # entry is ours alone from here on (one waiter per session, and a later
    # cancel finds nothing once popped).
    TURN_WAITER_BY_KEY.pop(key, None)
    if entry["cancelled"]:
        if entry["granted"]:
            # Slot was handed over just as !cancel fired: pass it on at
            # once so capacity is never leaked.
            await release_turn_slot()
        raise TurnCancelled()
    # Granted and wanted: release_turn_slot already counted the slot.


async def release_turn_slot():
    """Free one slot, handing it to the oldest non-cancelled waiter (FIFO).

    Cancelled entries still queued are skipped without taking a slot.
    No-op when unlimited.
    """
    global TURN_SLOTS_HELD
    if MAX_CONCURRENT_TURNS <= 0:
        return
    async with TURN_GATE_GUARD:
        TURN_SLOTS_HELD -= 1
        while TURN_WAITERS:
            nxt = TURN_WAITERS.popleft()
            if nxt.get("cancelled"):
                continue  # !cancel already woke it; it takes no slot.
            nxt["granted"] = True
            TURN_SLOTS_HELD += 1
            log.debug("[%s] turn slot granted from queue (%d/%d)",
                      nxt["key"], TURN_SLOTS_HELD, MAX_CONCURRENT_TURNS)
            nxt["event"].set()
            break


async def cancel_waiting_turn(key):
    """Abort a turn queued for a global slot. Returns True when one was.

    Never consumes a slot: still-queued entries are dequeued, and an entry
    granted-but-not-yet-woken is flagged so its waker hands the slot on.
    """
    if MAX_CONCURRENT_TURNS <= 0:
        return False
    async with TURN_GATE_GUARD:
        entry = TURN_WAITER_BY_KEY.get(key)
        if entry is None:
            return False
        entry["cancelled"] = True
        try:
            TURN_WAITERS.remove(entry)
        except ValueError:
            pass  # already popped by release; the waker releases the slot.
        entry["event"].set()
        log.info("[%s] turn cancelled while waiting for a slot", key)
        return True


def extract_session_id(ndjson_text):
    """Best-effort session-id extraction from `opencode run --format json`."""
    for line in ndjson_text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        for key in ("session_id", "sessionID", "sessionId"):
            if isinstance(obj.get(key), str):
                return obj[key]
        # some schemas nest: {"session": {"id": ...}} or event payloads
        for key in ("session", "payload", "data", "event"):
            sub = obj.get(key)
            if isinstance(sub, dict):
                for k in ("id", "session_id", "sessionID"):
                    if isinstance(sub.get(k), str):
                        return sub[k]
    return None


def strip_json_events(ndjson_text):
    """Pull human-readable text out of opencode JSON event output."""
    seen_types = set()

    # Event types that may carry assistant chat text. Anything else
    # (steps, sessions, agent notices, ...) is skipped to keep
    # operational chatter out of Discord.
    CHAT_TYPES = {"text", "message", "result", "response", "assistant",
                  "output"}

    def is_chat_event(obj):
        if not isinstance(obj, dict):
            return True  # plain-text lines handled by the JSON fallback
        t = obj.get("type")
        part = obj.get("part")
        ptype = part.get("type") if isinstance(part, dict) else None
        if ptype == "tool" or t == "tool_use":
            return False  # tool I/O is not chat text
        if ptype == "text":
            return True
        if t is None:
            return True  # untyped object: inspect content
        return t in CHAT_TYPES

    # Tool I/O envelope keys: never chat text, even when nested elsewhere.
    TOOL_KEYS = {"state", "metadata", "providerCall", "providerResult",
                 "rawInput"}

    def collect_text(obj, depth=0):
        # Recursively collect human text; returns list of strings.
        found = []
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in TOOL_KEYS:
                    continue
                if k in ("text", "content", "delta", "message") and isinstance(v, str) and v:
                    # skip obvious non-text: ids, paths, types
                    if k == "message" and v in ("step-start",):
                        continue
                    found.append(v)
                elif isinstance(v, (dict, list)):
                    found.extend(collect_text(v, depth + 1))
        elif isinstance(obj, list):
            for item in obj:
                found.extend(collect_text(item, depth + 1))
        return found

    def run_lines(strict):
        out = []
        for line in ndjson_text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                out.append(line)
                continue
            if isinstance(obj, dict):
                t = obj.get("type")
                if isinstance(t, str):
                    seen_types.add(t)
                part = obj.get("part")
                if isinstance(part, dict):
                    ptype = part.get("type")
                    if isinstance(ptype, str):
                        seen_types.add(f"part:{ptype}")
                    if ptype == "tool" or t == "tool_use":
                        continue  # tool I/O is not chat text
                    if strict and not is_chat_event(obj):
                        continue
                    text = part.get("text")
                    if isinstance(text, str) and text:
                        out.append(text)
                        continue
                elif strict and not is_chat_event(obj):
                    continue
                if t == "tool_use":
                    continue
                # fall back to recursive collect for message/result events
                for s in collect_text(obj):
                    # skip id-like strings and timestamps
                    if s.startswith(("ses_", "prt_", "msg_")):
                        continue
                    if s in ("step-start", "step-finish", "text"):
                        continue
                    out.append(s)
            else:
                out.append(str(obj))
        return out

    strict = run_lines(True)
    if seen_types:
        log.debug("opencode event types: %s", sorted(seen_types))
    text = "".join(strict).strip()
    if text:
        return text
    if not ndjson_text.strip():
        return ""
    # Last resort: legacy greedy pass (minus tool events) so unknown
    # future schemas degrade to noise rather than silence - and say so.
    log.warning("no chat-text events parsed, falling back to greedy "
                "extract; types=%s", sorted(seen_types))
    return "".join(run_lines(False)).strip()

def run_opencode(prompt, session_key, files=None, model=None):
    """Run one opencode turn. Returns (reply_text, session_id_or_None)."""
    cmd = [OPENCODE_BIN, "run", "--format", "json"]
    session_id = SESSIONS.get(session_key)
    if session_id:
        cmd += ["--session", session_id]
    m = model or OPENCODE_MODEL
    if m:
        cmd += ["-m", m]
    if OPENCODE_AGENT:
        cmd += ["--agent", OPENCODE_AGENT]
    if OPENCODE_AUTO:
        cmd += ["--auto"]
    for f in files or []:
        cmd += ["--file", str(f)]
    cmd += ["--", prompt]

    log.info("[%s] opencode start: session=%s files=%d timeout=%ds cwd=%s",
             session_key, session_id or "(new)", len(files or []),
             OPENCODE_TIMEOUT, OPENCODE_DIR)
    log.debug("[%s] cmd: %s", session_key, cmd)
    with TURN_LOCK:
        st = TURN_STATE.setdefault(
            session_key, {"proc": None, "cancelled": False})
    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(
            cmd, cwd=OPENCODE_DIR, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace")
    except FileNotFoundError:
        log.error("[%s] could not execute %r", session_key, OPENCODE_BIN)
        return (f"could not execute `{OPENCODE_BIN}` - is opencode installed "
                "and on PATH?"), session_id
    except OSError as e:
        log.error("[%s] could not start %r: %s", session_key, OPENCODE_BIN, e)
        return (f"could not execute `{OPENCODE_BIN}` - is opencode installed "
                "and on PATH?"), session_id
    with TURN_LOCK:
        st["proc"] = proc
    try:
        out, err = proc.communicate(timeout=OPENCODE_TIMEOUT)
    except subprocess.TimeoutExpired:
        with TURN_LOCK:
            cancelled = st.get("cancelled")
        try:
            proc.kill()
        except Exception:
            pass
        try:
            out, err = proc.communicate(timeout=30)
        except Exception:
            out, err = "", ""
        if cancelled:
            log.info("[%s] opencode cancelled during run", session_key)
            raise TurnCancelled()
        log.warning("[%s] opencode timed out after %ds", session_key, OPENCODE_TIMEOUT)
        return (f"opencode timed out after {OPENCODE_TIMEOUT}s. "
                "Your session is kept; try a smaller task or `!new`."), session_id
    with TURN_LOCK:
        cancelled = st.get("cancelled")
    if cancelled:
        log.info("[%s] opencode cancelled during run", session_key)
        raise TurnCancelled()

    out = out or ""
    err = err or ""
    rc = proc.returncode
    dt = time.monotonic() - t0
    log.info("[%s] opencode done in %.1fs: rc=%d stdout=%d chars stderr=%d chars",
             session_key, dt, rc, len(out), len(err))
    log.debug("[%s] stdout:\n%s", session_key, out)
    if err:
        log.debug("[%s] stderr:\n%s", session_key, err)
    new_sid = extract_session_id(out) or session_id
    text = strip_json_events(out).strip()
    log.info("[%s] agent reply (%d chars): %r", session_key, len(text),
             text[:1500])
    tin, tout, cost = extract_usage(out)
    if tin or tout or cost:
        u = USAGE.setdefault(session_key,
                             {"in": 0, "out": 0, "cost": 0.0, "turns": 0})
        u["in"] += tin
        u["out"] += tout
        u["cost"] += cost
        u["turns"] += 1
        log.info("[%s] usage this turn: %d in / %d out tokens, $%.4f "
                 "(session: %d turns, %d in / %d out, $%.4f)",
                 session_key, tin, tout, cost,
                 u["turns"], u["in"], u["out"], u["cost"])
    if rc != 0 and not text:
        tail = err.strip()[-1500:]
        text = f"opencode exited with code {rc}."
        if tail:
            text += f"\n```\n{tail}\n```"
    if not text:
        text = "(opencode returned no text)"
    return text, new_sid


def safe_key(key):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", key)


def prune_inbox(inbox, key="?", keep_files=None, keep_mb=None):
    """Drop oldest files beyond count/byte caps (reply leftovers too).

    keep_files/keep_mb default to the ATTACH_KEEP_* globals; callers with
    their own caps (e.g. the TTS cache) pass explicit values."""
    if keep_files is None:
        keep_files = ATTACH_KEEP_FILES
    if keep_mb is None:
        keep_mb = ATTACH_KEEP_MB
    try:
        files = sorted(
            (p for p in inbox.iterdir() if p.is_file()),
            key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    pruned = 0
    if keep_files > 0:
        while len(files) > keep_files:
            old = files.pop(0)
            try:
                old.unlink()
                pruned += 1
            except OSError:
                pass
    if keep_mb > 0:
        try:
            total = sum(p.stat().st_size for p in files)
        except OSError:
            total = 0
        while files and total > keep_mb * 1024 * 1024:
            old = files.pop(0)
            try:
                total -= old.stat().st_size
                old.unlink()
                pruned += 1
            except OSError:
                pass
    if pruned:
        log.info("[%s] pruned %d old file(s)", key, pruned)


def split_attach_markers(reply):
    """Strip [[attach:path]] markers. Returns (clean_text, [paths])."""
    paths = [m.group(1).strip().strip("'\"") for m in ATTACH_RE.finditer(reply)]
    clean = ATTACH_RE.sub("", reply).strip()
    # collapse 3+ blank lines left behind by removed markers
    clean = re.sub(r"\n{3,}", "\n\n", clean)
    return clean, paths


def split_react_markers(reply):
    """Strip [[react:emoji]] markers. Returns (clean_text, [emojis])."""
    emojis = [m.group(1).strip() for m in REACT_RE.finditer(reply)
              if m.group(1).strip()]
    clean = REACT_RE.sub("", reply).strip()
    clean = re.sub(r"\n{3,}", "\n\n", clean)
    return clean, emojis[:MAX_MODEL_REACTS]


def split_say_markers(reply):
    """Strip [[say:line]] markers. Returns (clean_text, [lines])."""
    lines = [m.group(1).strip() for m in SAY_RE.finditer(reply)
             if m.group(1).strip()]
    clean = SAY_RE.sub("", reply).strip()
    clean = re.sub(r"\n{3,}", "\n\n", clean)
    return clean, lines[:MAX_MODEL_SAYS]


def resolve_outbound(path_str):
    raw, p = _resolve_candidate(path_str)
    if not jailed(p):
        raise JailViolation(raw, p)
    return p


def voice_enabled(key):
    """Per-chat override wins, else the VOICEBOX_VOICE global default."""
    if key in VOICE_OVERRIDES:
        return bool(VOICE_OVERRIDES[key])
    return VOICEBOX_VOICE


VOICE_MODE_FLAG = Path(__file__).resolve().parent / ".opencode" / "voice-mode.on"


def voice_mode_on():
    return VOICE_MODE_FLAG.is_file()


def voice_mode_off(reason):
    """Fail-safe: disable agent voice mode. Returns True if it was on."""
    try:
        if VOICE_MODE_FLAG.is_file():
            VOICE_MODE_FLAG.unlink()
            log.warning("voice mode auto-disabled: %s", reason)
            return True
    except OSError as e:
        log.warning("voice mode auto-disable failed: %s", e)
    return False


# Consecutive Voicebox connection failures (reachable-but-error HTTP
# responses don't count — only down/unreachable). Trips the auto-off.
VOICEBOX_FAILS = {"n": 0}
VOICEBOX_FAIL_LIMIT = 3


def note_voicebox_result(ok):
    if ok:
        VOICEBOX_FAILS["n"] = 0
        return
    VOICEBOX_FAILS["n"] += 1
    if VOICEBOX_FAILS["n"] >= VOICEBOX_FAIL_LIMIT and voice_mode_on():
        voice_mode_off(
            f"Voicebox unreachable ({VOICEBOX_FAILS['n']}x) - "
            "say `voice mode on` to re-enable once it's back")


def voicebox_ping(timeout=5):
    """Fast reachability probe. Returns (ok, detail_ms_or_error)."""
    import time as _t
    if not VOICEBOX_URL:
        return False, "VOICEBOX_URL empty (voice disabled)"
    t0 = _t.monotonic()
    try:
        req = urllib.request.Request(
            f"{VOICEBOX_URL}/profiles",
            headers={"X-Voicebox-Client-Id": "discord-bot"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read(64)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"[:160]
    return True, f"{(_t.monotonic() - t0) * 1000:.0f}ms"


async def voice_ready():
    """Readiness gate. Returns [(label, ok, detail)]. Never raises."""
    results = []
    ok, detail = await asyncio.to_thread(voicebox_ping)
    results.append(("voicebox", ok, detail))
    results.append(("discord", client.is_ready(),
                    f"logged in as {client.user}" if client.is_ready()
                    else "not connected"))
    live = connected_guild_ids()
    results.append(("bot-vc", bool(live),
                    f"in {len(live)} VC(s)" if live
                    else "not in any voice channel (`!join`)"))
    if VOICE_USER_ID:
        try:
            uid = int(VOICE_USER_ID)
        except ValueError:
            results.append(("you-in-vc", False,
                            f"VOICE_USER_ID invalid ({VOICE_USER_ID!r})"))
            uid = None
        if uid is not None:
            where = None
            for gid in live:
                g = client.get_guild(gid)
                if g is None:
                    continue
                try:
                    m = g.get_member(uid) or await g.fetch_member(uid)
                except Exception as e:
                    where = f"lookup failed: {e}"[:120]
                    continue
                vs = getattr(m, "voice", None)
                if vs is not None and vs.channel is not None:
                    vc = g.voice_client
                    same = vc is not None and vc.channel is not None \
                        and vc.channel.id == vs.channel.id
                    where = (f"in #{vs.channel.name}"
                             f"{' (with bot)' if same else ' (NOT with bot)'}")
                    if same:
                        break
            results.append(("you-in-vc", where is not None
                            and "(with bot)" in where,
                            where or "not in any voice channel"))
    else:
        results.append(("you-in-vc", True, "skipped (VOICE_USER_ID empty)"))
    return results


def clean_for_tts(reply):
    """Strip markers/markdown the listener should never hear. Truncated."""
    text, _ = split_attach_markers(reply)
    text, _ = split_react_markers(text)
    # fenced code blocks -> keep the inner text, drop the fence language tag
    text = re.sub(r"```\w*\n?", " ", text)
    text = text.replace("```", " ")
    text = re.sub(r"`([^`]*)`", r"\1", text)
    # markdown links/images -> keep the label
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip()
    if VOICEBOX_MAX_CHARS > 0 and len(text) > VOICEBOX_MAX_CHARS:
        cut = text[:VOICEBOX_MAX_CHARS]
        # prefer a sentence boundary so speech doesn't stop mid-word
        for sep in (". ", "! ", "? ", "; ", ", ", " "):
            i = cut.rfind(sep)
            if i > VOICEBOX_MAX_CHARS // 2:
                cut = cut[:i + 1]
                break
        text = cut.strip()
    return text


def list_voicebox_profiles():
    """Fetch [(id, name)] from Voicebox. Empty list on any failure."""
    if not VOICEBOX_URL:
        return []
    try:
        req = urllib.request.Request(
            f"{VOICEBOX_URL}/profiles",
            headers={"X-Voicebox-Client-Id": "discord-bot"})
        with urllib.request.urlopen(req, timeout=15) as r:
            items = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        log.warning("voicebox profiles failed: %s", e)
        return []
    if not isinstance(items, list):
        return []
    return [(e.get("id"), e.get("name")) for e in items
            if isinstance(e, dict)]


_VOICE_PROFILE_CACHE = {}  # profile name -> {"at": monotonic, "id": pid}
_VOICE_PROFILE_TTL = 3600


def _resolve_profile_name(name):
    """Resolve a Voicebox profile name-or-id to an id. None on failure."""
    if not name:
        return None
    now = time.monotonic()
    hit = _VOICE_PROFILE_CACHE.get(name)
    if hit and now - hit["at"] < _VOICE_PROFILE_TTL:
        return hit["id"]
    items = list_voicebox_profiles()
    for pid, pname in items:
        if str(pid) == name:
            _VOICE_PROFILE_CACHE[name] = {"at": now, "id": pid}
            return pid
    want = name.lower()
    for pid, pname in items:
        if str(pname or "").lower() == want:
            _VOICE_PROFILE_CACHE[name] = {"at": now, "id": pid}
            log.info("voicebox profile %r -> %s", pname, pid)
            return pid
    log.warning("voicebox profile %r not found (%d profiles)",
                name, len(items))
    return None


def get_voicebox_profile_id(key=None):
    """Resolve the effective profile (per-chat override else global)."""
    return _resolve_profile_name(effective_voice_profile(key))


def tts_wav(reply_text, key=None):
    """Blocking Voicebox TTS. Returns wav bytes or None (never raises)."""
    if not VOICEBOX_URL:
        return None
    return tts_wav_clean(clean_for_tts(reply_text or ""), key)


def tts_wav_clean(text, key=None):
    """Blocking Voicebox TTS of pre-cleaned text. None on any failure."""
    if not VOICEBOX_URL or not (text or "").strip():
        return None
    text = text.strip()
    pid = get_voicebox_profile_id(key)
    if not pid:
        return None
    hit = _tts_cache_get(pid, text)
    if hit is not None:
        return hit
    payload = json.dumps(
        {"profile_id": pid, "text": text, "language": "en"}).encode()
    try:
        req = urllib.request.Request(
            f"{VOICEBOX_URL}/generate/stream", data=payload,
            headers={"Content-Type": "application/json",
                     "X-Voicebox-Client-Id": "discord-bot"})
        with urllib.request.urlopen(req, timeout=VOICEBOX_TIMEOUT) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace")[:500]
        except Exception:
            detail = ""
        log.warning("voicebox TTS http %s: %s", e.code, detail)
        note_voicebox_result(True)  # reachable, refused for cause
        return None
    except Exception as e:
        log.warning("voicebox TTS failed: %s", e)
        note_voicebox_result(False)  # unreachable -> counts toward auto-off
        return None
    if len(body) < 1000:
        log.warning("voicebox TTS returned %d bytes (not audio?)", len(body))
        return None
    log.info("voicebox TTS: %d chars -> %d bytes", len(text), len(body))
    note_voicebox_result(True)
    _tts_cache_put(pid, text, body)
    return body


def _tts_cache_key(profile_id, text):
    """Content hash: identical (profile, text) hits, anything else misses."""
    h = hashlib.sha256()
    h.update(profile_id.encode("utf-8"))
    h.update(b"\0")
    h.update(text.encode("utf-8"))
    return h.hexdigest()


def _tts_cache_get(profile_id, text):
    """Return cached wav bytes on hit, None on miss or corrupt entry.

    Corrupt entries (shorter than the not-audio guard) are deleted, never
    served — the caller falls through to a fresh TTS request."""
    if not VOICEBOX_CACHE_DIR:
        return None
    path = Path(VOICEBOX_CACHE_DIR) / (_tts_cache_key(profile_id, text)
                                      + ".wav")
    try:
        data = path.read_bytes()
    except OSError:
        log.debug("voicebox TTS cache miss")
        return None
    if len(data) < 1000:
        try:
            path.unlink()
        except OSError:
            pass
        log.warning("voicebox TTS cache: dropped corrupt entry %s", path.name)
        return None
    try:
        os.utime(path, None)  # recency for mtime-ordered pruning
    except OSError:
        pass
    log.info("voicebox TTS cache hit: %d bytes", len(data))
    return data


def _tts_cache_put(profile_id, text, data):
    """Store wav bytes under the content hash; prune to caps. Never raises."""
    if not VOICEBOX_CACHE_DIR or not data:
        return
    cdir = Path(VOICEBOX_CACHE_DIR)
    try:
        cdir.mkdir(parents=True, exist_ok=True)
        final = cdir / (_tts_cache_key(profile_id, text) + ".wav")
        fd, tmp = tempfile.mkstemp(prefix="tts-", suffix=".wav",
                                   dir=str(cdir))
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, final)  # atomic, Windows-safe
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        prune_inbox(cdir, key="tts-cache",
                    keep_files=VOICEBOX_CACHE_FILES,
                    keep_mb=VOICEBOX_CACHE_MB)
    except OSError as e:
        log.warning("voicebox TTS cache write failed: %s", e)


# Audio the bot transcribes via Voicebox instead of passing raw to opencode.
# Matches Voicebox's own accepted upload exts; Discord voice messages are .ogg.
STT_AUDIO_EXTS = {
    ".wav", ".mp3", ".m4a", ".ogg", ".oga", ".opus",
    ".flac", ".aac", ".webm",
}


def is_audio_attachment(filename, content_type):
    if Path(filename).suffix.lower() in STT_AUDIO_EXTS:
        return True
    return (content_type or "").split(";")[0].strip().lower().startswith(
        "audio/")


def transcribe_audio_file(path):
    """Blocking Voicebox STT. Returns (text, duration) or (None, None)."""
    if not VOICEBOX_URL:
        return None, None
    import mimetypes
    import uuid
    filename = Path(path).name
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    try:
        data = Path(path).read_bytes()
    except OSError as e:
        log.warning("STT read failed (%s): %s", path, e)
        return None, None
    boundary = uuid.uuid4().hex
    body = io.BytesIO()
    fields = {}
    if VOICEBOX_LANGUAGE:
        fields["language"] = VOICEBOX_LANGUAGE
    if VOICEBOX_STT_MODEL:
        fields["model"] = VOICEBOX_STT_MODEL
    for k, v in fields.items():
        body.write(
            f"--{boundary}\r\nContent-Disposition: form-data; "
            f"name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    body.write(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
        f"filename=\"{filename}\"\r\nContent-Type: {ctype}\r\n\r\n".encode())
    body.write(data)
    body.write(f"\r\n--{boundary}--\r\n".encode())
    for attempt in (1, 2):
        try:
            req = urllib.request.Request(
                f"{VOICEBOX_URL}/transcribe", data=body.getvalue(),
                headers={"Content-Type":
                         f"multipart/form-data; boundary={boundary}",
                         "X-Voicebox-Client-Id": "discord-bot"})
            with urllib.request.urlopen(req, timeout=VOICEBOX_TIMEOUT) as r:
                obj = json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                detail = ""
            log.warning("voicebox STT http %s: %s", e.code, detail)
            note_voicebox_result(True)  # reachable, refused for cause
            return None, None
        except Exception as e:
            log.warning("voicebox STT failed: %s", e)
            note_voicebox_result(False)  # unreachable -> counts toward auto-off
            return None, None
        text = (obj.get("text") or "").strip() if isinstance(obj, dict) else ""
        if text or attempt == 2:
            break
        log.warning("voicebox STT empty for %s, retrying once", filename)
    try:
        duration = float(obj.get("duration") or 0)
    except (ValueError, TypeError, AttributeError):
        duration = 0
    if not text:
        log.warning("voicebox STT returned no text for %s", filename)
        return None, None
    log.info("voicebox STT: %s (%.1fs) -> %d chars",
             filename, duration, len(text))
    note_voicebox_result(True)
    return text, duration


VC_CHUNK_CHARS = _env_int("VC_CHUNK_CHARS", 400)


def split_sentences(text, max_len=None):
    """Split speech text into <=max_len chunks at sentence boundaries."""
    if max_len is None or max_len <= 0:
        max_len = VC_CHUNK_CHARS
    cleaned = (text or "").strip()
    if not cleaned:
        return []
    parts = re.split(r"(?<=[.!?])\s+", cleaned)
    chunks, cur = [], ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if len(cur) + len(p) + 1 <= max_len:
            cur = (cur + " " + p).strip()
        else:
            if cur:
                chunks.append(cur)
            while len(p) > max_len:
                cut = p[:max_len]
                i = cut.rfind(" ")
                i = i if i > max_len // 2 else max_len
                chunks.append(p[:i].strip())
                p = p[i:].strip()
            cur = p
    if cur:
        chunks.append(cur)
    return chunks or [cleaned[:max_len]]


def ensure_opus():
    """Load the Opus codec needed for voice-channel audio. True when ready."""
    if discord.opus.is_loaded():
        return True
    here = Path(__file__).resolve().parent
    candidates = ([OPUS_LIB] if OPUS_LIB else []) + [
        str(here / "libopus-0.x64.dll"),
        str(here / "opus.dll"),
        "opus",
        "libopus-0",
    ]
    for c in candidates:
        try:
            discord.opus.load_opus(c)
        except Exception as e:
            log.debug("opus load via %r failed: %s", c, e)
            continue
        if discord.opus.is_loaded():
            log.info("opus loaded via %r", c)
            return True
    log.warning("opus not loaded - !join will fail until an Opus DLL is "
                "available (OPUS_LIB or libopus-0.x64.dll next to bot.py)")
    return False


VC_QUEUES = {}  # guild_id -> asyncio.Queue[(epoch, tmppath)]
VC_PLAYERS = {}  # guild_id -> asyncio.Task
VC_EPOCHS = {}  # guild_id -> int generation counter; bumped by !skip
VC_STREAMS = {}  # guild_id -> int active VC turn-stream deliveries


def guild_voice_client(guild_id):
    g = client.get_guild(guild_id)
    return g.voice_client if g else None


async def vc_player(guild_id):
    """Serial playback loop: one wav file at a time through the guild's VC."""
    q = VC_QUEUES[guild_id]
    while True:
        item = await q.get()
        try:
            if isinstance(item, tuple):
                epoch, tmppath = item
            else:
                epoch, tmppath = VC_EPOCHS.get(guild_id, 0), item
            if epoch != VC_EPOCHS.get(guild_id, 0):
                log.info("[guild:%s] VC dropping stale clip from prior epoch",
                         guild_id)
                try:
                    os.unlink(tmppath)
                except OSError:
                    pass
                continue
            vc = guild_voice_client(guild_id)
            if vc is None or not vc.is_connected():
                log.info("[guild:%s] VC gone, dropping queued clip", guild_id)
                try:
                    os.unlink(tmppath)
                except OSError:
                    pass
                continue
            done = asyncio.Event()

            def _after(err, path=tmppath, gid=guild_id):
                if err:
                    log.warning("[guild:%s] VC play error: %s", gid, err)
                try:
                    os.unlink(path)
                except OSError:
                    pass
                client.loop.call_soon_threadsafe(done.set)

            try:
                src = discord.FFmpegPCMAudio(tmppath, executable=FFMPEG_BIN)
            except Exception:
                log.exception("[guild:%s] ffmpeg source failed", guild_id)
                try:
                    os.unlink(tmppath)
                except OSError:
                    pass
                continue
            log.info("[guild:%s] speaking %s (%d queued)",
                     guild_id, tmppath, q.qsize())
            vc.play(src, after=_after)
            await done.wait()
        finally:
            q.task_done()


async def vc_say(guild_id, wav, inbox, epoch=None):
    """Queue wav bytes for VC playback. Returns False when not connected
    or when epoch mismatches (stale producer abandoned by !skip)."""
    vc = guild_voice_client(guild_id)
    if vc is None or not vc.is_connected():
        return False
    cur = VC_EPOCHS.get(guild_id, 0)
    if epoch is not None and epoch != cur:
        log.info("[guild:%s] VC abandoning stale chunk (epoch %d != %d)",
                 guild_id, epoch, cur)
        return False
    try:
        fd, tmppath = tempfile.mkstemp(
            prefix="vc-", suffix=".wav", dir=str(inbox))
        with os.fdopen(fd, "wb") as f:
            f.write(wav)
    except Exception:
        log.exception("[guild:%s] VC temp write failed", guild_id)
        return False
    q = VC_QUEUES.get(guild_id)
    if q is None:
        q = asyncio.Queue()
        VC_QUEUES[guild_id] = q
        VC_PLAYERS[guild_id] = asyncio.ensure_future(vc_player(guild_id))
    q.put_nowait((cur, tmppath))
    return True


def vc_drop(guild_id):
    """Empty a guild's playback queue, deleting unplayed clips."""
    q = VC_QUEUES.get(guild_id)
    if q is None:
        return
    n = 0
    while not q.empty():
        try:
            item = q.get_nowait()
        except asyncio.QueueEmpty:
            break
        tmppath = item[1] if isinstance(item, tuple) else item
        try:
            os.unlink(tmppath)
        except OSError:
            pass
        q.task_done()
        n += 1
    if n:
        log.info("[guild:%s] dropped %d queued VC clip(s)", guild_id, n)


VC_TMP = ATTACH_DIR / "_vc"  # staging for say-queue + VC playout clips
VC_EXPECTED_BYE = set()  # guild ids whose disconnect was a `!leave`
VC_AUTOLEAVE_TASKS = {}  # guild_id -> asyncio.Task (solo-idle countdown)
VC_AUTOLEAVE_AT = {}  # guild_id -> monotonic deadline (for `!status`)


def _vc_human_count(channel):
    """Non-bot members currently in a voice channel."""
    try:
        return sum(1 for m in channel.members
                   if not getattr(m, "bot", False))
    except Exception:
        return 1  # fail-safe: unknown membership never triggers a leave


def _cancel_autoleave(guild_id):
    task = VC_AUTOLEAVE_TASKS.pop(guild_id, None)
    VC_AUTOLEAVE_AT.pop(guild_id, None)
    if task is not None and not task.done():
        task.cancel()


def _arm_autoleave(guild_id, channel_id):
    """Start the solo-idle countdown. An already-running timer keeps its
    original deadline: the grace measures continuous alone-time."""
    task = VC_AUTOLEAVE_TASKS.get(guild_id)
    if task is not None and not task.done():
        return
    delay = VC_AUTOLEAVE_MINUTES * 60
    VC_AUTOLEAVE_AT[guild_id] = time.monotonic() + delay
    VC_AUTOLEAVE_TASKS[guild_id] = asyncio.ensure_future(
        _autoleave_countdown(guild_id, channel_id, delay))
    log.info("[guild:%s] VC auto-leave armed (%.1f min grace)",
             guild_id, VC_AUTOLEAVE_MINUTES)


def _refresh_autoleave(guild_id):
    """Arm the timer when the bot sits alone in VC; cancel otherwise."""
    if not VC_AUTOLEAVE_MINUTES or VC_AUTOLEAVE_MINUTES <= 0:
        _cancel_autoleave(guild_id)
        return
    vc = guild_voice_client(guild_id)
    if vc is None or not vc.is_connected() or vc.channel is None:
        _cancel_autoleave(guild_id)
        return
    if _vc_human_count(vc.channel) == 0:
        _arm_autoleave(guild_id, vc.channel.id)
    else:
        _cancel_autoleave(guild_id)


async def _autoleave_countdown(guild_id, channel_id, delay):
    task = asyncio.current_task()
    try:
        await asyncio.sleep(delay)
        # re-verify before leaving: never act on stale state (a join or
        # move since arming cancels the reason, not just the timer)
        vc = guild_voice_client(guild_id)
        if vc is None or not vc.is_connected():
            return
        ch = vc.channel
        if ch is None or ch.id != channel_id:
            return
        if _vc_human_count(ch) > 0:
            return
        VC_EXPECTED_BYE.add(guild_id)  # keep autorejoin from fighting this
        vc_drop(guild_id)
        try:
            vc.stop()
        except Exception:
            pass
        try:
            await vc.disconnect()
        except Exception as e:
            log.warning("[guild:%s] VC auto-leave disconnect failed: %s",
                        guild_id, e)
            return
        log.info("[guild:%s] VC auto-leave: alone %.1f min, disconnected",
                 guild_id, delay / 60)
    except asyncio.CancelledError:
        raise
    finally:
        if VC_AUTOLEAVE_TASKS.get(guild_id) is task:
            VC_AUTOLEAVE_TASKS.pop(guild_id, None)
            VC_AUTOLEAVE_AT.pop(guild_id, None)


SAY_STARTED = False
SAY_FAIL_AT = {}  # path -> last failed attempt (monotonic)


def say_targets(path):
    """Parse "<guildid>_name.txt" -> [guildid]; plain "*.txt" -> [] (= all)."""
    m = re.match(r"^(\d+)_", Path(path).name)
    return [int(m.group(1))] if m else []


def connected_guild_ids():
    return [g.id for g in client.guilds
            if g.voice_client is not None
            and g.voice_client.is_connected()]


async def process_say_file(path):
    """TTS one drop-in and queue it for VC. Returns True when consumed."""
    try:
        if path.stat().st_size > SAY_MAX_BYTES:
            log.warning("say-queue skipping oversize %s (%d bytes)",
                        path.name, path.stat().st_size)
            return True  # consume: never speakable
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError as e:
        log.warning("say-queue read failed (%s): %s", path.name, e)
        return True
    if not text:
        return True  # consume empty files silently
    targets = [g for g in (say_targets(path) or connected_guild_ids())]
    live = [g for g in targets if guild_voice_client(g) is not None
            and guild_voice_client(g).is_connected()]
    if not live:
        return False  # hold for later; nobody to speak to yet
    wav = await asyncio.to_thread(tts_wav, text)
    if not wav:
        last = SAY_FAIL_AT.get(str(path), 0.0)
        now = time.monotonic()
        if now - last < 60:
            return False  # backing off, keep file
        SAY_FAIL_AT[str(path)] = now
        log.warning("say-queue TTS failed, will retry: %s", path.name)
        return False
    VC_TMP.mkdir(parents=True, exist_ok=True)
    ok = False
    for gid in live:
        if await vc_say(gid, wav, VC_TMP):
            ok = True
            log.info("[guild:%s] say-queue: %s (%d chars)",
                     gid, path.name, len(text))
    return ok


async def say_watcher():
    """Background loop: speak *.txt drop-ins from SAY_DIR, then delete."""
    while True:
        try:
            await asyncio.sleep(SAY_POLL)
            if not SAY_DIR.is_dir():
                continue
            files = sorted(
                (p for p in SAY_DIR.iterdir()
                 if p.is_file() and p.suffix.lower() == ".txt"
                 and not p.name.startswith(".")),
                key=lambda p: p.stat().st_mtime)
            for p in files[:20]:
                try:
                    if await process_say_file(p):
                        try:
                            p.unlink()
                        except OSError:
                            pass
                        SAY_FAIL_AT.pop(str(p), None)
                except Exception:
                    log.exception("say-queue failed on %s", p.name)
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("say-watcher loop failed")


async def autojoin_vc():
    """Join VC_AUTOJOIN once at startup. Best-effort."""
    if not VC_AUTOJOIN:
        return
    try:
        cid = int(VC_AUTOJOIN)
    except ValueError:
        log.warning("VC_AUTOJOIN invalid (%r) - skipping", VC_AUTOJOIN)
        return
    ch = client.get_channel(cid)
    if ch is None:
        try:
            ch = await client.fetch_channel(cid)
        except Exception as e:
            log.warning("VC_AUTOJOIN channel %s unreachable: %s", cid, e)
            return
    if not hasattr(ch, "connect"):
        log.warning("VC_AUTOJOIN %s is not a voice channel", cid)
        return
    if not ensure_opus():
        log.warning("VC_AUTOJOIN skipped - opus not loaded")
        return
    try:
        vc = getattr(ch.guild, "voice_client", None)
        if vc is not None and vc.is_connected():
            if vc.channel is not None and vc.channel.id == ch.id:
                log.info("VC_AUTOJOIN already in %s", ch.name)
                return
            await vc.move_to(ch)
        else:
            await ch.connect()
        log.info("VC_AUTOJOIN joined %s", ch.name)
        _refresh_autoleave(ch.guild.id)
    except Exception as e:
        log.warning("VC_AUTOJOIN join failed: %s", e)


async def warmup_voice():
    """One silent TTS so the first real reply skips model-load cost."""
    if not VOICEBOX_WARMUP or not VOICEBOX_URL:
        return
    t0 = time.monotonic()
    wav = await asyncio.to_thread(tts_wav_clean, "Voice ready.")
    dt = time.monotonic() - t0
    if wav:
        log.info("voice warmup: %d bytes in %.1fs", len(wav), dt)
    else:
        log.warning("voice warmup failed - first reply will pay load cost")


async def autorejoin_vc(guild_id, delay=10):
    """Rejoin the autojoin channel after an unexpected disconnect."""
    await asyncio.sleep(delay)
    if not VC_AUTOJOIN or not VC_AUTOREJOIN:
        return
    if guild_voice_client(guild_id) is not None:
        return  # already back somehow
    try:
        if int(VC_AUTOJOIN or 0):
            ch = client.get_channel(int(VC_AUTOJOIN))
            if ch is not None and getattr(ch, "guild", None) is not None \
                    and ch.guild.id != guild_id:
                return  # autojoin belongs to another server
    except ValueError:
        return
    log.info("[guild:%s] autorejoin in progress", guild_id)
    await autojoin_vc()


intents = discord.Intents.default()
intents.message_content = True
intents.dm_messages = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


def session_key_for(message):
    if isinstance(message.channel, discord.DMChannel):
        return f"dm:{message.author.id}"
    return f"guild:{message.channel.id}"


def _is_dm(channel):
    return isinstance(channel, discord.DMChannel)


async def react(message, emoji):
    """Best-effort add reaction (missing perms just warn)."""
    if not emoji:
        return
    try:
        await message.add_reaction(emoji)
    except Exception as e:
        log.warning("react %r failed: %s", emoji, e)


async def swap_react(message, old, new):
    if old:
        try:
            await message.remove_reaction(old, client.user)
        except Exception as e:
            log.warning("unreact %r failed: %s", old, e)
    await react(message, new)


async def deliver_reply(key, channel, messages, reply, inbox, status):
    """Send one turn's reply + outbound files. Swaps working reacts."""
    # --- outbound attachments: [[attach:path]] -> discord.File ---
    reply, out_paths = split_attach_markers(reply)
    log.info("[%s] reply: %d chars, %d attach marker(s): %s",
             key, len(reply), len(out_paths), out_paths)
    reply, model_reacts = split_react_markers(reply)
    if model_reacts:
        log.info("[%s] model reacts: %s", key, model_reacts)
    reply, say_lines = split_say_markers(reply)
    if say_lines:
        log.info("[%s] model says: %s", key, say_lines)
    errors = []
    outbound = []
    for pstr in out_paths:
        try:
            p = resolve_outbound(pstr)
        except JailViolation as e:
            # Loud only for real paths (exfiltration attempt); nonexistent
            # markers stay silent-skipped (quoted doc examples must not spam).
            if e.resolved.exists():
                log.warning("[%s] outbound refused (outside file jail): "
                            "%r -> %s", key, e.raw, e.resolved)
                errors.append(jail_error_message(e.raw))
            else:
                log.info("[%s] outbound marker outside jail, no such file, "
                         "skipping: %r", key, pstr)
            continue
        log.info("[%s] outbound: %r -> %s", key, pstr, p)
        if not p.is_file():
            # Silent skip, not a user error: doc examples ([[attach:path]],
            # [[attach:FULL_PATH]]) echo back whenever the model quotes
            # instructions or source, and must never spam the channel.
            log.info("[%s] outbound marker has no such file, skipping: %s",
                     key, p)
            continue
        if p.stat().st_size > MAX_ATTACH_MB * 1024 * 1024:
            log.warning("[%s] outbound too big: %s %d bytes",
                        key, p, p.stat().st_size)
            errors.append(
                f"(could not attach {p.name}: exceeds {MAX_ATTACH_MB:g}MB limit)")
            continue
        outbound.append(p)
    if errors:
        reply = (reply + "\n" if reply else "") + "\n".join(errors)

    if status is not None:
        try:
            await status.delete()
        except Exception:
            pass

    # long replies go out as a .md file instead of a wall of chunks.
    # Voice reads the full text (up to VOICEBOX_MAX_CHARS), not the placeholder.
    voice_text = reply
    reply_file = None
    if REPLY_AS_FILE_LIMIT > 0 and len(reply) > REPLY_AS_FILE_LIMIT:
        try:
            fd, tmppath = tempfile.mkstemp(
                prefix="reply-", suffix=".md", dir=str(inbox))
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(reply)
            reply_file = tmppath
            log.info("[%s] reply %d chars -> file %s",
                     key, len(reply), tmppath)
            reply = f"reply too long ({len(reply)} chars), attached as file."
        except Exception:
            log.exception("[%s] reply-to-file failed, chunking instead", key)
            reply_file = None

    # --- voice pre-flight: start TTS while the text posts (overlap) ---
    # VC connected -> stream sentence chunks (say-lines first, first audio ASAP).
    # Otherwise -> one wav (say-lines folded in) attached as reply.wav.
    voice_task = None
    voice_chunks = None
    voice_stream = False
    voice_epoch = None
    say_chunks = [c for s in say_lines
                  for c in split_sentences(clean_for_tts(s))]
    if voice_enabled(key) and (voice_text.strip() or say_chunks):
        guild = getattr(channel, "guild", None)
        if guild is not None:
            vc = guild_voice_client(guild.id)
            voice_stream = vc is not None and vc.is_connected()
        if voice_stream:
            voice_chunks = say_chunks + split_sentences(
                clean_for_tts(voice_text))
            if voice_chunks:
                voice_epoch = VC_EPOCHS.get(guild.id, 0)
                VC_STREAMS[guild.id] = VC_STREAMS.get(guild.id, 0) + 1
                voice_task = asyncio.ensure_future(
                    asyncio.to_thread(tts_wav_clean, voice_chunks[0], key))
        else:
            file_text = ("\n".join(say_lines + [voice_text])
                         if say_lines else voice_text)
            voice_task = asyncio.ensure_future(
                asyncio.to_thread(tts_wav, file_text, key))

    try:
        first = True
        for part in split_smart(reply, MAX_DISCORD):
            # guilds: thread the first chunk under the user's message
            if first and messages and not _is_dm(channel):
                first = False
                try:
                    await messages[0].reply(part)
                    continue
                except Exception as e:
                    log.warning("[%s] reply-thread failed, sending plain: %s",
                                key, e)
            await channel.send(part)
        if reply_file:
            try:
                await channel.send(
                    file=discord.File(reply_file, filename="reply.md"))
            finally:
                try:
                    os.unlink(reply_file)
                except OSError:
                    pass
        for p in outbound:
            log.info("[%s] uploading %s (%d bytes)", key, p, p.stat().st_size)
            try:
                await channel.send(file=discord.File(str(p)))
                log.info("[%s] uploaded %s", key, p)
            except Exception as e:
                log.exception("[%s] upload failed: %s", key, p)
                await channel.send(f"(failed to attach {p.name}: {e})")
        # --- voice reply: spoken version of the same text via Voicebox ---
        # Text is always delivered above; TTS failure never blocks it.
        # Guild VC connected -> sentence chunks stream into the channel
        # (first audio ASAP, no file to click).
        # Otherwise (DMs, not joined) -> reply.wav attaches as before.
        if voice_task is not None:
            try:
                if voice_stream and voice_chunks:
                    guild = getattr(channel, "guild", None)
                    gid = guild.id if guild is not None else None
                    try:
                        first = await voice_task
                        rest = voice_chunks[1:]
                        if (gid is not None and voice_epoch is not None
                                and voice_epoch != VC_EPOCHS.get(gid, 0)):
                            log.info("[%s] VC stream skipped, "
                                     "abandoning %d chunk(s)", key,
                                     len(voice_chunks))
                            rest = []
                            first = None
                        if first and gid is not None:
                            if not await vc_say(gid, first, inbox,
                                                voice_epoch):
                                log.warning("[%s] VC gone or skipped mid-reply, "
                                            "dropping voice", key)
                                rest = []
                            else:
                                log.info("[%s] streaming voice: %d chunk(s), "
                                         "first %d bytes", key,
                                         len(voice_chunks), len(first))
                        for chunk in rest:
                            if (gid is not None and voice_epoch is not None
                                    and voice_epoch
                                    != VC_EPOCHS.get(gid, 0)):
                                log.info("[%s] VC stream skipped mid-turn, "
                                         "abandoning remaining chunks", key)
                                break
                            wav = await asyncio.to_thread(tts_wav_clean,
                                                          chunk, key)
                            if wav and gid is not None:
                                if not await vc_say(gid, wav, inbox,
                                                    voice_epoch):
                                    log.warning("[%s] VC gone or skipped mid-reply, "
                                                "dropping voice", key)
                                    break
                    finally:
                        if gid is not None:
                            VC_STREAMS[gid] = max(
                                0, VC_STREAMS.get(gid, 1) - 1)
                else:
                    wav = await voice_task
                    if wav:
                        if len(wav) > MAX_ATTACH_MB * 1024 * 1024:
                            log.warning("[%s] voice reply too big: %d bytes",
                                        key, len(wav))
                            wav = None
                    if wav:
                        log.info("[%s] uploading voice reply (%d bytes)",
                                 key, len(wav))
                        await channel.send(file=discord.File(
                            io.BytesIO(wav), filename="reply.wav"))
            except Exception:
                log.exception("[%s] voice reply send failed", key)
    except Exception:
        log.exception("[%s] reply send failed", key)
        if voice_task is not None and not voice_task.done():
            voice_task.cancel()
        if voice_stream and voice_epoch is not None:
            _g = getattr(channel, "guild", None)
            if _g is not None:
                VC_STREAMS[_g.id] = max(0, VC_STREAMS.get(_g.id, 1) - 1)
        for m in messages:
            await swap_react(m, REACT_START, REACT_ERROR)
        return
    for m in messages:
        for e in model_reacts:
            try:
                await m.add_reaction(e)
            except Exception as ex:
                log.warning("[%s] model react %r failed: %s", key, e, ex)
    for m in messages:
        await swap_react(m, REACT_START, REACT_DONE)


async def cancel_turn(key, channel):
    """Cancel the in-flight opencode run for key.

    Kills the running subprocess (if any), drops the queued follow-up
    buffer, and posts a confirmation. Never touches voice playback or
    other sessions. Returns True when something was stopped or dropped.
    """
    q = get_queue(key)
    with TURN_LOCK:
        st = TURN_STATE.get(key)
        proc = st["proc"] if st else None
        alive = proc is not None and proc.poll() is None
        if alive:
            st["cancelled"] = True
    # Global gate: a turn still waiting for a slot has no proc yet — wake
    # its waiter so it exits without consuming a slot or posting. (Its own
    # cleanup flips its messages' reacts; the confirmation post below is
    # the only message sent.)
    await cancel_waiting_turn(key)
    async with q.guard:
        pending = q.buffer
        q.buffer = []
        running = q.running
    for m, _, _ in pending:
        await swap_react(m, REACT_START, REACT_ERROR)
    if alive:
        try:
            proc.terminate()
        except Exception as e:
            log.warning("[%s] cancel terminate failed: %s", key, e)
        n = len(pending)
        extra = (f" (also dropped {n} queued message(s))" if n else "")
        log.info("[%s] turn cancelled by user%s", key, extra)
        await channel.send(
            f"cancelled the running turn for this chat.{extra}")
        return True
    if running or pending:
        # Turn already reached the reply/voice stage (or sits between
        # batches): no subprocess left, just drop the queued buffer.
        n = len(pending)
        if n:
            log.info("[%s] cancel during reply stage: dropped %d queued",
                     key, n)
            await channel.send(
                f"no in-flight opencode run (already replying) - "
                f"dropped {n} queued message(s).")
        else:
            await channel.send(
                "no in-flight opencode run (already replying) - "
                "nothing queued.")
        return True
    with TURN_LOCK:
        TURN_STATE.pop(key, None)  # stale dead-proc entry, if any
    await channel.send("nothing running for this chat.")
    return False


async def run_batches(key, channel, q, batch, inbox, status):
    """Run one turn, then single follow-up turns for coalesced arrivals."""
    global ACTIVE_TURNS
    async with ACTIVE_GUARD:
        ACTIVE_TURNS += 1
        first = ACTIVE_TURNS == 1
    if first:
        await update_presence()
    # Global gate: one slot for the whole call (every follow-up batch plus
    # its reply, voice TTS included). One acquire/release pair keeps the
    # audit to a single flag: the outer finally releases exactly once on
    # every post-acquire exit (normal return, TurnCancelled mid-run, or an
    # unexpected exception). A wait-cancel raises before slot_held flips —
    # or hands the slot straight on inside acquire — so it never leaks.
    # Holding through TTS can briefly idle a slot, accepted deliberately to
    # keep each session's text+voice delivery atomic and the code obvious.
    slot_held = False
    try:
        if MAX_CONCURRENT_TURNS > 0:
            try:
                await acquire_turn_slot(key)
            except TurnCancelled:
                log.info("[%s] turn cancelled while waiting for slot", key)
                with TURN_LOCK:
                    TURN_STATE.pop(key, None)
                async with q.guard:
                    rest = q.buffer
                    q.buffer = []
                own = [m for m, _, _ in batch]
                for m in own:
                    await swap_react(m, REACT_START, REACT_ERROR)
                for m, _, _ in rest:
                    await swap_react(m, REACT_START, REACT_ERROR)
                if status is not None:
                    try:
                        await status.delete()
                    except Exception:
                        pass
                return
            slot_held = True
        while True:
            msgs = [m for m, _, _ in batch]
            combined = "\n\n---\n\n".join(p for _, p, _ in batch)
            note = bridge_note_for(key, channel)
            combined = f"{combined}\n{note}" if combined else note
            files = [f for _, _, fs in batch for f in fs]
            log.info("[%s] turn: %d msg(s), %d chars + %d files, model=%s",
                     key, len(batch), len(combined), len(files),
                     MODEL_OVERRIDES.get(key) or OPENCODE_MODEL or "(default)")
            log.debug("[%s] full prompt:\n%s", key, combined[:3000])
            with TURN_LOCK:
                TURN_STATE[key] = {"proc": None, "cancelled": False}
            try:
                async with channel.typing():
                    reply, new_sid = await asyncio.to_thread(
                        run_opencode, combined, key, files,
                        MODEL_OVERRIDES.get(key))
            except TurnCancelled:
                log.info("[%s] turn cancelled, no reply posted", key)
                with TURN_LOCK:
                    TURN_STATE.pop(key, None)
                async with q.guard:
                    rest = q.buffer
                    q.buffer = []
                for m in msgs:
                    await swap_react(m, REACT_START, REACT_ERROR)
                for m, _, _ in rest:
                    await swap_react(m, REACT_START, REACT_ERROR)
                if status is not None:
                    try:
                        await status.delete()
                    except Exception:
                        pass
                return
            with TURN_LOCK:
                TURN_STATE.pop(key, None)
            if new_sid and new_sid != SESSIONS.get(key):
                SESSIONS[key] = new_sid
                remember_session(key, new_sid)
                save_state()
                log.info("[%s] session: %s", key, new_sid)
            await deliver_reply(key, channel, msgs, reply, inbox, status)
            status = None
            async with q.guard:
                if not q.buffer:
                    q.running = False
                    return
                batch = q.buffer
                q.buffer = []
                log.info("[%s] coalesced follow-up: %d msg(s)",
                         key, len(batch))
    finally:
        if slot_held:
            await release_turn_slot()
        async with q.guard:
            stuck = [m for m, _, _ in q.buffer]
            q.buffer = []
            q.running = False
        for m in stuck:
            await swap_react(m, REACT_START, REACT_ERROR)
        async with ACTIVE_GUARD:
            ACTIVE_TURNS -= 1
            last = ACTIVE_TURNS == 0
        if last:
            await update_presence()


def list_sessions(limit=25):
    """Live `opencode session list` for the bot project. None on failure."""
    try:
        proc = subprocess.run(
            [OPENCODE_BIN, "session", "list", "--format", "json",
             "-n", str(limit)],
            cwd=OPENCODE_DIR, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        log.warning("session list failed: %s", e)
        return None
    if proc.returncode != 0:
        log.warning("session list rc=%d: %s", proc.returncode,
                    (proc.stderr or "")[:500])
        return None
    try:
        data = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, list) else None


def build_session_options(entries, key, max_options=24):
    """Pure render: [(label, description, sid)] with origin + active star."""
    active = SESSIONS.get(key)
    known = set(KNOWN.get(key, []))
    opts = []
    for e in entries[:max_options]:
        if not isinstance(e, dict):
            continue
        sid = e.get("id", "")
        if not sid:
            continue
        star = "* " if sid == active else ""
        origin = "bot" if sid in known else "cli"
        try:
            dt = datetime.fromtimestamp(
                int(e.get("updated", 0) or 0) / 1000).strftime("%d/%m %H:%M")
        except (ValueError, TypeError, OSError):
            dt = "?"
        title = str(e.get("title") or "(untitled)")
        opts.append((f"{star}{title}"[:100], f"[{origin}] {dt}"[:100], sid))
    return opts


def key_for_interaction(interaction):
    ch = interaction.channel
    if ch is None or isinstance(ch, discord.DMChannel):
        return f"dm:{interaction.user.id}"
    return f"guild:{interaction.channel_id}"


MODELS_CACHE = {"at": 0.0, "items": []}
MODELS_TTL = 3600


def list_models():
    """Live `opencode models` (provider/model lines). None on failure."""
    try:
        proc = subprocess.run(
            [OPENCODE_BIN, "models"],
            cwd=OPENCODE_DIR, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        log.warning("model list failed: %s", e)
        return None
    if proc.returncode != 0:
        log.warning("model list rc=%d: %s", proc.returncode,
                    (proc.stderr or "")[:500])
        return None
    items = [ln.strip() for ln in (proc.stdout or "").splitlines()
             if "/" in ln.strip() and " " not in ln.strip()]
    items = [re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", it) for it in items]
    items = [it for it in items if "/" in it and " " not in it]
    if not items:
        log.warning("model list parsed 0 models (%d chars stdout)",
                    len(proc.stdout or ""))
        return None
    log.info("model list: %d model(s)", len(items))
    if len(items) < 10:
        log.warning("only %d models listed - provider credentials are "
                    "probably missing from this process's environment",
                    len(items))
    return items


def get_models():
    now = time.monotonic()
    if not MODELS_CACHE["items"] or now - MODELS_CACHE["at"] > MODELS_TTL:
        items = list_models()
        if items:
            MODELS_CACHE.update(at=now, items=items)
    return MODELS_CACHE["items"]


def resolve_model_arg(arg, items):
    """Returns (action, value): show/clear/set/error."""
    a = (arg or "").strip()
    if not a:
        return ("show", None)
    if a.lower() in ("clear", "default"):
        return ("clear", None)
    if items:
        if a not in items:
            return ("error", f"unknown model `{a}` - see `!model` list "
                             "or pick from `/model` autocomplete.")
        return ("set", a)
    if "/" not in a:
        return ("error", f"bad model `{a}` - use provider/model, e.g. "
                         "`google/gemini-2.5-flash`.")
    return ("set", a)


def current_model_s(key):
    m = MODEL_OVERRIDES.get(key)
    if m:
        return m
    return f"{OPENCODE_MODEL} (env)" if OPENCODE_MODEL else "opencode default"


async def model_autocomplete(interaction: discord.Interaction, current: str):
    items = await asyncio.to_thread(get_models)
    cur = (current or "").lower()
    out = []
    if not cur or "default".startswith(cur):
        out.append(app_commands.Choice(
            name="Default (env / opencode default)", value="__clear__"))
    for it in items:
        if cur in it.lower():
            prov, name = it.split("/", 1)
            out.append(app_commands.Choice(
                name=f"{name} ({prov})"[:100], value=it))
        if len(out) >= 25:
            break
    return out


@tree.command(name="model",
              description="View or switch the opencode model for this chat")
@app_commands.describe(model="provider/model, or Default to clear")
@app_commands.autocomplete(model=model_autocomplete)
async def model_cmd(interaction: discord.Interaction, model: str):
    if ALLOWED and str(interaction.user.id) not in ALLOWED:
        await interaction.response.send_message(
            "not allowed.", ephemeral=True)
        return
    key = key_for_interaction(interaction)
    if not model or model.strip() == "__clear__":
        MODEL_OVERRIDES.pop(key, None)
        save_state()
        log.info("[%s] model override cleared", key)
        await interaction.response.send_message(
            "model cleared - back to default.", ephemeral=True)
        return
    items = await asyncio.to_thread(get_models)
    if items and model not in items:
        await interaction.response.send_message(
            "unknown model - pick one from the autocomplete list.",
            ephemeral=True)
        return
    MODEL_OVERRIDES[key] = model
    save_state()
    log.info("[%s] model override: %s", key, model)
    await interaction.response.send_message(
        f"model for this chat: `{model}`.", ephemeral=True)


class SessionPicker(discord.ui.View):
    def __init__(self, key, options, titles, timeout=120):
        super().__init__(timeout=timeout)
        self.key = key
        self.titles = titles
        self.sel = discord.ui.Select(
            placeholder="Pick an opencode session…",
            min_values=1, max_values=1)
        for label, desc, sid in options:
            self.sel.add_option(label=label, description=desc, value=sid)
        self.sel.add_option(label="+ New session",
                            description="Start fresh on next message",
                            value="__new__")
        self.sel.callback = self.picked
        self.add_item(self.sel)

    async def picked(self, interaction: discord.Interaction):
        sid = self.sel.values[0]
        if sid == "__new__":
            SESSIONS.pop(self.key, None)
            USAGE.pop(self.key, None)
            save_state()
            log.info("[%s] session reset via picker", self.key)
            await interaction.response.send_message(
                "fresh opencode session started - next message starts it.",
                ephemeral=True)
            return
        SESSIONS[self.key] = sid
        remember_session(self.key, sid)
        save_state()
        log.info("[%s] session switched via picker: %s", self.key, sid)
        await interaction.response.send_message(
            f"switched to `{self.titles.get(sid, sid)}`.",
            ephemeral=True)


@tree.command(name="sessions",
                     description="Browse and switch opencode sessions")
async def sessions_cmd(interaction: discord.Interaction):
    if ALLOWED and str(interaction.user.id) not in ALLOWED:
        await interaction.response.send_message(
            "not allowed.", ephemeral=True)
        return
    key = key_for_interaction(interaction)
    await interaction.response.defer(ephemeral=True)
    entries = await asyncio.to_thread(list_sessions)
    if entries is None:
        known = list(KNOWN.get(key, []))
        if SESSIONS.get(key) and SESSIONS[key] not in known:
            known = [SESSIONS[key]] + known
        if not known:
            await interaction.followup.send(
                "opencode session list unreachable and nothing remembered.",
                ephemeral=True)
            return
        entries = [{"id": s, "title": s, "updated": 0} for s in known[:24]]
    options = build_session_options(entries, key)
    if not options:
        await interaction.followup.send(
            "no sessions found.", ephemeral=True)
        return
    titles = {sid: label.lstrip("* ") for label, _, sid in options}
    await interaction.followup.send(
        "Pick an opencode session for this chat "
        "(`*` = current, `+ New session` = fresh):",
        view=SessionPicker(key, options, titles), ephemeral=True)


@client.event
async def on_ready():
    log.info("logged in as %s (id %s)", client.user, client.user.id)
    await update_presence()
    global TREE_SYNCED, SAY_STARTED
    if not SAY_STARTED and SAY_DIR_ENABLED:
        SAY_STARTED = True
        SAY_DIR.mkdir(parents=True, exist_ok=True)
        asyncio.ensure_future(say_watcher())
        log.info("say-queue watching %s", SAY_DIR)
    await autojoin_vc()
    if VOICEBOX_WARMUP:
        asyncio.ensure_future(warmup_voice())
    if not TREE_SYNCED:
        TREE_SYNCED = True
        for g in client.guilds:
            try:
                tree.copy_global_to(guild=g)
                n = await tree.sync(guild=g)
                log.info("slash synced to %s: %d cmd(s)", g.name, len(n))
            except Exception as e:
                log.warning("guild sync failed (%s): %s", g.name, e)
        try:
            await tree.sync()
            log.info("slash synced globally (DMs can take up to an hour)")
        except Exception as e:
            log.warning("global sync failed: %s", e)


@client.event
async def on_voice_state_update(member, before, after):
    # Bot was disconnected / moved out: drop queued clips for that guild.
    if member.id == client.user.id:
        guild = getattr(after.channel or before.channel, "guild", None)
        if after.channel is None and guild is not None:
            vc_drop(guild.id)
            log.info("[guild:%s] VC disconnected, queue cleared", guild.id)
            if guild.id in VC_EXPECTED_BYE:
                VC_EXPECTED_BYE.discard(guild.id)
            elif VC_AUTOREJOIN and VC_AUTOJOIN:
                asyncio.ensure_future(autorejoin_vc(guild.id))
        if guild is not None:
            _refresh_autoleave(guild.id)
        return
    # another member moved: re-evaluate solo-idle state where the bot sits
    guilds = set()
    for ch in (before.channel, after.channel):
        g = getattr(ch, "guild", None)
        if g is not None:
            guilds.add(g.id)
    for gid in guilds:
        _refresh_autoleave(gid)


@client.event
async def on_message(message):
    if message.author.bot:
        return
    if ALLOWED and str(message.author.id) not in ALLOWED:
        return

    is_dm = isinstance(message.channel, discord.DMChannel)
    content = message.content.strip()

    if not is_dm:
        mentioned = client.user in message.mentions
        prefixed = content.startswith(GUILD_PREFIX)
        if not (mentioned or prefixed):
            return
        if prefixed:
            content = content[len(GUILD_PREFIX):].strip()
        # strip a leading mention
        content = re.sub(rf"^<@!?{client.user.id}>\s*", "", content).strip()

    if not content and not message.attachments:
        return

    if content.lower() in ("!help", "help"):
        extra = (f"\n(psst: you attached {len(message.attachments)} file(s) - "
                 "resend them with your actual message.)"
                 if message.attachments else "")
        await message.channel.send(
            "DM me anything and I'll run it through opencode.\n"
            "`!new` - fresh session (plain message, not /new)\n"
            "`!status` - session, usage, inbox, queue\n"
            "`!voice [on|off]` - spoken replies via Voicebox (Computer voice)\n"
            "`!voiceprofile [name-or-id|clear]` - per-chat voice profile\n"
            "`!join` / `!leave` - speak replies in your voice channel (servers)\n"
            "`!say <text>` - speak a line in VC now, no opencode call\n"
            "`!skip` - stop the current VC clip and drop the queue "
            "(stays connected)\n"
            "`!voiceready` - check voicebox + discord + VC + you-in-VC\n"
            "`!cancel` - stop the running turn (kills this run; send again "
            "if a follow-up started)\n"
            f"`{GUILD_PREFIX} <prompt>` - use me in a server channel\n"
            "Attach + `place this in <dir>` saves files directly.\n"
            "`send me <path>` sends a file back directly.\n"
            "Rapid messages merge into one follow-up - wait for the check.\n"
            "`/sessions` - browse and switch opencode sessions (servers).\n"
            "`/model` - switch the model for this chat."
            + extra)
        return

    key = session_key_for(message)
    if content.lower() == "!cancel":
        await cancel_turn(key, message.channel)
        return

    if content.lower() == "!new":
        SESSIONS.pop(key, None)
        USAGE.pop(key, None)
        save_state()
        extra = (f" ({len(message.attachments)} attached file(s) ignored - "
                 "resend them now.)" if message.attachments else "")
        await message.channel.send("fresh opencode session started." + extra)
        return

    if content.lower() == "!status":
        sid = SESSIONS.get(key, "(none)")
        u = USAGE.get(key, {"in": 0, "out": 0, "cost": 0.0, "turns": 0})
        try:
            inbox = ATTACH_DIR / safe_key(key)
            nfiles = nbytes = 0
            for p in inbox.iterdir():
                if p.is_file():
                    nfiles += 1
                    nbytes += p.stat().st_size
            inbox_s = f"{nfiles} file(s), {nbytes / 1024 / 1024:.1f} MB"
        except OSError:
            inbox_s = "n/a"
        q = get_queue(key)
        queue_s = (f"running + {len(q.buffer)} pending" if q.running
                   else "idle")
        if MAX_CONCURRENT_TURNS > 0:
            async with TURN_GATE_GUARD:
                _run_n = TURN_SLOTS_HELD
                _wait_n = len(TURN_WAITERS)
            gate_s = f" | global: {_run_n} running, {_wait_n} waiting"
        else:
            async with ACTIVE_GUARD:
                _active_n = ACTIVE_TURNS
            gate_s = f" | global: {_active_n} running (unlimited)"
        model = MODEL_OVERRIDES.get(key)
        model_s = model if model else (
            f"{OPENCODE_MODEL} (env)" if OPENCODE_MODEL else "opencode default")
        voice_s = (f"on ({effective_voice_profile(key)})" if voice_enabled(key)
                   else "off")
        guild = getattr(message, "guild", None)
        gvc = guild.voice_client if guild is not None else None
        if gvc is not None and gvc.is_connected():
            pending = (VC_QUEUES.get(guild.id).qsize()
                       if VC_QUEUES.get(guild.id) else 0)
            auto = ""
            deadline = VC_AUTOLEAVE_AT.get(guild.id)
            if deadline is not None:
                rem = deadline - time.monotonic()
                if rem > 0:
                    auto = (f", auto-leave in {int(rem // 60)}m"
                            f"{int(rem % 60):02d}s")
            vc_s = (f"in #{gvc.channel.name} "
                    f"({'speaking' if gvc.is_playing() else 'idle'}"
                    f"{f', {pending} queued' if pending else ''}{auto})"
                    if gvc.channel else "connected")
        else:
            vc_s = "not connected (`!join` in a server)"
        try:
            pending_say = sum(
                1 for p in SAY_DIR.iterdir()
                if p.is_file() and p.suffix.lower() == ".txt"
                and not p.name.startswith(".")) if SAY_DIR_ENABLED else 0
        except OSError:
            pending_say = 0
        await message.channel.send(
            f"session `{sid}`\n"
            f"model: `{model_s}`\n"
            f"voice: `{voice_s}`\n"
            f"vc: `{vc_s}`\n"
            f"say-queue: `{pending_say} pending`\n"
            f"turns: {u['turns']} | tokens: {u['in']} in / {u['out']} out | "
            f"cost: ${u['cost']:.4f}\n"
            f"inbox: {inbox_s}\n"
            f"queue: {queue_s}{gate_s}")
        return

    if content.lower() == "!voice" or content.lower().startswith("!voice "):
        arg = content[6:].strip().lower()
        if arg in ("on", "off"):
            VOICE_OVERRIDES[key] = (arg == "on")
            save_state()
            log.info("[%s] voice override: %s", key, arg)
            await message.channel.send(
                f"voice replies {arg} for this chat.")
        else:
            state = "on" if voice_enabled(key) else "off"
            await message.channel.send(
                f"voice replies: `{state}` "
                f"(profile `{effective_voice_profile(key)}`). "
                "`!voice on` / `!voice off` to change.")
        return

    if content.lower() == "!voiceprofile" or content.lower().startswith(
            "!voiceprofile "):
        arg = content[len("!voiceprofile"):].strip()
        if not arg:
            cur = VOICE_PROFILE_OVERRIDES.get(key)
            await message.channel.send(
                "voice profile for this chat: "
                f"`{cur or VOICEBOX_PROFILE + ' (global)'}`. "
                "`!voiceprofile <name-or-id>` to change, "
                "`!voiceprofile clear` to reset.")
            return
        if arg.lower() == "clear":
            VOICE_PROFILE_OVERRIDES.pop(key, None)
            save_state()
            log.info("[%s] voice profile override cleared", key)
            await message.channel.send(
                "voice profile cleared - back to global "
                f"`{VOICEBOX_PROFILE}`.")
            return
        pid = await asyncio.to_thread(_resolve_profile_name, arg)
        if pid is None:
            items = await asyncio.to_thread(list_voicebox_profiles)
            names = ", ".join(f"`{n}`" for _, n in items if n)
            await message.channel.send(
                f"unknown voice profile `{arg}` - available: "
                f"{names or '(none found)'}.")
            return
        VOICE_PROFILE_OVERRIDES[key] = arg
        save_state()
        log.info("[%s] voice profile override: %s", key, arg)
        await message.channel.send(
            f"voice profile for this chat: `{arg}`.")
        return

    if content.lower() == "!join":
        guild = getattr(message, "guild", None)
        if guild is None:
            await message.channel.send(
                "voice channels only exist in servers - `!join` works in a "
                "server channel, not DMs.")
            return
        vs = getattr(message.author, "voice", None)
        target = vs.channel if vs else None
        if target is None:
            await message.channel.send(
                "join a voice channel first, then `!join` to pull me in.")
            return
        if not ensure_opus():
            await message.channel.send(
                "voice audio library (Opus) isn't loaded - check the bot log "
                "for the opus path it tried.")
            return
        try:
            vc = guild.voice_client
            if vc is not None and vc.is_connected():
                if vc.channel is not None and vc.channel.id == target.id:
                    await message.channel.send(
                        f"already in `{target.name}` - replies are spoken there.")
                else:
                    await vc.move_to(target)
                    log.info("[%s] VC moved to %s", key, target.name)
                    await message.channel.send(
                        f"moved to `{target.name}` - replies are spoken there.")
            else:
                await target.connect()
                log.info("[%s] VC joined %s", key, target.name)
                await message.channel.send(
                    f"joined `{target.name}` - replies are spoken there, "
                    "no file to click.")
        except Exception as e:
            log.exception("[%s] VC join failed", key)
            await message.channel.send(f"could not join `{target.name}`: {e}")
        _refresh_autoleave(guild.id)
        return

    if content.lower() in ("!leave", "!disconnect", "!stop"):
        guild = getattr(message, "guild", None)
        vc = guild.voice_client if guild is not None else None
        if vc is None or not vc.is_connected():
            await message.channel.send("not in a voice channel.")
            return
        try:
            name = vc.channel.name if vc.channel else "voice"
            VC_EXPECTED_BYE.add(guild.id)
            vc_drop(guild.id)
            try:
                vc.stop()
            except Exception:
                pass
            await vc.disconnect()
            log.info("[%s] VC left %s", key, name)
            await message.channel.send(
                f"left `{name}` - back to reply.wav files.")
        except Exception as e:
            log.exception("[%s] VC leave failed", key)
            await message.channel.send(f"could not leave: {e}")
        return

    if content.lower() == "!skip":
        guild = getattr(message, "guild", None)
        vc = guild.voice_client if guild is not None else None
        if vc is None or not vc.is_connected():
            await message.channel.send("not in a voice channel.")
            return
        gid = guild.id
        VC_EPOCHS[gid] = VC_EPOCHS.get(gid, 0) + 1
        try:
            was_playing = bool(vc.is_playing())
        except Exception:
            was_playing = False
        q = VC_QUEUES.get(gid)
        pending = q.qsize() if q is not None else 0
        streaming = VC_STREAMS.get(gid, 0) > 0
        try:
            vc.stop()
        except Exception:
            pass
        vc_drop(gid)
        log.info("[%s] VC skip: stopped=%s dropped=%d", key,
                 was_playing, pending)
        if was_playing or pending or streaming:
            extra = f" ({pending} queued dropped)" if pending else ""
            await message.channel.send(f"skipped.{extra}")
        else:
            await message.channel.send("nothing playing.")
        return

    if content.lower() == "!say" or content.lower().startswith("!say "):
        line = content[4:].strip()
        if not line:
            await message.channel.send(
                "usage: `!say <text>` - speaks in every connected voice "
                "channel, no opencode call.")
            return
        live = connected_guild_ids()
        if not live:
            await message.channel.send(
                "not in any voice channel - `!join` first.")
            return
        async with message.channel.typing():
            wav = await asyncio.to_thread(tts_wav, line, key)
        if not wav:
            await message.channel.send("TTS failed - check the bot log.")
            return
        VC_TMP.mkdir(parents=True, exist_ok=True)
        n = 0
        for gid in live:
            if await vc_say(gid, wav, VC_TMP):
                n += 1
        log.info("[%s] !say queued for %d guild(s): %r", key, n, line[:80])
        await message.channel.send(
            f"saying it in {n} voice channel(s).")
        return

    if content.lower() == "!voiceready":
        results = await voice_ready()
        lines = []
        for label, ok, detail in results:
            lines.append(f"{'OK ' if ok else 'FAIL'} `{label}`: {detail}")
        allok = all(ok for _, ok, _ in results)
        lines.append("voice ready." if allok else
                     "NOT voice ready - fix the FAIL lines above.")
        log.info("[%s] voiceready: %s",
                 key, "; ".join(f"{l}={o}" for l, o, _ in results))
        await message.channel.send("\n".join(lines))
        return

    if content.lower() == "!model" or content.lower().startswith("!model "):
        arg = content[6:].strip()
        items = await asyncio.to_thread(get_models)
        action, value = resolve_model_arg(arg, items)
        if action == "show":
            lines = [f"current: `{current_model_s(key)}`"]
            if items:
                lines += [f"- `{m}`" for m in items]
            else:
                lines.append("(model list unavailable)")
            for part in split_smart("\n".join(lines), MAX_DISCORD):
                await message.channel.send(part)
        elif action == "clear":
            MODEL_OVERRIDES.pop(key, None)
            save_state()
            await message.channel.send("model cleared - back to default.")
        elif action == "set":
            MODEL_OVERRIDES[key] = value
            save_state()
            log.info("[%s] model override via !model: %s", key, value)
            await message.channel.send(f"model for this chat: `{value}`.")
        else:
            await message.channel.send(value)
        return

    log.info("[%s] msg from %s (%d attach, %d chars): %r",
             key, message.author, len(message.attachments), len(content),
             content[:200])
    await react(message, REACT_START)

    # --- direct send: "send me <path>" uploads without opencode ---
    send = SEND_RE.match(content) if not message.attachments else None
    if send:
        try:
            p = resolve_outbound(send.group(1))
        except JailViolation as e:
            # Loud only when the path exists (real read); nonexistent
            # outside-jail text falls through to opencode as normal chat.
            if e.resolved.exists():
                log.warning("[%s] direct send refused (outside file jail): "
                            "%r -> %s", key, e.raw, e.resolved)
                await swap_react(message, REACT_START, REACT_ERROR)
                await message.channel.send(jail_error_message(e.raw))
                return
            log.info("[%s] send pattern outside jail, no such file, "
                     "using opencode: %r", key, send.group(1))
        else:
            log.info("[%s] direct send: %r -> %s", key, send.group(1), p)
            if p.is_file():
                if p.stat().st_size > MAX_ATTACH_MB * 1024 * 1024:
                    await swap_react(message, REACT_START, REACT_ERROR)
                    await message.channel.send(
                        f"could not send `{p.name}`: exceeds {MAX_ATTACH_MB:g}MB limit")
                    return
                async with message.channel.typing():
                    await message.channel.send(file=discord.File(str(p)))
                log.info("[%s] direct-sent %s (%d bytes)", key, p, p.stat().st_size)
                await swap_react(message, REACT_START, REACT_DONE)
                return
            # not a file path: fall through to opencode for normal chat
            # containing the word "send" (e.g. "send me the report summary")
            log.info("[%s] send pattern but not a file, using opencode: %s", key, p)

    status = None
    if message.attachments:
        status = await message.channel.send(
            f"got {len(message.attachments)} attachment(s), saving...")

    async with message.channel.typing():
        # --- inbound attachments: save + pass via --file ---
        inbox = ATTACH_DIR / safe_key(key)
        inbox.mkdir(parents=True, exist_ok=True)
        local_files = []
        saved_files = []
        attach_notes = []
        stt_pending = []  # [(saved_path, filename, size, content_type)]
        for att in message.attachments:
            size_mb = (att.size or 0) / (1024 * 1024)
            log.info("[%s] inbound: %s (%d bytes, %s)",
                     key, att.filename, att.size, att.content_type)
            if size_mb > MAX_ATTACH_MB:
                log.warning("[%s] inbound skipped (too big): %s %.1fMB",
                            key, att.filename, size_mb)
                attach_notes.append(
                    f"[Attachment skipped: {att.filename} ({size_mb:.1f}MB "
                    f"exceeds {MAX_ATTACH_MB:g}MB limit)]")
                continue
            orig_stem = Path(att.filename).stem
            orig_suffix = Path(att.filename).suffix
            dest = inbox / att.filename
            # avoid overwriting: file(1).ext (keep original stem)
            i = 1
            while dest.exists():
                dest = inbox / f"{orig_stem}({i}){orig_suffix}"
                i += 1
            try:
                log.info("[%s] saving -> %s", key, dest.resolve())
                await att.save(dest)
                log.info("[%s] saved %d bytes -> %s",
                         key, dest.stat().st_size, dest.resolve())
                saved_files.append(str(dest.resolve()))
                if (VOICEBOX_TRANSCRIBE
                        and is_audio_attachment(att.filename,
                                                att.content_type)):
                    # Transcribed after the drop check; the transcript (not
                    # the raw bytes) is what opencode sees.
                    stt_pending.append(
                        (str(dest.resolve()), att.filename, att.size,
                         att.content_type))
                elif use_file_flag(att.filename, att.content_type):
                    local_files.append(str(dest.resolve()))
                    attach_notes.append(
                        f"[Attached file: {att.filename} ({att.size} bytes, "
                        f"{att.content_type or 'unknown type'}) saved at {dest.resolve()}]")
                else:
                    log.info("[%s] path-only (video, no --file): %s",
                             key, dest.resolve())
                    attach_notes.append(
                        f"[Attached video: {att.filename} ({att.size} bytes) "
                        f"saved at {dest.resolve()} - NOT inlined, "
                        f"use shell/file tools on this path]")
            except Exception as e:
                log.exception("[%s] save failed: %s", key, att.filename)
                attach_notes.append(
                    f"[Attachment failed: {att.filename}: {e}]")

        prune_inbox(inbox, key)

        if status is not None:
            try:
                await status.edit(
                    content=f"saved {len(saved_files)}/{len(message.attachments)} "
                            "file(s), asking opencode...")
            except Exception:
                pass

        # --- direct drop: "place this in <dir>" moves files, no opencode ---
        drop = DROP_RE.match(content) if saved_files else None
        if drop:
            try:
                target = resolve_target_dir(drop.group(1))
            except JailViolation as e:
                log.warning("[%s] drop refused (outside file jail): %r -> %s",
                            key, e.raw, e.resolved)
                if status is not None:
                    try:
                        await status.delete()
                    except Exception:
                        pass
                await swap_react(message, REACT_START, REACT_ERROR)
                await message.channel.send(jail_error_message(e.raw))
                return
            log.info("[%s] direct drop: %d file(s) -> %s", key, len(saved_files), target)
            try:
                target.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                log.exception("[%s] drop mkdir failed: %s", key, target)
                if status is not None:
                    try:
                        await status.delete()
                    except Exception:
                        pass
                await swap_react(message, REACT_START, REACT_ERROR)
                await message.channel.send(f"could not create {target}: {e}")
                return
            moved, errs = [], []
            for src in saved_files:
                try:
                    dst = target / Path(src).name
                    j = 1
                    stem, suf = dst.stem, dst.suffix
                    while dst.exists():
                        dst = target / f"{stem}({j}){suf}"
                        j += 1
                    shutil.move(src, dst)
                    moved.append(dst)
                    log.info("[%s] moved %s -> %s", key, src, dst)
                except Exception as e:
                    log.exception("[%s] move failed: %s", key, src)
                    errs.append(f"{Path(src).name}: {e}")
            if status is not None:
                try:
                    await status.delete()
                except Exception:
                    pass
            lines = [f"moved {len(moved)} file(s) to `{target}`:"]
            lines += [f"- `{p.name}`" for p in moved]
            lines += [f"(failed: {e})" for e in errs]
            await message.channel.send("\n".join(lines))
            await swap_react(message, REACT_START,
                             REACT_DONE if moved and not errs else REACT_ERROR)
            return

        prompt = content
        if stt_pending:
            if status is not None:
                try:
                    await status.edit(
                        content=f"transcribing {len(stt_pending)} voice "
                                "message(s)...")
                except Exception:
                    pass
            for saved_path, fname, fsize, fctype in stt_pending:
                text, duration = await asyncio.to_thread(
                    transcribe_audio_file, saved_path)
                if text:
                    attach_notes.append(
                        f"[Voice message {fname} ({duration:.1f}s): {text}]")
                else:
                    # STT failed/down: fall back to the old raw-audio path.
                    log.warning("[%s] STT fallback to --file: %s",
                                key, fname)
                    if use_file_flag(fname, fctype):
                        local_files.append(saved_path)
                    attach_notes.append(
                        f"[Attached file: {fname} ({fsize} bytes, "
                        f"{fctype or 'unknown type'}) saved at {saved_path} "
                        f"(transcription unavailable)]")
            if status is not None:
                try:
                    await status.edit(
                        content=f"saved {len(saved_files)}/{len(message.attachments)} "
                                "file(s), asking opencode...")
                except Exception:
                    pass
        if attach_notes:
            prompt = (prompt + "\n" if prompt else "") + "\n".join(attach_notes)

        log.info("[%s] opencode prompt: %d chars + %d files (--file=%s)",
                 key, len(prompt), len(local_files), local_files)
        log.debug("[%s] prompt attach notes:\n%s", key, "\n".join(attach_notes) or "(none)")
        q = get_queue(key)
        async with q.guard:
            if q.running:
                q.buffer.append((message, prompt, local_files))
                log.info("[%s] coalesced into running turn (%d pending)",
                         key, len(q.buffer))
                return
            q.running = True
        await run_batches(key, message.channel, q,
                          [(message, prompt, local_files)], inbox, status)


if __name__ == "__main__":
    if _TOKEN_ERROR:
        sys.exit(_TOKEN_ERROR)
    ensure_opus()
    client.run(TOKEN)
