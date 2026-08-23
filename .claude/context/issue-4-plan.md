# Issue #4 — STT & TTS support

## Goal
Let a room talk to Claude by voice: inbound Matrix `m.audio` messages get transcribed
(Whisper) and routed like normal text, and assistant answers get spoken back as audio
(edge-tts), with a per-room `local` vs `cloud` toggle that is visible in `!status`.

## Assumptions (issue is under-specified)
- **"a setting in the chat in status"** → interpreted as a new per-room `!voice` command whose
  state is rendered by `!status`. Toggle is `!voice engine local|cloud`; `local` forces *both*
  STT and TTS to run offline on the host.
- **"all apis used need to be free"**:
  - TTS cloud = `edge-tts` (PyPI, Microsoft Edge read-aloud endpoint, **no API key**). Genuinely free.
  - STT cloud = there is **no keyless free Whisper API**. Closest free options need a free-tier
    token (HuggingFace Inference `openai/whisper-large-v3`, or Groq whisper). Plan: make the
    cloud STT backend a configurable OpenAI-compatible `/audio/transcriptions` URL + optional
    token (default: HF inference). If no token is configured, cloud STT falls back to local and
    says so in the room. **Flagging this — may need user decision.**
  - Local TTS = `piper-tts` if a voice model is present, else `espeak-ng`. Both free/offline.
  - Local STT = `faster-whisper`, default model `medium` (per issue), configurable.
- Only the **final assistant text** of a run is spoken, not tool notices or streamed partials.
- Voice is **off by default** per room; nothing changes for existing rooms until `!voice on`.

## Files to touch
- `app/config.py` — new typed settings: `voice_enabled_default`, `stt_backend`, `stt_api_url`,
  `stt_api_token`, `stt_model` (`medium`), `stt_max_seconds`, `tts_voice` (e.g. `de-DE-KatjaNeural`),
  `tts_max_chars`, `piper_model_path`. Env-driven per project convention.
- `app/models.py` — `Room` gains `stt_enabled`, `tts_enabled` (bool) and `voice_engine`
  (`cloud|local`), plus `tts_voice` override. Room state belongs on `Room` per CLAUDE.md.
- `app/db.py` — `create_all` does **not** ALTER existing tables; add a small idempotent
  "add missing columns" step in `init_db()` (PRAGMA table_info → `ALTER TABLE … ADD COLUMN`).
  Without this, existing `data/elementclaude.sqlite3` breaks on the new columns.
- `app/voice/stt.py` *(new)* — `transcribe(path, *, local: bool) -> str | None`; local via
  `faster-whisper` in `asyncio.to_thread` (blocking CPU), model lazily loaded and cached
  process-wide; cloud via httpx multipart POST.
- `app/voice/tts.py` *(new)* — `synthesize(text, *, local: bool, voice: str) -> Path | None`;
  cloud via `edge_tts.Communicate` → mp3 into `data/tts/`, local via piper/espeak subprocess →
  wav. Includes `speakable(text)` helper that strips markdown/code fences/URLs and truncates.
- `app/matrix/dispatcher.py` — split the current text branch into a reusable
  `_route_text(event, body)`; in the media branch, if `msgtype == "m.audio"` (or MSC3245 voice)
  **and** room STT is on → download, transcribe, echo `🎙️ "<transcript>"` as a notice, then
  `_route_text(...)` with the transcript so `!`-commands and shell stdin still work. Falls back
  to the existing attachment behaviour when STT is off or transcription fails.
- `app/agent/session.py` — in `_run_prompt`, accumulate the final assistant text; after
  `ResultMessage`, if room TTS is on, synthesize and `outbox.send_media(..., msgtype="m.audio")`.
  Best-effort: TTS failures log + never break the run.
- `app/commands/builtin.py` — new `cmd_voice` (`on|off`, `stt on|off`, `tts on|off`,
  `engine local|cloud`, `voice <name>`, no-args = show state), register in `BUILTINS`,
  add a `HelpSection`/`HelpEntry`, and add voice lines to `cmd_status` output.
- `pyproject.toml` — optional extra `[voice]` = `edge-tts`, `faster-whisper`; add `app.voice`
  to `tool.setuptools.packages`. Imports stay lazy so the base install keeps working.
- `.env.example` — document the new vars.
- `Dockerfile` — add `ffmpeg` (+ note that local Whisper models are large; docker mode is
  cloud-only by default).
- `CLAUDE.md` / `README.md` — directory layout + `!voice` docs.

## Approach
1. Settings + `Room` columns + idempotent SQLite column migration in `init_db()`.
2. `app/voice/tts.py` with edge-tts cloud path and `speakable()` text cleanup; wire it into
   `_run_prompt` behind the room flag. Verify end-to-end audio playback in Element first — it is
   the lower-risk half and needs no model downloads.
3. `app/voice/stt.py` with local faster-whisper path (lazy model, `to_thread`, duration cap).
4. Dispatcher rework: `_route_text` extraction + audio interception + transcript echo.
5. Cloud STT backend (OpenAI-compatible multipart) + graceful "no token → local" fallback.
6. Local TTS (piper/espeak) so `engine local` is fully offline.
7. `!voice` command, `!status` fields, `!help` entry.
8. Docs, `.env.example`, Dockerfile, optional dependency extra.

## Tests / verification
- No suite exists yet; add a first minimal `pytest` + `pytest-asyncio` set (wired into
  `pyproject.toml`) covering the pure logic only:
  - `speakable()` strips code fences/markdown and truncates at `tts_max_chars`.
  - `!voice` argument parsing → expected `upsert_room` fields (invalid args rejected).
  - `init_db()` column migration is idempotent and non-destructive on a pre-existing DB file.
- Manual, in a whitelisted room (host mode; user restarts the service themselves):
  1. `!voice on` → `!status` shows `voice … stt on / tts on / engine cloud`.
  2. Send an Element voice message → transcript notice appears, Claude answers, an `m.audio`
     reply plays back in Element.
  3. Speak a command (`"status"` won't have `!`; say a normal prompt) and verify a *typed*
     `!cancel` still works while voice is enabled.
  4. `!voice engine local` → repeat with network egress to edge/HF blocked; both directions
     must still work (after first model download).
  5. `!voice off` → audio messages fall back to the old "📎 attached" behaviour.
- Check `journalctl --user -u elementclaude` for no new exceptions; `!run` and image
  attachments must be unaffected.

## Out of scope
- Streaming / low-latency TTS, barge-in, or speaking partial tokens as they arrive.
- Speaking tool-use notices, `!run` shell output, or error notices.
- Proper Matrix voice-message metadata (MSC3245 waveform/duration) — we send plain `m.audio`.
- Wake words, speaker diarization, per-user voices, voice-based approval of tool permissions.
- Paid providers (OpenAI/ElevenLabs/Deepgram) and any change to `messaging-bot`.
- Bundling/auto-downloading Whisper or piper model weights into the Docker image.
- Starting/restarting the `elementclaude` systemd user service (explicitly forbidden).
