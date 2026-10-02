# Roadmap

Where the bot goes next. Each item is a GitHub issue and lands as its own
PR. Checked off as they merge.

Voice loop status: done and shipped (TTS replies, VC playback + streaming,
STT voice notes, say-queue, `!say`, `!voiceready`, autojoin, warmup,
`[[say:]]` marker, voice-aware bridge note). Everything below builds on it.

## Control (brakes first — nothing here can currently be stopped)

- [ ] #1 `!cancel` in-flight opencode turn
- [ ] #2 `!skip` VC playback
- [ ] #3 Global `MAX_CONCURRENT_TURNS`

## Safety

- [ ] #4 Jail drop/send filesystem paths
- [ ] #5 Role-based permissions (DJ/admin split)

## Media

- [ ] #6 Video thumbnails via ffmpeg

## UX polish

- [ ] #7 Reaction controls (cancel / replay / regenerate)
- [ ] #8 Auto-leave VC when alone
- [ ] #9 TTS hash cache
- [ ] #10 Per-chat voice profile (`!voiceprofile`)
- [ ] #12 Slash parity for `!` commands
- [ ] #13 Scheduled/proactive text

## Process

- [ ] #11 Committed `tests/` suite

---

Filed as issues #1–#13 (in order), each with acceptance criteria for its PR.
