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
    OPENCODE_MODEL      optional, passed as -m
    OPENCODE_AGENT      optional, passed as --agent
    OPENCODE_AUTO       "1" to pass --auto (auto-approve tools). Default "0".
                        Only enable for users you trust; it lets the agent
                        run shell commands and edit files unattended.
    OPENCODE_TIMEOUT    seconds per run (default 600)
    STATE_FILE          default ./state/sessions.json
    GUILD_PREFIX        default "!oc"
    ATTACH_DIR          default ./attachments (inbound inbox)
    MAX_ATTACH_MB       default 25 (in/out file size cap)
    ATTACH_KEEP_FILES   default 50 (inbox prune: newest N kept, 0 = unlimited)
    ATTACH_KEEP_MB      default 500 (inbox prune: total MB cap, 0 = unlimited)
    LOG_LEVEL           default INFO (e.g. DEBUG for verbose)
    REPLY_AS_FILE_LIMIT default 4000 (longer replies sent as reply.md; 0 disables)
    REACT_START/DONE/ERROR  default hourglass/check/cross (empty disables)

Required Discord privileged intent: Message Content (toggle in the
Developer Portal -> Bot -> Privileged Gateway Intents).
"""

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
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
OPENCODE_MODEL = os.environ.get("OPENCODE_MODEL", "").strip()
OPENCODE_AGENT = os.environ.get("OPENCODE_AGENT", "").strip()
OPENCODE_AUTO = os.environ.get("OPENCODE_AUTO", "0") == "1"
OPENCODE_TIMEOUT = _env_int("OPENCODE_TIMEOUT", 600)
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

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("discord-bot")

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

def resolve_target_dir(path_str):
    p = Path(path_str.strip().strip("'\"")).expanduser()
    if not p.is_absolute():
        p = (Path(OPENCODE_DIR) / p).resolve()
    return p


# Marker the model emits to send a file back: [[attach:D:\files\clip.mp4]]
ATTACH_RE = re.compile(r"\[\[attach:(.+?)\]\]", re.IGNORECASE)

# Marker the model emits to react to the user's message: [[react:EMOJI]]
# Literal unicode emoji (or custom <:name:id>); shortcodes won't resolve.
REACT_RE = re.compile(r"\[\[react:(.+?)\]\]", re.IGNORECASE)
MAX_MODEL_REACTS = 5

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
    "To send a file back to the user, put [[attach:FULL_PATH]] on its own "
    "line, e.g. [[attach:D:\\files\\clip.mp4]]. Use absolute paths.]"
)

if not TOKEN:
    _TOKEN_ERROR = "DISCORD_BOT_TOKEN is not set (env or .env file)"
else:
    _TOKEN_ERROR = None
if not ALLOWED:
    print("WARNING: ALLOWED_USER_IDS empty - any Discord user can DM the bot.",
          file=sys.stderr)


def load_state():
    """State shape: {"active": {...}, "known": {...}, "models": {...}}."""
    try:
        raw = json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {"active": {}, "known": {}, "models": {}}
    if isinstance(raw, dict) and isinstance(raw.get("active"), dict):
        known = raw.get("known")
        models = raw.get("models")
        return {"active": raw["active"],
                "known": known if isinstance(known, dict) else {},
                "models": models if isinstance(models, dict) else {}}
    # legacy flat {key: sid}
    act = ({k: v for k, v in raw.items() if isinstance(v, str)}
           if isinstance(raw, dict) else {})
    return {"active": act,
            "known": {k: [v] for k, v in act.items()},
            "models": {}}


def save_state():
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(
        {"active": SESSIONS, "known": KNOWN, "models": MODEL_OVERRIDES},
        indent=2))


_STATE = load_state()
SESSIONS = _STATE["active"]  # key -> opencode session id
KNOWN = _STATE["known"]  # key -> [opencode session ids the bot has used]
MODEL_OVERRIDES = _STATE["models"]  # key -> "provider/model"


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


ACTIVE_TURNS = 0
ACTIVE_GUARD = asyncio.Lock()
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
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            cmd, cwd=OPENCODE_DIR, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=OPENCODE_TIMEOUT)
    except subprocess.TimeoutExpired:
        log.warning("[%s] opencode timed out after %ds", session_key, OPENCODE_TIMEOUT)
        return (f"opencode timed out after {OPENCODE_TIMEOUT}s. "
                "Your session is kept; try a smaller task or `!new`."), session_id
    except FileNotFoundError:
        log.error("[%s] could not execute %r", session_key, OPENCODE_BIN)
        return (f"could not execute `{OPENCODE_BIN}` - is opencode installed "
                "and on PATH?"), session_id

    out = proc.stdout or ""
    dt = time.monotonic() - t0
    log.info("[%s] opencode done in %.1fs: rc=%d stdout=%d chars stderr=%d chars",
             session_key, dt, proc.returncode, len(out),
             len(proc.stderr or ""))
    log.debug("[%s] stdout:\n%s", session_key, out)
    if proc.stderr:
        log.debug("[%s] stderr:\n%s", session_key, proc.stderr)
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
    if proc.returncode != 0 and not text:
        err = (proc.stderr or "").strip()[-1500:]
        text = f"opencode exited with code {proc.returncode}."
        if err:
            text += f"\n```\n{err}\n```"
    if not text:
        text = "(opencode returned no text)"
    return text, new_sid


def safe_key(key):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", key)


def prune_inbox(inbox, key="?"):
    """Drop oldest inbox files beyond count/byte caps (reply leftovers too)."""
    try:
        files = sorted(
            (p for p in inbox.iterdir() if p.is_file()),
            key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    pruned = 0
    if ATTACH_KEEP_FILES > 0:
        while len(files) > ATTACH_KEEP_FILES:
            old = files.pop(0)
            try:
                old.unlink()
                pruned += 1
            except OSError:
                pass
    if ATTACH_KEEP_MB > 0:
        try:
            total = sum(p.stat().st_size for p in files)
        except OSError:
            total = 0
        while files and total > ATTACH_KEEP_MB * 1024 * 1024:
            old = files.pop(0)
            try:
                total -= old.stat().st_size
                old.unlink()
                pruned += 1
            except OSError:
                pass
    if pruned:
        log.info("[%s] pruned %d old inbox file(s)", key, pruned)


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


def resolve_outbound(path_str):
    p = Path(path_str.strip().strip("'\"")).expanduser()
    if not p.is_absolute():
        p = (Path(OPENCODE_DIR) / p).resolve()
    return p


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
    errors = []
    outbound = []
    for pstr in out_paths:
        p = resolve_outbound(pstr)
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

    # long replies go out as a .md file instead of a wall of chunks
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
    except Exception:
        log.exception("[%s] reply send failed", key)
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


async def run_batches(key, channel, q, batch, inbox, status):
    """Run one turn, then single follow-up turns for coalesced arrivals."""
    global ACTIVE_TURNS
    async with ACTIVE_GUARD:
        ACTIVE_TURNS += 1
        first = ACTIVE_TURNS == 1
    if first:
        await update_presence()
    try:
        while True:
            msgs = [m for m, _, _ in batch]
            combined = "\n\n---\n\n".join(p for _, p, _ in batch)
            combined = f"{combined}\n{BRIDGE_NOTE}" if combined else BRIDGE_NOTE
            files = [f for _, _, fs in batch for f in fs]
            log.info("[%s] turn: %d msg(s), %d chars + %d files, model=%s",
                     key, len(batch), len(combined), len(files),
                     MODEL_OVERRIDES.get(key) or OPENCODE_MODEL or "(default)")
            log.debug("[%s] full prompt:\n%s", key, combined[:3000])
            async with channel.typing():
                reply, new_sid = await asyncio.to_thread(
                    run_opencode, combined, key, files,
                    MODEL_OVERRIDES.get(key))
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
    global TREE_SYNCED
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
            f"`{GUILD_PREFIX} <prompt>` - use me in a server channel\n"
            "Attach + `place this in <dir>` saves files directly.\n"
            "`send me <path>` sends a file back directly.\n"
            "Rapid messages merge into one follow-up - wait for the check.\n"
            "`/sessions` - browse and switch opencode sessions (servers).\n"
            "`/model` - switch the model for this chat."
            + extra)
        return

    key = session_key_for(message)
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
        model = MODEL_OVERRIDES.get(key)
        model_s = model if model else (
            f"{OPENCODE_MODEL} (env)" if OPENCODE_MODEL else "opencode default")
        await message.channel.send(
            f"session `{sid}`\n"
            f"model: `{model_s}`\n"
            f"turns: {u['turns']} | tokens: {u['in']} in / {u['out']} out | "
            f"cost: ${u['cost']:.4f}\n"
            f"inbox: {inbox_s}\n"
            f"queue: {queue_s}")
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
        p = resolve_outbound(send.group(1))
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
                if use_file_flag(att.filename, att.content_type):
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
            target = resolve_target_dir(drop.group(1))
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
    client.run(TOKEN)
