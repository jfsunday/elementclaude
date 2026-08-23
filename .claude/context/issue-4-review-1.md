# Review 1 — Issue #4 (STT & TTS support)

Branch: `auto/issue-4-feature-add-stt-and-tts-support` · single commit `2b961b4` vs `develop`
Tests: `uv run --extra dev pytest -q` → **32 passed** (0.62s). Import smoke test of
`app.main`, `app.voice.*`, `app.matrix.dispatcher`, `app.agent.session` → OK.

## Verdict — one sentence

NEEDS_FIX — the feature is architecturally sound, well-tested and faithful to the issue, but
the two headline paths fail *silently* when their optional dependency is missing, `speakable()`
mangles every `snake_case` identifier it reads aloud, and the documented "free cloud STT"
default URL points at a HuggingFace host that was retired.

## What was requested vs what was built

| Issue requirement | Built | Where |
|---|---|---|
| "Add TTS … with Microsoft edge tts" | `edge_tts.Communicate(text, voice).save()` → mp3 → `m.audio` | `app/voice/tts.py:70-82`, `app/agent/session.py:322-339` |
| "Add STT … with whisper" | `faster-whisper` in `asyncio.to_thread`, process-wide cached model, `_local_lock` serialises runs | `app/voice/stt.py:29-59` |
| "transcribe with whisper medium" | `stt_model` default `"medium"` | `app/config.py:45` |
| "or any free api" | Configurable HF-inference **or** OpenAI-compatible `/audio/transcriptions` (Groq etc.), auto-detected by URL suffix | `app/voice/stt.py:71-91` |
| "if a setting in the chat in status is set use local" | `!voice engine local\|cloud` persisted on `Room.voice_engine`, rendered in `!status` **and** `!voice` | `app/commands/builtin.py:138,174-223` |
| "if the same setting is set also it should be run locally" (STT too) | Same flag drives both: `stt.resolve_local()` and `local=room.voice_engine == "local"` for TTS | `app/voice/stt.py:16-26`, `app/agent/session.py:333` |
| "all apis used need to be free" | edge-tts keyless; HF/Groq free tier; local = faster-whisper + piper/espeak-ng, fully offline. Honestly documented that no keyless Whisper API exists. | `README.md`, `.env.example` |
| (implied) voice input behaves like typed input | `_route_text()` extracted and reused, so a transcript still hits shell stdin, pending-question answers and `!`-commands | `app/matrix/dispatcher.py:31-67,124` |

Extras beyond the issue, all justified: idempotent SQLite `ALTER TABLE` backfill
(`app/db.py:30-86`) — required, since `create_all` would otherwise break existing
`data/elementclaude.sqlite3`; first test suite in the repo; `EXTRAS=` support in `run-host.sh`;
`ffmpeg`/`espeak-ng` in the Dockerfile; docs in `README.md` + `CLAUDE.md`.

Verified correct by inspection, not just by diff:
- No TTS echo loop: `messaging-bot/app/matrix/callbacks.py:25-26` drops the bot's own events,
  so the `m.audio` we post never re-enters `_handle_media`.
- `messaging-bot`'s `/api/messages/media` passes `msgtype` straight through
  (`app/routes/media.py:47`, `app/matrix/client.py:251-286`), so `m.audio` is accepted and
  gets encrypted in E2EE rooms. No change to `messaging-bot` was made. ✅
- `_add_missing_columns` renders `NOT NULL DEFAULT` correctly for the four new columns and is
  a no-op on fresh DBs; covered by `tests/test_db_migration.py`.
- `final_text` survives the trailing `flush_stream(final=True)` (early-returns on empty
  `stream_text` without clearing it), so the right text is spoken.

## Bugs / gaps

1. **TTS fails completely silently when `edge-tts` / `espeak-ng` is unavailable.**
   `_synthesize_cloud` (`app/voice/tts.py:71-75`) and `_synthesize_local` (`:106-110`) log a
   warning and return `None`; `_maybe_speak` then returns without a word. A user who follows
   the README, runs `!voice on` without `EXTRAS=voice`, gets **zero** feedback in the room —
   only a journal line. For the headline feature of this issue that's not acceptable.

2. **`speakable()` destroys `snake_case` identifiers.** `_MD_EMPHASIS`
   (`app/voice/tts.py:21`) treats `_` as emphasis. Verified:
   - `"I updated some_var_name and other_thing_here"` → `"I updated somevarname and otherthinghere"`
   - `"Fixed __init__ and _add_missing_columns"` → `"Fixed init and addmissing_columns"`

   In a *coding* assistant that reads answers aloud, identifiers are exactly what you want to
   hear correctly. `*bold*` handling is fine; only `_` is the problem.

3. **Default `STT_API_URL` is very likely dead.**
   `https://api-inference.huggingface.co/models/openai/whisper-large-v3`
   (`app/config.py:50`, `.env.example`, `README.md`) — HuggingFace retired the
   `api-inference.huggingface.co` serverless host in favour of `router.huggingface.co`
   /`hf-inference` provider routes. Couldn't verify from here (no network egress in this
   sandbox), but if it 404s, the *only* documented free cloud STT path is broken on arrival
   and the fallback to local silently swallows it.

