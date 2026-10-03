---
name: discord-voice
description: "Speak in the Discord voice channel via the bot's say-queue, check voice status, pick voices, and understand the Voicebox voice loop (TTS replies, STT voice notes)."
version: 1.1.0
author: Hermes Agent
license: MIT
metadata:
  audience: maintainers
  workflow: discord-voice
---

# Discord Voice

Speak out loud in the server's voice channel through the
discord-bot + Voicebox loop. The bot sits in VC persistently (VC_AUTOJOIN)
and speaks every reply in the configured voice.

## Speak from a shell/agent session (primary path)

Drop a UTF-8 `.txt` file into the say-queue — no Discord message needed,
no opencode call. The bot's watcher picks it up within ~2s, speaks it,
deletes it:

```
<repo>/say_queue/
```

- Plain `*.txt` plays in every connected VC.
- `<guildid>_*.txt` targets one server, e.g. `<guildid>_note.txt`.
- `<profile>__*.txt` speaks it in another voice, e.g. `nicole__line.txt`
  (combine: `<guildid>_<profile>__*.txt`). Unknown names fall back to
  the default voice with a log warning. Each voice caches separately.
- Voices: `Computer` (default) plus presets `Nicole`, `River`, `Sarah`,
  `Sky`, `Bella` — and any cloned voice added in Voicebox. To see the
  live list, send `!voiceprofile <badname>` in Discord; the rejection
  message prints every available name.
- Keep it short (a sentence or two ≈ 5s of speech). Files over
  `SAY_MAX_BYTES` (8192) are skipped; speech truncates at
  `VOICEBOX_MAX_CHARS` (1200) — full text still posts to chat for replies.
- Code fences / markdown are stripped before speaking.

Verification: file deleted = TTS succeeded and queued for VC. Playback
itself is confirmed in the bot log (`speaking ...vc-*.wav`, then the
ffmpeg process terminating with code 0). If the file sits unprocessed,
no VC is connected (`!join` / check `VC_AUTOJOIN`).

## Readiness gate

Before a voice session (or before the first voice drop of your own
session), confirm all four preconditions — in Discord: `!voiceready`.
It reports one `OK`/`FAIL` line each for: Voicebox reachable, Discord
gateway connected, bot in a VC, and you in the same VC with the bot
(the last needs `VOICE_USER_ID` set in the bot env, else skipped).

Fail-safe: 3 consecutive Voicebox connection failures auto-delete
`.opencode/voice-mode.on` and log `voice mode auto-disabled`. Re-enable
with `voice mode on` once Voicebox is back. A dead gateway needs no
toggle — a down bot simply produces no voice until restarted.

Keep-warm: while this flag exists, the bot re-checks model residency
every `VOICEBOX_KEEPALIVE_S` seconds (default 300, `0` disables) and
reloads only lapsed models — instant no-op while warm, so steady-state
cost is ~zero. Deleting the flag (voice mode off) silences all probes,
e.g. while a local LLM holds the GPU. Startup and VC-join warmups run
regardless of the flag.

## Speak from Discord chat

- `!say <text>` — speaks immediately in VC, no opencode call.
- `!voiceprofile <name>` — this chat's voice from now on (preset voices
  use their own engine automatically).
- Say-queue files can use `<profile>__` to speak one line in another
  voice without switching.
- Normal replies are spoken automatically when voice is on.
- `!voice off` mutes spoken turn-replies for that chat. The say-queue is
  explicit operator intent and always speaks regardless of `!voice`.

## Check state

- `!status` in Discord shows `voice:` (on/off + profile) and `vc:` (channel,
  speaking/idle, queued clips) plus pending say files.
- Bot log lines: `voicebox TTS:` (generation), `speaking` (queued for VC),
  `say-queue:` (drop-in spoken), `voicebox STT:` (inbound transcription).

## Inbound (how users talk back)

Users record Discord voice notes; the bot transcribes via Voicebox Whisper
and opencode sees `[Voice message NAME (Ns): transcript]`. In servers,
voice notes don't need `@bot`/`!oc` — anything else still does. There is NO live
VC listening (discord.py cannot receive audio) — voice notes are the input
path. STT failure falls back to `--file`, never silence.

## Model markers (what the backend model can emit)

- `[[attach:FULL_PATH]]` — upload a file, stripped from text.
- `[[say:line]]` (max 3/turn) — spoken-only aside, stripped from text,
  played first in VC when live (in the chat's voice), else folded into
  `reply.wav`.
- `[[react:EMOJI]]` (max 5/turn) — react to the user's message.
- The bridge prompt tells the model its reply will be heard (VC live vs
  audio file), so it front-loads conclusions and keeps code in files.

## Voice mode (agent behavior toggle)

When the user enables voice mode ("voice mode on", or the flag file
`.opencode/voice-mode.on` exists), split every turn like a call with
screen-share: human replies go to voice, technical work stays in text.

- **Voice (say-queue):** 1–3 sentences, conversational, plain words. Status,
  summaries, questions, completion notices. No code, no paths, no URLs, no
  numbers-as-data, no markdown, no emoji — say "the bot file", not
  `bot.py` or a full path. One drop per turn max unless something
  genuinely needs interrupting; milestones over micro-steps.
- **Text (chat reply):** everything technical — code blocks, diffs, logs,
  exact commands, config values, file paths.
- Never read text content aloud verbatim; paraphrase the human meaning.
- Approvals first: before any action likely to raise a permission/approval
  gate, speak a one-line heads-up (what + why), then act. The gate itself
  still appears in text/UI as normal — voice announces it, never replaces
  it. TTS lags a few seconds behind, so keep the line short and let the
  text prompt carry the detail.
- "Voice mode off" or deleting the flag file returns to text-only replies
  (turn replies still follow the bot's own `!voice` setting).

## Pitfalls

1. Say-queue files are fire-and-forget: no confirmation except deletion.
   For must-confirm lines, check the bot log.
2. First TTS/STT after a Voicebox restart is slow (model load) unless
   keep-warm already reloaded it; Whisper may return empty once while
   downloading — the bot retries once, then falls back.
3. The bot only speaks when a VC is connected. DMs always get `reply.wav`.
4. Anyone with shell access to the bot host can make it speak — same trust
   level as `OPENCODE_AUTO=1`. Never speak secrets aloud; VC audio is heard
   by everyone in the channel.
