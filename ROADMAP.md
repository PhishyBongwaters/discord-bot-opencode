# Roadmap

Where the bot goes next. Each item is a GitHub issue and lands as its own
PR. Checked off as they merge.

Voice loop status: done and shipped (TTS replies, VC playback + streaming,
STT voice notes, say-queue, `!say`, `!voiceready`, autojoin, warmup,
`[[say:]]` marker, voice-aware bridge note). Everything below builds on it.

## Control (brakes first — nothing here can currently be stopped)

- [x] #1 `!cancel` in-flight opencode turn
- [x] #2 `!skip` VC playback
- [x] #3 Global `MAX_CONCURRENT_TURNS`

## Safety

- [x] #4 Jail drop/send filesystem paths
- [x] #5 Role-based permissions (DJ/admin split)

## Media

- [x] #6 Video thumbnails via ffmpeg

## UX polish

- [x] #7 Reaction controls (cancel / replay / regenerate)
- [x] #8 Auto-leave VC when alone
- [x] #9 TTS hash cache
- [x] #10 Per-chat voice profile (`!voiceprofile`)
- [x] #12 Slash parity for `!` commands
- [x] #13 Scheduled/proactive text

## Process

- [x] #11 Committed `tests/` suite

---

Filed as issues #1–#13 (in order), each with acceptance criteria for its PR.
