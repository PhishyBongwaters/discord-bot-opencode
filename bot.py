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
from pathlib import Path

from chunking import split_smart

try:
    import discord
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

TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
ALLOWED = {u.strip() for u in os.environ.get("ALLOWED_USER_IDS", "").split(",")
           if u.strip()}
OPENCODE_BIN = os.environ.get("OPENCODE_BIN", "opencode")
OPENCODE_DIR = os.environ.get("OPENCODE_DIR", os.getcwd())
OPENCODE_MODEL = os.environ.get("OPENCODE_MODEL", "").strip()
OPENCODE_AGENT = os.environ.get("OPENCODE_AGENT", "").strip()
OPENCODE_AUTO = os.environ.get("OPENCODE_AUTO", "0") == "1"
OPENCODE_TIMEOUT = int(os.environ.get("OPENCODE_TIMEOUT", "600"))
STATE_FILE = Path(os.environ.get("STATE_FILE", "state/sessions.json"))
GUILD_PREFIX = os.environ.get("GUILD_PREFIX", "!oc")
ATTACH_DIR = Path(os.environ.get("ATTACH_DIR", "attachments"))
MAX_ATTACH_MB = float(os.environ.get("MAX_ATTACH_MB", "25"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
REPLY_AS_FILE_LIMIT = int(os.environ.get("REPLY_AS_FILE_LIMIT", "4000"))
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
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


SESSIONS = load_state()  # key -> opencode session id


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
    parts = []
    seen_types = set()

    def collect_text(obj, depth=0):
        # Recursively collect human text; returns list of strings.
        found = []
        if isinstance(obj, dict):
            for k, v in obj.items():
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

    for line in ndjson_text.splitlines():
        line = line.strip()
        if not line:
            continue

        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            parts.append(line)
            continue

        if isinstance(obj, dict):
            t = obj.get("type")
            if isinstance(t, str):
                seen_types.add(t)
            # skip pure lifecycle events with no text payload
            part = obj.get("part")
            if isinstance(part, dict):
                ptype = part.get("type")
                if isinstance(ptype, str):
                    seen_types.add(f"part:{ptype}")
                text = part.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
                    continue
            # fall back to recursive collect for message/result events
            for s in collect_text(obj):
                # skip id-like strings and timestamps
                if s.startswith(("ses_", "prt_", "msg_")):
                    continue
                if s in ("step-start", "step-finish", "text"):
                    continue
                parts.append(s)
        else:
            parts.append(str(obj))

    if seen_types:
        log.debug("opencode event types: %s", sorted(seen_types))
    return "".join(parts).strip()

def run_opencode(prompt, session_key, files=None):
    """Run one opencode turn. Returns (reply_text, session_id_or_None)."""
    cmd = [OPENCODE_BIN, "run", "--format", "json"]
    session_id = SESSIONS.get(session_key)
    if session_id:
        cmd += ["--session", session_id]
    if OPENCODE_MODEL:
        cmd += ["-m", OPENCODE_MODEL]
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


def session_key_for(message):
    if isinstance(message.channel, discord.DMChannel):
        return f"dm:{message.author.id}"
    return f"guild:{message.channel.id}"


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
        for part in split_smart(reply, MAX_DISCORD):
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
            log.info("[%s] turn: %d msg(s), %d chars + %d files",
                     key, len(batch), len(combined), len(files))
            log.debug("[%s] full prompt:\n%s", key, combined[:3000])
            async with channel.typing():
                reply, new_sid = await asyncio.to_thread(
                    run_opencode, combined, key, files)
            if new_sid and new_sid != SESSIONS.get(key):
                SESSIONS[key] = new_sid
                save_state(SESSIONS)
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


@client.event
async def on_ready():
    log.info("logged in as %s (id %s)", client.user, client.user.id)
    await update_presence()


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
        await message.channel.send(
            "DM me anything and I'll run it through opencode.\n"
            "`!new` - start a fresh opencode session (plain message, not /new)\n"
            f"`{GUILD_PREFIX} <prompt>` - use me in a server channel\n"
            "Attach + `place this in <dir>` saves files directly.\n"
            "`send me <path>` sends a file back directly.")
        return

    key = session_key_for(message)
    if content.lower() == "!new":
        SESSIONS.pop(key, None)
        save_state(SESSIONS)
        await message.channel.send("fresh opencode session started.")
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
