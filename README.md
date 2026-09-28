# opencode-discord

Two-way chat between Discord and opencode. DM the bot, it forwards to `opencode run` and replies with the result. No Hermes, no extra gateway.

- DMs: any message goes to opencode (per-user session)
- Servers: `@bot` mention or `!oc <prompt>` (per-channel session)
- `!new`: start a fresh opencode session (plain message with `!`, **not** `/new` — there are no slash commands)
- `!help`: usage
- Attachments in both directions
- `discord-send.py`: one-shot sender for shell/opencode (REST only, no gateway)

## Setup

1. Discord Developer Portal -> your app -> Bot:
   - Copy the token.
   - Enable **Privileged Gateway Intent: Message Content**.
   - Invite the bot to your server (bot scope, Send Messages / Read Messages / Attach Files) or just DM it.
2. Install:
   ```
   pip install -r requirements.txt
   ```
3. Configure (`.env` in this dir, or env vars):
   ```
   DISCORD_BOT_TOKEN=...
   ALLOWED_USER_IDS=243337216758120448
   OPENCODE_BIN=C:\Users\macdo\AppData\Roaming\npm\opencode.cmd
   OPENCODE_DIR=D:\Projects\discord-bot
   OPENCODE_AUTO=1
   LOG_LEVEL=INFO
   ```
4. Run:
   ```
   python bot.py
   ```

## Config reference

| Var | Default | Notes |
| --- | ------- | ----- |
| `DISCORD_BOT_TOKEN` | (required) | Bot token |
| `ALLOWED_USER_IDS` | empty (= anyone, warns) | Comma-separated Discord user ids |
| `OPENCODE_BIN` | `opencode` | Path to opencode binary/`.cmd` |
| `OPENCODE_DIR` | cwd | Working dir for opencode runs; relative attach paths resolve here |
| `OPENCODE_MODEL` | empty | Passed as `-m`, e.g. `provider/model#variant` |
| `OPENCODE_AGENT` | empty | Passed as `--agent` |
| `OPENCODE_AUTO` | `0` | Set `1` to pass `--auto` (auto-approve tools). Required for unattended file moves/writes. Only for users you trust |
| `OPENCODE_TIMEOUT` | `600` | Seconds per run |
| `STATE_FILE` | `state/sessions.json` | Maps Discord session key -> opencode session id |
| `GUILD_PREFIX` | `!oc` | Server-channel prefix |
| `ATTACH_DIR` | `attachments` | Inbound inbox root (`<dir>/<session_key>/`) |
| `MAX_ATTACH_MB` | `25` | In/out file size cap |
| `LOG_LEVEL` | `INFO` | `DEBUG` dumps full opencode stdout/stderr |

`sample.env` shows the same knobs.

## Attachments

**Inbound (Discord -> opencode):** files save to `attachments/<dm_or_channel>/` and the local path is added to the prompt. Text/code/images are also passed with `opencode run --file`. Video (`mp4/mov/mkv/avi/webm/m4v/mpg/mpeg/wmv/flv/gif`, any `video/*`) is **path-only** — never inlined, the model uses shell/file tools on the saved path.

Example: send `clip.mp4` with `drop this in d:\files` — the model gets `[Attached video: clip.mp4 (...) saved at D:\...\attachments\...\clip.mp4 - NOT inlined, use shell/file tools on this path]`.

**Outbound (opencode -> Discord):** ask for a file by path (`send me D:\files\clip.mp4`). The model emits `[[attach:D:\files\clip.mp4]]` on its own line; the bot strips the marker and uploads via `discord.File`. Missing/oversize files come back as text errors, not silent fails.

Requires `OPENCODE_AUTO=1` (or an agent that can approve file tools) for actual moves/writes.

## discord-send.py

One-shot REST sender, stdlib-only:

```
python discord-send.py --to '#ops' "deploy finished"
python discord-send.py --to 123456789012345678 --file report.md
echo "hi" | python discord-send.py --to dm:987654321098765432
python discord-send.py --to '#ops' --subject "Nightly" --file out.txt
python discord-send.py --list
```

Targets: `#name`, `<channel_id>`, `channel:<id>`, `dm:<user_id>`, `user:<id>`, `@<id>`, or `DEFAULT_DISCORD_CHANNEL` env. Exit codes: 0 ok, 1 delivery failure, 2 usage error.

## Troubleshooting

- **"application did not respond"**: you used `/new`. Send `!new` as a plain message instead.
- **"I don't see any file" / stuck refusal**: the opencode session predates the fix or a failed turn. Send `!new`, then resend the file fresh. Session continuity (`--session`) keeps old context otherwise.
- **"unable to help" on file moves**: set `OPENCODE_AUTO=1` and restart. Without `--auto`, non-interactive runs can't approve writes.
- **`mkgy2(1)(2)(3).gif`**: same filename re-sent repeatedly; the inbox dedups instead of overwriting. Safe to delete `attachments/` contents.
- **No reply at all**: check stderr logs (`[dm:...]` / `[guild:...]` lines), verify Message Content intent is on, and that your user id is in `ALLOWED_USER_IDS`.
