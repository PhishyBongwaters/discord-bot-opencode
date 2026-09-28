#!/usr/bin/env python3
"""opencode-discord bot: two-way DM chat between Discord and opencode.

- DMs the bot -> forwarded to `opencode run`; the reply comes back to the DM.
- In guilds: reacts to @mentions or `!oc` prefix (per-channel sessions).
- `!new` resets your session, `!help` shows help.
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

Required Discord privileged intent: Message Content (toggle in the
Developer Portal -> Bot -> Privileged Gateway Intents).
"""

import asyncio
import json
import os
import re
import subprocess
import sys
from pathlib import Path

try:
    import discord
except ImportError:
    sys.exit("discord.py not installed: pip install -r requirements.txt")

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

    for line in ndjson_text.splitlines():
        line = line.strip()
        if not line:
            continue

        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            parts.append(line)
            continue

        # top-level text fields
        for key in ("text", "content", "delta", "message"):
            v = obj.get(key)
            if isinstance(v, str) and v:
                parts.append(v)

        # opencode v2 event format
        part = obj.get("part")
        if isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str) and text:
                parts.append(text)

    return "".join(parts).strip()

def run_opencode(prompt, session_key):
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
    cmd.append(prompt)

    try:
        proc = subprocess.run(
            cmd, cwd=OPENCODE_DIR, capture_output=True, text=True,
            timeout=OPENCODE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return (f"opencode timed out after {OPENCODE_TIMEOUT}s. "
                "Your session is kept; try a smaller task or `!new`."), session_id
    except FileNotFoundError:
        return (f"could not execute `{OPENCODE_BIN}` - is opencode installed "
                "and on PATH?"), session_id

    out = proc.stdout or ""
    print("=== STDOUT ===")
    print(out)
    print("=== STDERR ===")
    print(proc.stderr)
    new_sid = extract_session_id(out) or session_id
    text = strip_json_events(out).strip()
    if proc.returncode != 0 and not text:
        err = (proc.stderr or "").strip()[-1500:]
        text = f"opencode exited with code {proc.returncode}."
        if err:
            text += f"\n```\n{err}\n```"
    if not text:
        text = "(opencode returned no text)"
    return text, new_sid


def chunk(text, n=MAX_DISCORD):
    # prefer splitting on newlines near the boundary
    while len(text) > n:
        cut = text.rfind("\n", 0, n)
        cut = cut if cut > n // 2 else n
        yield text[:cut]
        text = text[cut:].lstrip("\n")
    if text:
        yield text


intents = discord.Intents.default()
intents.message_content = True
intents.dm_messages = True

client = discord.Client(intents=intents)


def session_key_for(message):
    if isinstance(message.channel, discord.DMChannel):
        return f"dm:{message.author.id}"
    return f"guild:{message.channel.id}"


@client.event
async def on_ready():
    print(f"logged in as {client.user} (id {client.user.id})")


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

    if not content:
        return

    if content.lower() in ("!help", "help"):
        await message.channel.send(
            "DM me anything and I'll run it through opencode.\n"
            "`!new` - start a fresh opencode session\n"
            f"`{GUILD_PREFIX} <prompt>` - use me in a server channel")
        return

    key = session_key_for(message)
    if content.lower() == "!new":
        SESSIONS.pop(key, None)
        save_state(SESSIONS)
        await message.channel.send("fresh opencode session started.")
        return

    prompt = content
    async with message.channel.typing():
        reply, new_sid = await asyncio.to_thread(run_opencode, prompt, key)
    if new_sid and new_sid != SESSIONS.get(key):
        SESSIONS[key] = new_sid
        save_state(SESSIONS)
    for part in chunk(reply):
        await message.channel.send(part)


if __name__ == "__main__":
    if _TOKEN_ERROR:
        sys.exit(_TOKEN_ERROR)
    client.run(TOKEN)