4. **The "falling back to local" notice is emitted on every single voice message.**
   `app/matrix/dispatcher.py:105-107` posts `ℹ️ no STT_API_TOKEN set — transcribing locally`
   per audio event. With the default config (`engine cloud`, no token) that's noise on every
   turn, forever.

5. **STT length cap is unenforceable without `info.duration`.** `app/matrix/dispatcher.py:78-84`
   only applies `stt_max_seconds` when the client sent `info.duration`. Element voice messages
   do, but any other `m.audio` (or a re-uploaded file) bypasses the cap entirely — and there is
   no byte-size guard either, so an arbitrarily long file goes straight into `faster-whisper`.

6. **No progress feedback on the first local transcription.** `outbox.set_typing(room_id, True)`
   is called once (`dispatcher.py:109`) with the default 30 s timeout, but the first local run
   downloads the ~1.5 GB `medium` model. The room looks dead for minutes. `_typing_keepalive`
   already exists in `app/agent/session.py:311-319` and is not reused here.

7. **`run-host.sh` only supports one extra.** `UV_EXTRA_ARGS=(--extra "$EXTRAS")` —
   `EXTRAS="voice dev"` produces `--extra "voice dev"` and fails. Low impact, but the README
   makes `EXTRAS=` the documented install path.

8. *(Risk note, out of scope — do not fix here)* In **unencrypted** rooms `messaging-bot`
   normalises media through the `RoomMessageMedia` branch (`callbacks.py:45-57`), and nio's
   `RoomMessageMedia` has no `msgtype` attribute (`nio/events/room_events.py:939-949`), so
   `content["msgtype"]` comes through as `None` and `_is_media_message()` drops the event.
   Encrypted DMs are fine (`RoomEncryptedAudio` is *not* a `RoomMessageMedia`, so it hits the
   generic raw-content fallback). Since `messaging-bot` is off-limits, this should at least be
   noted in the README: **STT works in encrypted rooms; unencrypted rooms drop audio.**

## Style / conventions

Conventions are followed closely — nothing to complain about structurally:
- `from __future__ import annotations` present in every new module ✅
- Room state only mutated via `upsert_room` / `audit`; no direct table writes ✅
- New settings are typed `Field`s in `app/config.py`, no ad-hoc `os.environ` ✅
- Heavy voice deps imported lazily inside functions; base install still imports fine ✅
  (verified: `import app.voice.stt, app.voice.tts` works without the extra)
- New columns are nullable-or-defaulted per the (newly documented) `_add_missing_columns` rule ✅
- `app.voice` added to `tool.setuptools.packages` ✅
- Systemd service was not touched; `run-host.sh` is the only thing that writes the unit ✅
- Commit message in English, single line ✅

Two nitpicks:
- `app/voice/tts.py:94` exceeds the ~100-col width the rest of the file keeps to.
- `speakable()` runs all regexes over the *full* answer and truncates last
  (`app/voice/tts.py:31-50`). Truncating first would bound the lazy-quantifier work in
  `_MD_EMPHASIS` on multi-thousand-line answers.

## Suggested fixes

1. Surface unavailable backends instead of no-oping:
   - Add a small `available(local: bool) -> str | None` probe in `app/voice/tts.py`
     (importlib for `edge_tts`, `shutil.which` for `piper`/`espeak-ng`) and call it from
     `cmd_voice` when `tts_enabled` is switched on → reply
     `⚠️ tts on, but edge-tts is missing — start with EXTRAS=voice ./run-host.sh`.
   - Same for STT in `cmd_voice` when `stt_enabled` flips on and `faster_whisper` is absent.
   - Optionally also post a one-shot notice from `_maybe_speak` the first time synthesis
     returns `None` for a room.
2. Fix `_MD_EMPHASIS` so it leaves `snake_case` alone — either drop `_` from the alternation
   entirely (Claude's output uses `*`/`**` for emphasis anyway) or require a word boundary,
   e.g. `(?<![\w])(_{1,3})(?=\S)(.+?)(?<=\S)\1(?![\w])`. Add a regression test to
   `tests/test_speakable.py` asserting `speakable("_add_missing_columns")` round-trips.
3. Verify `STT_API_URL` against live HuggingFace. If the legacy host is gone, switch the
   default to the current router URL (or make the default empty and document Groq's
   `https://api.groq.com/openai/v1/audio/transcriptions` as the recommended free tier).
4. Emit the `no STT_API_TOKEN` note at most once per room (module-level `set[str]` of room ids,
   or just drop it from `_handle_media` and show it in `!voice` / `!status` instead, where the
   engine is already displayed).
5. Add a byte-size guard next to the duration check in `_handle_media` (e.g. skip STT above
   `stt_max_seconds * 32 KiB`) so a missing `info.duration` can't bypass the cap.
6. Reuse `_typing_keepalive` (or send a one-line `🎙️ transcribing…` notice) around
   `stt.transcribe` so the first, model-downloading run doesn't look like a hang.
7. `run-host.sh`: split `EXTRAS` on whitespace —
   `for e in $EXTRAS; do UV_EXTRA_ARGS+=(--extra "$e"); done` and build `EXTRA_FLAGS` the same
   way.
8. Add one README line: STT currently only reaches elementclaude from **encrypted** rooms
   (see Bugs #8) — no code change, just set the expectation.
