# opencode-discord

Two-way chat between Discord and opencode. DM the bot, it forwards to `opencode run` and replies with the result. No Hermes, no extra gateway.

- DMs: any message goes to opencode (per-user session)
- Servers: `@bot` mention or `!oc <prompt>` (per-channel session)
- `!new`: start a fresh opencode session (plain message with `!`, **not** `/new` — there are no slash commands)
- `!status`: session id, model, voice/VC state, say-queue pending, turns, tokens, cost, inbox, queue state
- `!voice [on|off]`: spoken replies via Voicebox (Computer voice); `!join` / `!leave`: speak replies in your voice channel (servers)
- `!say <text>`: speak a line in VC now, no opencode call; `!skip`: stop the current VC clip and drop the queue (stays connected); `!voiceready`: readiness check (voicebox + discord + VC + you-in-VC)
- `!model`: show current + available; `!model provider/name` to switch, `!model clear` to reset
- `!cancel`: stop the in-flight opencode run for this chat (repeat if a follow-up started; no-op when idle)
- `/sessions`: dropdown browser to switch opencode sessions (servers; DMs after global sync)
- `/model`: autocomplete to switch the model for this chat
- `!help`: usage
- Reactions: hourglass while working, check when done, cross on failure
- Dynamic presence: Listening when idle, DND "working..." while any turn runs
- Per-session coalescing queue (a message arriving mid-turn merges into one follow-up, not its own run)
- Fence-aware chunking (`chunking.py`, shared with the sender) + long replies sent as `reply.md`
- Attachments in both directions (direct drop/send bypass the model)
- Voice loop via Voicebox: record a voice note and the transcript goes to opencode; replies come back spoken (voice channel when `!join`ed, else `reply.wav`)
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
5. Voice (optional): with Voicebox running (`VOICEBOX_URL`), set `VC_AUTOJOIN` to your voice channel id (right-click → Copy Channel ID) and `VOICE_USER_ID` to your user id — then `!voiceready` in Discord confirms the whole loop. Details in Voice below.

## Config reference

| Var | Default | Notes |
| --- | ------- | ----- |
| `DISCORD_BOT_TOKEN` | (required) | Bot token |
| `ALLOWED_USER_IDS` | empty (= anyone, warns) | Comma-separated Discord user ids |
| `OPENCODE_BIN` | `opencode` | Path to opencode binary/`.cmd` |
| `OPENCODE_DIR` | cwd | Working dir for opencode runs; relative attach paths resolve here |
| `FILE_JAIL` | `OPENCODE_DIR` | Allowlist root for drop targets + send sources; empty also fails closed to `OPENCODE_DIR`. DMs and guilds share the same rule (see Trust model) |
| `OPENCODE_MODEL` | empty | Passed as `-m`, e.g. `provider/model#variant` |
| `OPENCODE_AGENT` | empty | Passed as `--agent` |
| `OPENCODE_AUTO` | `0` | Set `1` to pass `--auto` (auto-approve tools). Required for unattended file moves/writes. Only for users you trust |
| `OPENCODE_TIMEOUT` | `600` | Seconds per run |
| `MAX_CONCURRENT_TURNS` | `2` | Max simultaneous opencode runs across all chats; extra turns wait FIFO (see `!status`); `0` = unlimited |
| `STATE_FILE` | `state/sessions.json` | Maps Discord session key -> opencode session id |
| `GUILD_PREFIX` | `!oc` | Server-channel prefix |
| `ATTACH_DIR` | `attachments` | Inbound inbox root (`<dir>/<session_key>/`) |
| `MAX_ATTACH_MB` | `25` | In/out file size cap |
| `ATTACH_KEEP_FILES` | `50` | Inbox prune: keep newest N files (`0` = unlimited) |
| `ATTACH_KEEP_MB` | `500` | Inbox prune: total MB cap (`0` = unlimited) |
| `LOG_LEVEL` | `INFO` | `DEBUG` dumps full opencode stdout/stderr |
| `REPLY_AS_FILE_LIMIT` | `4000` | Replies longer than this go out as `reply.md`; `0` disables |
| `REACT_START` / `REACT_DONE` / `REACT_ERROR` | hourglass / check / cross | Working/done/error reactions; empty disables |
| `VOICEBOX_URL` | `http://127.0.0.1:17493` | Voicebox base URL; empty disables all voice features |
| `VOICEBOX_PROFILE` | `Computer` | Voice profile name or id for spoken replies |
| `VOICEBOX_VOICE` | `1` | `1` = attach a spoken reply to every turn; `0` = text-only unless a chat opts in with `!voice on` |
| `VOICEBOX_TIMEOUT` | `120` | Seconds per TTS/STT request |
| `VOICEBOX_MAX_CHARS` | `1200` | Max chars sent to TTS per turn (full text still posts) |
| `VC_CHUNK_CHARS` | `400` | Max chars per spoken chunk when streaming to VC (smaller = first audio sooner) |
| `VOICEBOX_TRANSCRIBE` | `1` | `1` = transcribe inbound voice notes/audio via Voicebox Whisper; `0` = pass raw audio with `--file` |
| `VOICEBOX_STT_MODEL` | empty (= server default) | Whisper size: `base`/`small`/`medium`/`large`/`turbo` |
| `VOICEBOX_LANGUAGE` | empty (= auto) | STT language hint, e.g. `en` |
| `FFMPEG_BIN` | `ffmpeg` | ffmpeg executable for voice-channel playback |
| `OPUS_LIB` | empty (= auto-detect) | Explicit path to the Opus DLL; otherwise `libopus-0.x64.dll` / `opus.dll` next to `bot.py`, then system `opus` |
| `SAY_DIR` | `say_queue` | Dir watched for `*.txt` drop-ins the bot speaks in VC (empty disables); see Voice |
| `SAY_POLL` | `2.0` | Seconds between say-queue scans |
| `SAY_MAX_BYTES` | `8192` | Largest say file accepted (bigger is skipped) |
| `VC_AUTOJOIN` | empty (= disabled) | Voice channel id to join on startup and sit in until restart |
| `VC_AUTOREJOIN` | `1` | `1` = rejoin the autojoin channel if disconnected unexpectedly (`!leave` still sticks) |
| `VOICEBOX_WARMUP` | `1` | `1` = one silent TTS at startup so the first real reply skips model-load cost |
| `VOICE_USER_ID` | empty (= skip) | Your Discord user id; `!voiceready` checks you're sitting in the VC with the bot |

