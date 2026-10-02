---
name: discord-voice
description: "Speak in the Discord voice channel via the bot's say-queue, check voice status, and understand the Voicebox voice loop (TTS replies, STT voice notes)."
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  audience: maintainers
  workflow: discord-voice
---

# Discord Voice

Speak out loud in the server's voice channel (General) through the
discord-bot + Voicebox loop. The bot sits in VC persistently (VC_AUTOJOIN)
and speaks every reply in the Computer voice.

## Speak from a shell/agent session (primary path)

Drop a UTF-8 `.txt` file into the say-queue — no Discord message needed,
no opencode call. The bot's watcher picks it up within ~2s, speaks it,
deletes it:

```
D:\Projects\discord-bot\say_queue\
```

- Plain `*.txt` plays in every connected VC (this server has one).
- `<guildid>_*.txt` targets one server, e.g. `1513943523043446987_note.txt`.
- Keep it short (a sentence or two ≈ 5s of speech). Files over
  `SAY_MAX_BYTES` (8192) are skipped; speech truncates at
  `VOICEBOX_MAX_CHARS` (1200) — full text still posts to chat for replies.
- Code fences / markdown are stripped before speaking.

Verification: file deleted = TTS succeeded and queued for VC. Playback
itself is confirmed in the bot log (`speaking ...vc-*.wav`, then the
ffmpeg process terminating with code 0). If the file sits unprocessed,
no VC is connected (`!join` / check `VC_AUTOJOIN`).

## Speak from Discord chat

- `!say <text>` — speaks immediately in VC, no opencode call.
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
and opencode sees `[Voice message NAME (Ns): transcript]`. There is NO live
VC listening (discord.py cannot receive audio) — voice notes are the input
path. STT failure falls back to `--file`, never silence.

## Voice mode (agent behavior toggle)

When the user enables voice mode ("voice mode on", or the flag file
`.opencode/voice-mode.on` exists), split every turn like a call with
screen-share: human replies go to voice, technical work stays in text.

- **Voice (say-queue):** 1–3 sentences, conversational, plain words. Status,
  summaries, questions, completion notices. No code, no paths, no URLs, no
  numbers-as-data, no markdown, no emoji — say "the bot file", not
  `D:\Projects\discord-bot\bot.py`. One drop per turn max unless something
  genuinely needs interrupting; milestones over micro-steps.
- **Text (chat reply):** everything technical — code blocks, diffs, logs,
  exact commands, config values, file paths.
- Never read text content aloud verbatim; paraphrase the human meaning.
- "Voice mode off" or deleting the flag file returns to text-only replies
  (turn replies still follow the bot's own `!voice` setting).

## Pitfalls

1. Say-queue files are fire-and-forget: no confirmation except deletion.
   For must-confirm lines, check the bot log.
2. First TTS/STT after Voicebox restart is slow (model load); Whisper may
   return empty once while downloading — the bot retries once, then falls
   back.
3. The bot only speaks when a VC is connected. DMs always get `reply.wav`.
4. Anyone with shell access to the bot host can make it speak — same trust
   level as `OPENCODE_AUTO=1`. Never speak secrets aloud; VC audio is heard
   by everyone in the channel.
