#!/usr/bin/env python3
"""discord-send: one-shot Discord sender, stdlib-only.

Drop-in replacement for `hermes send --to discord ...` with no Hermes
dependency and no running gateway. Talks directly to the Discord REST API
using a bot token, exactly like `hermes send` does for bot-token platforms.

Usage:
    discord-send --to '#ops' "deploy finished"
    discord-send --to 123456789012345678 --file report.md
    echo "hi" | discord-send --to dm:987654321098765432
    discord-send --to '#ops' --subject "Nightly" --file /tmp/out.txt

Target formats (--to):
    #channel-name        look up a text channel by name (first match)
    <channel_id>         numeric channel id
    channel:<id>         explicit channel id
    dm:<user_id>         DM a user (creates the DM channel)
    user:<id> / @<id>    same as dm:<id>
    (omitted)            DEFAULT_DISCORD_CHANNEL from env

Config (env vars, or a .env file in cwd / ~/.config/opencode-discord/):
    DISCORD_BOT_TOKEN       required
    DEFAULT_DISCORD_CHANNEL optional default target

Exit codes mirror `hermes send`: 0 ok, 1 delivery failure, 2 usage error.
"""

import argparse
import json
import os
import sys
import urllib.request
import urllib.error

API = "https://discord.com/api/v10"
MAX_LEN = 2000


def load_dotenv():
    for path in (".env",
                 os.path.expanduser("~/.config/opencode-discord/.env")):
        if not os.path.isfile(path):
            continue
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        os.environ.setdefault(k.strip(), v.strip().strip("'\""))
        except OSError:
            pass


def api(token, method, path, payload=None):
    req = urllib.request.Request(
        API + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        method=method,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "opencode-discord-send/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode()
        except Exception:
            body = ""
        return e.code, {"_error": body}


def die_usage(msg):
    print(f"discord-send: {msg}", file=sys.stderr)
    sys.exit(2)


def die_delivery(msg):
    print(f"discord-send: delivery failed: {msg}", file=sys.stderr)
    sys.exit(1)


def resolve_channel(token, target):
    """Return (kind, id) where kind is 'channel' or 'dm-user'."""
    if not target:
        target = os.environ.get("DEFAULT_DISCORD_CHANNEL", "")
    if not target:
        die_usage("no target: pass --to or set DEFAULT_DISCORD_CHANNEL")
    t = target.strip()

    if t.startswith("#"):
        name = t[1:]
        status, guilds = api(token, "GET", "/users/@me/guilds")
        if status != 200:
            die_delivery(f"guild list -> HTTP {status}: {guilds}")
        for g in guilds if isinstance(guilds, list) else []:
            status, chans = api(token, "GET", f"/guilds/{g['id']}/channels")
            if status != 200 or not isinstance(chans, list):
                continue
            for c in chans:
                if c.get("name") == name and c.get("type") in (0, 5):
                    return "channel", c["id"]
        die_delivery(f"no text channel named #{name} visible to the bot")

    low = t.lower()
    for prefix in ("channel:", "dm:", "user:", "@"):
        if low.startswith(prefix):
            rest = t[len(prefix):]
            kind = "dm-user" if prefix in ("dm:", "user:", "@") else "channel"
            if not rest.isdigit():
                die_usage(f"bad target {t!r}: id must be numeric")
            return kind, rest
    if t.isdigit():
        return "channel", t
    die_usage(
        f"bad target {t!r}: use #name, <channel_id>, channel:<id>, "
        "dm:<user_id>, user:<id>, or @<user_id>"
    )


def ensure_dm_channel(token, user_id):
    status, data = api(token, "POST", "/users/@me/channels",
                       {"recipient_id": user_id})
    if status not in (200, 201):
        die_delivery(f"open DM -> HTTP {status}: {data}")
    return data["id"]


def chunk(text, n=MAX_LEN):
    while text:
        yield text[:n]
        text = text[n:]


def send_chunks(token, channel_id, text):
    sent = 0
    for part in chunk(text):
        status, data = api(token, "POST", f"/channels/{channel_id}/messages",
                           {"content": part})
        if status not in (200, 201):
            die_delivery(f"POST message -> HTTP {status}: {data}")
        sent += 1
    return sent


def list_targets(token, only=None):
    status, guilds = api(token, "GET", "/users/@me/guilds")
    if status != 200:
        die_delivery(f"guild list -> HTTP {status}: {guilds}")
    out = []
    for g in guilds if isinstance(guilds, list) else []:
        if only and only not in (g["name"], str(g["id"])):
            continue
        status, chans = api(token, "GET", f"/guilds/{g['id']}/channels")
        if status != 200 or not isinstance(chans, list):
            continue
        for c in sorted(chans, key=lambda c: c.get("position", 0)):
            if c.get("type") in (0, 5):
                out.append(f"discord:#{c['name']}  ({c['id']})  [{g['name']}]")
    return out


def read_body(args):
    if args.file:
        if args.file == "-":
            return sys.stdin.read()
        with open(args.file, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    if args.message:
        return args.message
    if not sys.stdin.isatty():
        return sys.stdin.read()
    die_usage("no message: pass text, --file, or pipe stdin")


def main():
    load_dotenv()
    p = argparse.ArgumentParser(
        prog="discord-send",
        description="Send a one-shot message to Discord (no Hermes needed).")
    p.add_argument("-t", "--to", default=None, help="target (see --help)")
    p.add_argument("-f", "--file", default=None,
                   help="read message body from file (- = stdin)")
    p.add_argument("-s", "--subject", default=None,
                   help="prepend a subject/header line")
    p.add_argument("-l", "--list", nargs="?", const="", metavar="GUILD",
                   help="list reachable text channels, then exit")
    p.add_argument("-q", "--quiet", action="store_true",
                   help="suppress stdout on success")
    p.add_argument("--json", action="store_true",
                   help="emit raw JSON result instead of human-readable output")
    p.add_argument("message", nargs="?", help="message text")
    args = p.parse_args()

    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        die_usage("DISCORD_BOT_TOKEN is not set (env or .env file)")

    if args.list is not None:
        targets = list_targets(token, args.list or None)
        if args.json:
            print(json.dumps({"targets": targets}))
        else:
            print("\n".join(targets) if targets else "(no channels found)")
        return

    body = read_body(args).rstrip("\n")
    if args.subject:
        body = f"**{args.subject}**\n{body}" if body else f"**{args.subject}**"
    if not body:
        die_usage("message body is empty")

    kind, ident = resolve_channel(token, args.to)
    channel_id = ensure_dm_channel(token, ident) if kind == "dm-user" else ident
    n = send_chunks(token, channel_id, body)

    if args.json:
        print(json.dumps({"ok": True, "channel_id": channel_id,
                          "chunks": n}))
    elif not args.quiet:
        print(f"sent ({n} chunk{'s' if n != 1 else ''}) -> {channel_id}")


if __name__ == "__main__":
    main()