`sample.env` shows the same knobs.

## Behavior

- **Reacts:** hourglass on your message means it's accepted and working; check means done; cross means something failed (details in console).
- **Coalescing:** if you send more messages while a turn is running, they merge into a single follow-up turn (separated by `---`) instead of one run each. The follow-up reply covers everything merged. To force separate turns, wait for the check react first.
- **Cancel:** `!cancel` kills the in-flight opencode run for that chat, drops queued follow-ups, posts `cancelled ...`, and flips reacts to cross. One `!cancel` kills the current run — if a follow-up already started, send it again. If the turn already reached the reply/voice stage there's no subprocess left, so `!cancel` only drops queued messages and says so. Other chats are unaffected, and voice-channel playback is never stopped (use `!skip` for that).
- **Skip:** `!skip` stops the current voice-channel clip, drops the queued clips, and abandons in-flight TTS for that server, then posts `skipped ...`. The bot stays connected (this is not `!leave`). Idle with nothing queued posts `nothing playing.`; in DMs or when not in a voice channel it posts `not in a voice channel.` Other servers are unaffected.
- **Presence:** Listening while idle, DND "working..." while any turn runs (waiting turns count as working).
- **Concurrency cap:** at most `MAX_CONCURRENT_TURNS` opencode runs at once (default 2, `0` = unlimited); extra turns wait FIFO and show in `!status` (`queue: ... | global: N running, M waiting`). `!cancel` while waiting drops the turn without running it.
- **Usage:** per-turn tokens/cost logged; `!status` shows session totals (in-memory, resets on restart).
- **Guild sessions are shared:** everyone talking to the bot in one channel shares that channel's opencode session. Replies thread under your message.

## Attachments

**Direct drop (no model involved):** attach file(s) with `place / put / save / drop / move / copy this in(to) <dir>` (e.g. `Place this in d:/projects`). The bot saves to `attachments/<dm_or_channel>/`, creates the target dir, moves the files, and confirms. opencode is never called, so filename/content moderation can't refuse.

**Direct send (no model involved):** `send me D:\files\clip.mp4` (or `send <path>`) with no attachments uploads that path straight via `discord.File`. If the text isn't an existing file path, it falls through to opencode as normal chat.

**Via opencode:** files save to `attachments/<dm_or_channel>/` and the local path is added to the prompt. Text/code/images (incl. gif) are also passed with `opencode run --file`. True video (`mp4/mov/mkv/avi/webm/m4v/mpg/mpeg/wmv/flv`, any `video/*`) is **path-only** — never inlined, the model uses shell/file tools on the saved path. Audio notes (`ogg/opus/mp3/wav/m4a/flac/aac/webm`, any `audio/*`) are **transcribed** via Voicebox first — opencode sees `[Voice message NAME (Ns): transcript]`, never the raw bytes (falls back to `--file` if transcription fails).

