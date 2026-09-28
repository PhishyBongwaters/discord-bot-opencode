# opencode-discord

Two-way chat between Discord and opencode. DM the bot, it forwards to `opencode run` and replies with the result. No Hermes, no extra gateway.

- DMs: any message goes to opencode (per-user session)
- Servers: `@bot` mention or `!oc <prompt>` (per-channel session)
- `!new`: start a fresh opencode session (plain message with `!`, **not** `/new` — there are no slash commands)
- `!status`: session id, model, turns, tokens, cost, inbox, queue state
- `/sessions`: dropdown browser to switch opencode sessions (servers; DMs after global sync)
- `/model`: autocomplete to switch the model for this chat
- `!help`: usage
- Reactions: hourglass while working, check when done, cross on failure
- Dynamic presence: Listening when idle, DND "working..." while any turn runs
- Per-session coalescing queue (a message arriving mid-turn merges into one follow-up, not its own run)
- Fence-aware chunking (`chunking.py`, shared with the sender) + long replies sent as `reply.md`
- Attachments in both directions (direct drop/send bypass the model)
- `discord-send.py`: one-shot sender for shell/opencode (REST only, no gateway)

## Setup

1. Discord Developer Portal -> your app -> Bot:
   - Copy the token.
   - Enable **Privileged Gateway Intent: Message Content**.
   - Invite the bot to your server (bot scope, Send Messages / Read Messages / Attach Files / Add Reactions) or just DM it. For slash commands (`/sessions`), the invite must also include the `applications.commands` scope — re-invite if `/sessions` doesn't appear.
2. Install:
   ```
   pip install -r requirements.txt
   ```
3. Configure (`.env` in this dir, or env vars):
   ```
   DISCORD_BOT_TOKEN=...
   ALLOWED_USER_IDS=243337216758120448
   DEFAULT_DISCORD_CHANNEL=1513943523894759606
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
| `ATTACH_KEEP_FILES` | `50` | Inbox prune: keep newest N files (`0` = unlimited) |
| `ATTACH_KEEP_MB` | `500` | Inbox prune: total MB cap (`0` = unlimited) |
| `LOG_LEVEL` | `INFO` | `DEBUG` dumps full opencode stdout/stderr |
| `REPLY_AS_FILE_LIMIT` | `4000` | Replies longer than this go out as `reply.md`; `0` disables |
| `REACT_START` / `REACT_DONE` / `REACT_ERROR` | hourglass / check / cross | Working/done/error reactions; empty disables |

`sample.env` shows the same knobs.

## Behavior

- **Reacts:** hourglass on your message means it's accepted and working; check means done; cross means something failed (details in console).
- **Coalescing:** if you send more messages while a turn is running, they merge into a single follow-up turn (separated by `---`) instead of one run each. The follow-up reply covers everything merged. To force separate turns, wait for the check react first.
- **Presence:** Listening while idle, DND "working..." while any turn runs.
- **Usage:** per-turn tokens/cost logged; `!status` shows session totals (in-memory, resets on restart).
- **Guild sessions are shared:** everyone talking to the bot in one channel shares that channel's opencode session. Replies thread under your message.

## Attachments

**Direct drop (no model involved):** attach file(s) with `place / put / save / drop / move / copy this in(to) <dir>` (e.g. `Place this in d:/projects`). The bot saves to `attachments/<dm_or_channel>/`, creates the target dir, moves the files, and confirms. opencode is never called, so filename/content moderation can't refuse.

**Direct send (no model involved):** `send me D:\files\clip.mp4` (or `send <path>`) with no attachments uploads that path straight via `discord.File`. If the text isn't an existing file path, it falls through to opencode as normal chat.

**Via opencode:** files save to `attachments/<dm_or_channel>/` and the local path is added to the prompt. Text/code/images (incl. gif) are also passed with `opencode run --file`. True video (`mp4/mov/mkv/avi/webm/m4v/mpg/mpeg/wmv/flv`, any `video/*`) is **path-only** — never inlined, the model uses shell/file tools on the saved path.

**Outbound via opencode:** the model emits `[[attach:D:\files\clip.mp4]]` on its own line; the bot strips the marker and uploads. Markers pointing at nonexistent files are silently skipped (quoted doc examples must never spam the channel); oversize files come back as text errors. The model can also react to your message with `[[react:EMOJI]]` (literal emoji, or custom `<:name:id>`; max 5 per turn, invalid ones are skipped with a console warning).

Model-driven moves/writes require `OPENCODE_AUTO=1` (or an agent that can approve file tools).

## Slash commands

`/sessions` shows a dropdown of the bot project's opencode sessions (newest first, via `opencode session list`). Each entry is tagged `[bot]` (the bot has used it in this chat) or `[cli]` (terminal/other), with the active session starred. Pick one to switch this chat to it, or `+ New session` for a fresh start. Everything is ephemeral (only you see it).

`/model` switches the model for this chat with autocomplete over `opencode models` (`provider/model`, 50+ entries — hence search, not a dropdown). Pick `Default` to clear back to `OPENCODE_MODEL`/opencode default. Overrides persist per chat and show in `!status`.

Notes: per-guild sync is instant on startup; global sync (which covers DMs) can take up to an hour to propagate. Session listing is project-scoped to `OPENCODE_DIR` — terminal sessions in other directories won't appear.

## discord-send.py

One-shot REST sender, stdlib-only (tested: channel + DM delivery work):

```
python discord-send.py --to '#ops' "deploy finished"
python discord-send.py --to 123456789012345678 --file report.md
echo "hi" | python discord-send.py --to dm:987654321098765432
python discord-send.py --to '#ops' --subject "Nightly" --file out.txt
python discord-send.py --list
```

With `DEFAULT_DISCORD_CHANNEL` set in `.env`, omit `--to`. Targets: `#name`, `<channel_id>`, `channel:<id>`, `dm:<user_id>`, `user:<id>`, `@<id>`. Exit codes: 0 ok, 1 delivery failure, 2 usage error. Requires `chunking.py` alongside for fence-aware splitting. Retries Discord 429s (up to 3, honors `retry_after`).

## Troubleshooting

- **"application did not respond"**: you used `/new`. Send `!new` as a plain message instead.
- **"unable to help" / content refusal on file moves**: use the direct drop form (attach + `Place this in d:/projects`) — it bypasses the model entirely. Same for sends: `send me D:\files\clip.mp4` uploads without asking the model.
- **"I don't see any file" / stuck refusal**: the opencode session predates the fix or a failed turn. Send `!new`, then resend the file fresh. Session continuity (`--session`) keeps old context otherwise.
- **`mkgy2(1)(2)(3).gif`**: same filename re-sent repeatedly; the inbox dedups instead of overwriting. Safe to delete `attachments/` contents.
- **My messages got answered together**: that's coalescing — arrivals during a running turn merge into one follow-up. Wait for the check react between messages for separate turns.
- **Raw tool output in chat**: tool results (`part.type: tool`) are filtered out; only assistant text is forwarded. If dumps leak through on a new event shape, grab the `opencode event types: [...]` DEBUG line and it can be added to the filter.
- **No reply at all**: check stderr logs (`[dm:...]` / `[guild:...]` lines), verify Message Content intent is on, and that your user id is in `ALLOWED_USER_IDS` (anyone else is silently ignored).
