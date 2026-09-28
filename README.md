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

**Direct drop (no model involved):** attach file(s) with `place / put / save / drop / move / copy this in(to) <dir>` (e.g. `Place this in d:/projects`). The bot saves to `attachments/<dm_or_channel>/`, creates the target dir, moves the files, and confirms. opencode is never called, so filename/content moderation can't refuse.

**Direct send (no model involved):** `send me D:\files\clip.mp4` (or `send <path>`) with no attachments uploads that path straight via `discord.File`. If the text isn't an existing file path, it falls through to opencode as normal chat.

**Via opencode:** files save to `attachments/<dm_or_channel>/` and the local path is added to the prompt. Text/code/images (incl. gif) are also passed with `opencode run --file`. True video (`mp4/mov/mkv/avi/webm/m4v/mpg/mpeg/wmv/flv`, any `video/*`) is **path-only** — never inlined, the model uses shell/file tools on the saved path.

**Outbound via opencode:** the model emits `[[attach:D:\files\clip.mp4]]` on its own line; the bot strips the marker and uploads. Missing/oversize files come back as text errors, not silent fails.

Model-driven moves/writes require `OPENCODE_AUTO=1` (or an agent that can approve file tools).

## discord-send.py

One-shot REST sender, stdlib-only (tested: channel + DM delivery work):

```
python discord-send.py --to '#ops' "deploy finished"
python discord-send.py --to 123456789012345678 --file report.md
echo "hi" | python discord-send.py --to dm:987654321098765432
python discord-send.py --to '#ops' --subject "Nightly" --file out.txt
python discord-send.py --list
```

With `DEFAULT_DISCORD_CHANNEL` set in `.env`, omit `--to`. Targets: `#name`, `<channel_id>`, `channel:<id>`, `dm:<user_id>`, `user:<id>`, `@<id>`. Exit codes: 0 ok, 1 delivery failure, 2 usage error.

## Troubleshooting

- **"application did not respond"**: you used `/new`. Send `!new` as a plain message instead.
- **"unable to help" / content refusal on file moves**: use the direct drop form (attach + `Place this in d:/projects`) — it bypasses the model entirely. Same for sends: `send me D:\files\clip.mp4` uploads without asking the model.
- **"I don't see any file" / stuck refusal**: the opencode session predates the fix or a failed turn. Send `!new`, then resend the file fresh. Session continuity (`--session`) keeps old context otherwise.
- **`mkgy2(1)(2)(3).gif`**: same filename re-sent repeatedly; the inbox dedups instead of overwriting. Safe to delete `attachments/` contents.
- **No reply at all**: check stderr logs (`[dm:...]` / `[guild:...]` lines), verify Message Content intent is on, and that your user id is in `ALLOWED_USER_IDS`.