**Outbound via opencode:** the model emits `[[attach:D:\files\clip.mp4]]` on its own line; the bot strips the marker and uploads. Markers pointing at nonexistent files are silently skipped (quoted doc examples must never spam the channel); oversize files come back as text errors. `[[say:Deploy complete.]` (max 3 per turn) is a spoken-only aside — stripped from text, played in VC when live, else folded into `reply.wav`. The model can also react to your message with `[[react:EMOJI]]` (literal emoji, or custom `<:name:id>`; max 5 per turn, invalid ones are skipped with a console warning).

Model-driven moves/writes require `OPENCODE_AUTO=1` (or an agent that can approve file tools).

**Trust model (file jail):** direct drops, direct sends, and model-emitted `[[attach:]]` markers all resolve the user/model-specified side through `FILE_JAIL` (default `OPENCODE_DIR`; empty fails closed to `OPENCODE_DIR` — there is no unlimited mode). `..`, mixed separators, drive-letter case, UNC/extended paths, and symlinks/junctions are resolved first (`Path.resolve()`), then checked — string-prefix bypasses don't work, and the drop handler checks *before* `mkdir` (never creates the refused dir). Outside-jail targets/sources are refused with a chat error and a log line; the same rule applies in DMs and guilds. Inbox paths (`attachments/<key>/`) are bot-managed and unaffected. Loud/silent rule: a jail violation is LOUD (chat error) only when the resolved path exists — a real exfiltration attempt. Nonexistent outside-jail paths stay silent: `[[attach:]]` markers skip as before (quoted doc examples must never spam), and `send me <nonexistent>` falls through to opencode as normal chat.

## Voice

Full async voice loop backed by Voicebox (`VOICEBOX_URL`, on by default — no `PyNaCl`/`davey`/Opus needed for any of this):

- **You → bot:** record a Discord voice note (or attach audio). The bot transcribes it via Voicebox Whisper and opencode sees `[Voice message voice-message.ogg (12.4s): ...]`. Send a note alone or with text. If transcription fails, the raw file falls back to `--file`.
- **Bot → you:** every reply is also spoken in `VOICEBOX_PROFILE` (default `Computer`). Speech is capped at `VOICEBOX_MAX_CHARS` (full text always posts); long replies stream sentence-by-sentence (first audio starts while later sentences still generate), short ones go as one clip. Code fences, `[[attach:]]`/`[[react:]]`/`[[say:]]` markers, and markdown links are stripped before speaking. The model is told its reply will be heard, so it front-loads conclusions and keeps code in `[[attach:]]` files — and it can emit `[[say:line]]` (max 3) for spoken-only asides that stay out of the text.

Spoken delivery, in order of preference:

1. **Voice channel (servers):** sit in a VC and send `!join` (`!oc !join` / `@bot !join`). Replies stream sentence-by-sentence (first audio starts while later sentences still generate) and queue serially — no file to click. `!leave` disconnects. DMs have no VC, so they always use files.
2. **`reply.wav` file:** when not joined (or in DMs), the spoken reply attaches to the channel.

Per-chat control: `!voice off` mutes spoken replies entirely (VC or file), `!voice on` re-enables; `!voice` shows state. TTS failure never blocks the text reply.

**Staying in VC:** set `VC_AUTOJOIN` to a voice channel id and the bot joins it on startup and sits there until restart (`VC_AUTOREJOIN=1` rejoins after unexpected drops; `!leave` is still respected and sticks). Find the id by right-clicking the channel → Copy Channel ID (Developer Mode on).

**Readiness gate:** `!voiceready` checks all four preconditions and reports `OK`/`FAIL` per line: Voicebox reachable (with ping ms), Discord gateway connected, bot in a VC, and you in the same VC (needs `VOICE_USER_ID` set, else skipped). Run it before a voice session instead of guessing.

**Fail-safe auto-off:** 3 consecutive Voicebox *connection* failures (unreachable/down — not HTTP errors, which mean it's alive) delete the `.opencode/voice-mode.on` flag and log `voice mode auto-disabled`. Say `voice mode on` to re-enable once Voicebox is back. The bot can only detect this while running, so a dead gateway (bot itself down) just means no voice at all until restart.

**Say-queue (agent-initiated speech):** the bot watches `SAY_DIR` (default `say_queue/`) every `SAY_POLL` seconds. Drop in a `.txt` file and it speaks it in VC via the same Computer voice — no Discord message needed, no opencode call. Plain `*.txt` plays in every connected VC; `<guildid>_*.txt` targets one server. Files are deleted after speaking; if no VC is connected they're held until one is (TTS failures retry with backoff). This is the path for speaking *from* a shell/agent session: anything that can write a file (including opencode itself mid-turn, or another harness) can make the bot talk. `!say <text>` is the same thing from Discord chat. `!status` shows pending say files.

**Voice mode (agent behavior):** when enabled, the agent splits turns like a call with screen-share — human summaries, status, questions, and completion notices go to voice (one short say-queue drop per turn, plain conversational language, no code/paths/URLs); all technical content (code, diffs, logs, exact commands, paths) stays in text chat. Before any action likely to raise a permission/approval gate, it speaks a one-line heads-up first (the gate itself still appears in text as normal). Toggle: say `voice mode on/off`, backed by the flag file `.opencode/voice-mode.on` (presence = on). The flag is local-only and gitignored, so it never leaks into clones — but any agent session in this repo (including the bot's own opencode backend, which discovers the `discord-voice` skill) honors it. Defined in full in `.opencode/skills/discord-voice/SKILL.md`.

Prerequisites: Voicebox running with a `Computer` (or your) profile and a Whisper model downloaded (first `/transcribe` may return 202 while it downloads — the bot treats that as "transcription unavailable" and falls back to `--file`). VC playback additionally needs `ffmpeg` on PATH and Opus (`pip install -r requirements.txt` covers `PyNaCl`/`davey`; the Windows Opus DLL is auto-loaded from next to `bot.py`, override with `OPUS_LIB`). There is no live VC *listening* — `discord.py` can't receive audio; voice notes are the input path.

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

## Tests

Stdlib-only suite (`tests/`, `unittest`) — no Discord token, voice, or
network needed. `tests/__init__.py` stubs the `discord` module and pins
config to temp dirs before importing `bot.py`, so it runs on any machine
with Python 3.9+:

```
python -m unittest discover -s tests -t .
```

Covers: marker parsing (`[[attach:]]`/`[[react:]]`/`[[say:]]` incl. caps),
sentence chunking, `clean_for_tts`, turn-queue coalescing (buffered
arrivals drain as one follow-up batch), the voicebox readiness
counter/auto-off, say-queue consume/hold/speak/backoff, prompt assembly
(bridge note, model-arg resolution, usage extraction), file-jail
confinement, inbox pruning, and session-state persistence. The tests
encode current behavior — if one fails after a change, the change
altered behavior; fix the code or file a new issue, not the test.

## Troubleshooting

- **"application did not respond"**: you used `/new`. Send `!new` as a plain message instead.
- **"unable to help" / content refusal on file moves**: use the direct drop form (attach + `Place this in d:/projects`) — it bypasses the model entirely. Same for sends: `send me D:\files\clip.mp4` uploads without asking the model.
- **"I don't see any file" / stuck refusal**: the opencode session predates the fix or a failed turn. Send `!new`, then resend the file fresh. Session continuity (`--session`) keeps old context otherwise.
- **`mkgy2(1)(2)(3).gif`**: same filename re-sent repeatedly; the inbox dedups instead of overwriting. Safe to delete `attachments/` contents.
- **My messages got answered together**: that's coalescing — arrivals during a running turn merge into one follow-up. Wait for the check react between messages for separate turns.
- **Raw tool output in chat**: tool results (`part.type: tool`) are filtered out; only assistant text is forwarded. If dumps leak through on a new event shape, grab the `opencode event types: [...]` DEBUG line and it can be added to the filter.
- **Operational chatter**: agent/session notices ("Switched agent to Build") are filtered from chat. If a future opencode schema produces zero known chat events, the bot falls back to a greedy extract and logs a warning — noise over silence, and the warning says so.
- **No reply at all**: check stderr logs (`[dm:...]` / `[guild:...]` lines), verify Message Content intent is on, and that your user id is in `ALLOWED_USER_IDS` (anyone else is silently ignored).
- **Voice note came back as a file reference, not a transcript**: Voicebox was unreachable, still downloading the Whisper model (first run), or Whisper returned empty twice. The bot falls back to `--file` so nothing is lost — check the bot log for `voicebox STT` lines and retry.
- **No spoken reply**: `!voice off` mutes TTS per chat (`!status` shows it); empty `VOICEBOX_URL` disables voice globally; TTS failure only ever drops the audio, text always posts. Run `!voiceready` — it pinpoints which side is down.
- **First reply after idle is slow**: cold Voicebox model load costs ~25s once; the bot sends a silent warmup TTS at startup (`VOICEBOX_WARMUP`, watch for `voice warmup:` in the log). Manually unloading the model in Voicebox UI re-cools it.
- **`!join` says Opus isn't loaded**: the bot needs `libopus-0.x64.dll` next to `bot.py` (or set `OPUS_LIB`), plus `ffmpeg` on PATH. The startup log says which opus path it loaded.
