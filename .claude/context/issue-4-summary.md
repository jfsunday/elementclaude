Closes #4

Automated via auto-code.

> ⚠️ Review status after 3 review pass(es): **NEEDS_FIX**. Please inspect carefully before merging.

## Plan
# Issue #4 — STT & TTS support

### Goal
Let a room talk to Claude by voice: inbound Matrix `m.audio` messages get transcribed
(Whisper) and routed like normal text, and assistant answers get spoken back as audio
(edge-tts), with a per-room `local` vs `cloud` toggle that is visible in `!status`.

### Assumptions (issue is under-specified)
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

### Files to touch
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

### Approach
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

### Tests / verification
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

### Out of scope
- Streaming / low-latency TTS, barge-in, or speaking partial tokens as they arrive.
- Speaking tool-use notices, `!run` shell output, or error notices.
- Proper Matrix voice-message metadata (MSC3245 waveform/duration) — we send plain `m.audio`.
- Wake words, speaker diarization, per-user voices, voice-based approval of tool permissions.
- Paid providers (OpenAI/ElevenLabs/Deepgram) and any change to `messaging-bot`.
- Bundling/auto-downloading Whisper or piper model weights into the Docker image.
- Starting/restarting the `elementclaude` systemd user service (explicitly forbidden).

## Review passes
### Pass 1
# Review 1 — Issue #4 (STT & TTS support)

Branch: `auto/issue-4-feature-add-stt-and-tts-support` · single commit `2b961b4` vs `develop`
Tests: `uv run --extra dev pytest -q` → **32 passed** (0.62s). Import smoke test of
`app.main`, `app.voice.*`, `app.matrix.dispatcher`, `app.agent.session` → OK.

#### Verdict — one sentence

NEEDS_FIX — the feature is architecturally sound, well-tested and faithful to the issue, but
the two headline paths fail *silently* when their optional dependency is missing, `speakable()`
mangles every `snake_case` identifier it reads aloud, and the documented "free cloud STT"
default URL points at a HuggingFace host that was retired.

#### What was requested vs what was built

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

#### Bugs / gaps

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

#### Style / conventions

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

#### Suggested fixes

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

### Pass 2
# Review 2 — Issue #4 (STT & TTS support)

Branch: `auto/issue-4-feature-add-stt-and-tts-support` · `2b961b4` + `6eeb574` vs `develop`
Tests: `uv run --extra dev pytest -q` → **34 passed** (0.71s), +2 regression tests since review 1.
Import smoke test of `app.main`, `app.voice.*`, `app.matrix.dispatcher`, `app.agent.session`,
`app.commands.builtin` on a base install (no `voice` extra) → OK.

#### Verdict — one sentence

NEEDS_FIX — every substantive finding from review 1 is genuinely fixed and verified, but this
commit adds voice *system* packages to the Dockerfile plus a comment claiming "docker mode is
cloud-voice by default" while never installing the `[voice]` extra, so cloud TTS is dead in the
documented `dc up` deployment, and `.env.example` hands out an install command the README
explicitly says does not work.

#### What was requested vs what was built

| Issue requirement | Built | Where |
|---|---|---|
| "Add TTS … with Microsoft edge tts" | `edge_tts.Communicate(text, voice).save()` → mp3 → `m.audio` | `app/voice/tts.py:89-101`, `app/agent/session.py:316-342` |
| "Add STT … with whisper" | `faster-whisper`, lazily loaded + process-cached, `asyncio.to_thread` under `_local_lock` | `app/voice/stt.py:51-75` |
| "transcribe with whisper medium" | `stt_model` default `"medium"` | `app/config.py:45` |
| "or any free api" | HF inference (raw body) **or** OpenAI-compatible `/audio/transcriptions` (Groq), auto-detected by URL suffix | `app/voice/stt.py:87-107` |
| "if a setting in the chat in status is set use local" | `!voice engine local\|cloud` on `Room.voice_engine`, rendered by `!status` and `!voice` | `app/commands/builtin.py:138,199-238` |
| "…also it should be run locally" (STT too) | Same flag drives both: `stt.resolve_local()` / `local=room.voice_engine == "local"` | `app/voice/stt.py:17-27`, `app/agent/session.py:326` |
| "all apis used need to be free" | edge-tts keyless; HF/Groq free tier; local = faster-whisper + piper/espeak-ng. Honest docs that no keyless Whisper API exists. | `README.md:100-141`, `.env.example:45-77` |
| (implied) spoken input == typed input | `_route_text()` shared by `m.text` and transcripts → shell stdin, pending answers, `!`-commands all still work | `app/matrix/dispatcher.py:32-68,158` |

### Review-1 findings — all verified fixed

1. **Silent failure** → `tts.unavailable_reason()` / `stt.unavailable_reason()` probes, surfaced in
   `!voice` (`builtin.py:224-236`), once-per-room in `_maybe_speak` (`session.py:329-338`), and in
   the transcription failure notice (`dispatcher.py:148-153`). Verified live: base install reports
   ``` `edge-tts` is not installed ``` / ``` `faster-whisper` is not installed ```.
2. **`snake_case` mangling** → `_` removed from `_MD_EMPHASIS`, new bounded `_MD_UNDERSCORE`
   (`tts.py:22-25`). Verified: `some_func(a_b) and __init__` round-trips intact, `_italic_` still
   collapses, `**strong**`/`~~strike~~` unaffected. Two regression tests added.
3. **Dead HF URL** → now `router.huggingface.co/hf-inference/…`. Verified over the network from
   this machine: new URL returns **401** (endpoint alive, wants a token), legacy
   `api-inference.huggingface.co` fails to connect at all. Correct call.
4. **Per-message fallback noise** → note dropped from `_handle_media` (`dispatcher.py:122-123`),
   shown in `!voice` instead (`builtin.py:227-229`).
5. **Duration cap bypass** → byte-size guard at `dispatcher.py:101-110` (`32 KiB/s * stt_max_seconds`).
6. **Model-download hang** → `_typing_keepalive` moved to `outbox.typing_keepalive()` and reused by
   both the STT path (`dispatcher.py:138`) and `_run_prompt` (`session.py:388`), plus a one-shot
   "first run downloads the model" notice gated on `stt.model_loaded()`.
7. **`EXTRAS` word-splitting** → `run-host.sh:36-40` loops, README documents `EXTRAS="voice dev"`.
8. **Unencrypted-room caveat** → documented in `README.md:138-141`, no `messaging-bot` change. ✅

#### Bugs / gaps

1. **Docker mode cannot do cloud TTS — and the Dockerfile now claims it can.**
   `Dockerfile:19` is still `RUN pip install --no-cache-dir .` — no `[voice]` extra, so `edge_tts`
   is absent from the image. But `Dockerfile:5-6` (added by *this* change) says *"ffmpeg +
   espeak-ng back the voice features. Whisper model weights are NOT baked in …, so docker mode is
   cloud-voice by default."* With `voice_engine_default=cloud` (`config.py:42`), the reality in a
   container is the exact inverse of the comment:
   - cloud TTS (`edge-tts`): **broken** — the only thing the comment promises,
   - local TTS (`espeak-ng`): **works** — the thing the comment says is not available,
   - local STT (`faster-whisper`): **broken**,
   - cloud STT: works (pure `httpx`), token permitting.

   Worse, the new failure message is host-only advice: a Docker user running `!voice on` is told
   ``start with `EXTRAS=voice ./run-host.sh` `` (`tts.py:69`), which is meaningless inside the
   container. `dc up --build -d` is a first-class documented deployment path in `CLAUDE.md`, and
   `edge-tts` is a small pure-Python dependency, so there is no size argument for leaving it out.

2. **`.env.example:45` contradicts the README and `run-host.sh`.**
   It says *"Needs the optional extra: `uv pip install -e '.[voice]'`"*, while `run-host.sh:14-16`
   and `README.md:125-127` explicitly warn that *"`uv run` re-syncs the venv on every start …
   a manual `uv pip install '.[voice]'` gets pruned away again on the next start."* A user who
   follows `.env.example` installs the extra, watches it vanish on the next restart, and lands
   right back in the silent-failure scenario review 1 was about.

3. **Two notices for one over-long voice message.** When `info.duration` exceeds the cap,
   `dispatcher.py:91-95` posts *"🎙️ longer than 300s — attaching instead of transcribing"*, then
   falls through to `dispatcher.py:112-117` which posts *"📎 attached — will hand it to Claude with
   your next message."* The parallel size-cap branch (`:102-110`) gets this right by returning
   early with a single combined notice. Cosmetic, but inconsistent within the same function.

4. **Groq misconfiguration degrades to a generic error.** `stt.py:96` is
   `settings.stt_api_model or settings.stt_model` — `stt_model` defaults to `"medium"`, which is a
   faster-whisper size, not a valid OpenAI/Groq model id. Setting only `STT_API_URL` to Groq
   (which `.env.example:52` presents as a copy-paste option) yields a 400 that `transcribe()`
   swallows into `"could not transcribe"`. `STT_API_MODEL` is documented, but the fallback silently
   guarantees failure rather than saying "set `STT_API_MODEL` for OpenAI-compatible endpoints".

5. **`_prune()` can eat the answer it just produced (narrow race).** `tts.py:159` calls `_prune`
   *outside* the `try/except` at `:147-154`, and `_prune` globs then `p.stat()`s
   (`tts.py:84`). Two rooms answering concurrently can hit `FileNotFoundError` in the sort key,
   which propagates out of `synthesize()` into `_maybe_speak`'s handler — the audio was rendered
   successfully but never gets sent, and the room hears nothing.

#### Style / conventions

Conventions are followed throughout; nothing structural to complain about.
- `from __future__ import annotations` in every new module ✅
- Room state mutated only via `upsert_room` / `audit`, never direct table writes ✅
- New settings are typed `Field`s in `app/config.py`, no ad-hoc `os.environ` ✅
- Voice deps imported lazily; base install verified importable without the extra ✅
- New columns nullable-or-defaulted, backfilled by `_add_missing_columns`, covered by
  `tests/test_db_migration.py` (idempotent + row-preserving) ✅
- `app.voice` in `tool.setuptools.packages` ✅; systemd unit untouched ✅; `messaging-bot` untouched ✅
- Commit messages English, single line ✅

Nits:
- `except (asyncio.CancelledError, Exception): pass` (`dispatcher.py:145`, `session.py:430`) is
  `except BaseException` written the long way. It works, but `except Exception` after the
  `.cancel()` plus a narrow `asyncio.CancelledError` clause would say what is actually meant.
- `_tts_warned` (`session.py:313`) grows unbounded per room id. Harmless at this scale, but it is
  process-global mutable state with no eviction, same shape as `_pending_attachments`.
- `builtin.py:138` (`!status` voice line) is ~115 cols, past the width the rest of the file keeps.

#### Suggested fixes

1. Make the Dockerfile match its own comment — `RUN pip install --no-cache-dir '.[voice]'` (or at
   minimum add `edge-tts`, which is pure Python and tiny). If `faster-whisper` is deliberately
   excluded to keep the image small, say *that* in the comment instead of "cloud-voice by default",
   and make `unavailable_reason()` not recommend `EXTRAS=voice ./run-host.sh` unconditionally —
   e.g. `"install the `voice` extra (`EXTRAS=voice ./run-host.sh`, or rebuild the image)"`.
2. `.env.example:45`: replace `uv pip install -e '.[voice]'` with `EXTRAS=voice ./run-host.sh`
   so all three places (env example, README, `run-host.sh` header) agree.
3. `dispatcher.py`: make the duration branch return early with one combined notice, mirroring the
   size branch — e.g. queue the attachment and post
   `"🎙️ longer than {n}s — 📎 attached instead."`, then `return`.
4. `stt.py:96`: drop the `or settings.stt_model` fallback and fail loudly when an
   OpenAI-compatible URL is configured without `STT_API_MODEL` (log + return `None` with a reason),
   so the room is told what to set instead of a generic "could not transcribe".
5. `tts.py`: move `_prune(directory)` inside the `try` (or wrap it in its own
   `try/except Exception: logger.debug(...)`) so a pruning race can never suppress an audio file
   that was rendered successfully.

### Pass 3
# Review 3 — Issue #4 (STT & TTS support)

Branch: `auto/issue-4-feature-add-stt-and-tts-support` · `2b961b4` + `6eeb574` + `bd26996` vs `develop`
Tests: `uv run --extra dev pytest -q` → **34 passed** (0.75s). `speakable()` probed live against
6 real-world inputs. Working tree clean.

#### Verdict — one sentence

NEEDS_FIX — every finding from reviews 1 and 2 is genuinely fixed and the feature now matches the
issue end to end, but `speakable()` returning an empty string for a code-only or URL-only answer is
reported to the room as *"tts is on but silent: synthesis failed"*, and the brand-new
`pip install '.[voice]'` in the Dockerfile pulls `ctranslate2` into `python:3.12-slim` without
`libgomp1` — neither the build nor the import was ever exercised.

#### What was requested vs what was built

| Issue requirement | Built | Where |
|---|---|---|
| "Add TTS … with Microsoft edge tts" | `edge_tts.Communicate(text, voice).save()` → mp3 → `m.audio` via `outbox.send_media` | `app/voice/tts.py:106-118`, `app/agent/session.py:316-342` |
| "Add STT … with whisper" | `faster-whisper`, lazily imported, process-cached model, `asyncio.to_thread` under `_local_lock` | `app/voice/stt.py:59-89` |
| "transcribe with whisper medium" | `stt_model` default `"medium"` | `app/config.py:45` |
| "or any free api" | HF inference (raw body) **or** OpenAI-compatible `/audio/transcriptions` (Groq), auto-detected by `_is_openai_compatible()` | `app/voice/stt.py:36-39,101-128` |
| "if a setting in the chat in status is set use local" | `!voice engine local\|cloud` persisted on `Room.voice_engine`, rendered by `!status` **and** `!voice` | `app/commands/builtin.py:138-139,193,221` |
| "…also it should be run locally" (STT too) | One flag drives both: `stt.resolve_local()` / `local=room.voice_engine == "local"` | `app/voice/stt.py:17-27`, `app/agent/session.py:326` |
| "all apis used need to be free" | edge-tts keyless; HF/Groq free tier; local = faster-whisper + piper/espeak-ng. Honest docs that no keyless Whisper API exists. | `README.md:100-141`, `.env.example:45-80` |
| (implied) spoken input == typed input | `_route_text()` shared by `m.text` and transcripts → PTY stdin, pending answers, `!`-commands | `app/matrix/dispatcher.py:32-68,164` |

### Review-2 findings — all verified fixed

1. **Docker had no `[voice]` extra while claiming cloud voice** → `Dockerfile:19` is now
   `pip install --no-cache-dir '.[voice]'`, the misleading comment is gone, `HF_HOME=/data/hf-cache`
   lands on the `./data:/data` volume (`docker-compose.yml:18`) so model weights survive a rebuild,
   and `unavailable_reason()` now says *"install the `voice` extra (`EXTRAS=voice ./run-host.sh`, or
   rebuild the image)"* (`tts.py:69-72`, `stt.py:52-55`). README/`.env.example` agree.
2. **`.env.example` contradicting the README** → now points at `EXTRAS=voice ./run-host.sh` and notes
   the docker image ships it (`.env.example:46-48`).
3. **Two notices for one over-long voice message** → `too_long` computed up front, media downloaded
   with `queue=True`, single combined notice, early `return` (`dispatcher.py:85-103`). Verified the
   `not want_stt` fall-through can no longer double-post.
4. **Groq misconfiguration → generic error** → `or settings.stt_model` fallback dropped;
   `_transcribe_cloud` refuses with a log line (`stt.py:112-114`) and `unavailable_reason(local=False)`
   surfaces it in `!voice` before the first failure (`stt.py:44-50`).
5. **`_prune()` race eating fresh audio** → wrapped in `try/except OSError` with a per-file `_mtime()`
   that swallows a vanished entry (`tts.py:85-103`).
6. Nits also addressed: `except (CancelledError, Exception)` split into two clauses
   (`dispatcher.py:149-152`, `session.py:430-433`), `!status` voice line rewrapped
   (`builtin.py:138-139`).

Re-verified by inspection, not just diff:
- No TTS echo loop: `messaging-bot/app/matrix/callbacks.py:25-26` drops the bot's own events.
- `_MD_UNDERSCORE` holds up — `speakable("Fixed \`_add_missing_columns\` and __init__ in some_var_name")`
  round-trips verbatim, while `_ital_`, `**bold**` and `~~strike~~` still collapse.
- `parse_voice_args` rejects every malformed form (`stt`, `engine remote`, `voice a b`, `on off`);
  `upsert_room(tts_voice=None)` genuinely clears the override (`state.py:37-38`, plain `setattr`).
- `_local_lock = asyncio.Lock()` at module scope is safe on 3.12 (no loop binding since 3.10).
- `final_text` survives the trailing `flush_stream(final=True)` no-op, so the right text is spoken.

#### Bugs / gaps

1. **An answer with nothing speakable is reported as a TTS failure.** `synthesize()` returns `None`
   when `speakable(text)` is empty (`tts.py:157-159`), and `_maybe_speak` cannot tell that apart from
   a real backend failure — `reason` is `None`, so it posts
   *"⚠️ tts is on but silent: synthesis failed — see the logs. `!voice tts off` to stop trying."*
   (`session.py:329-338`). Verified live:
   - `"```\nx\n```"` → `""`
   - `"See https://example.com"` → `"See"` (survives, but a bare-URL answer → `""`)

   In a *coding* assistant, "the whole answer was one code block" is a routine outcome, not an error.
   The user is told a working feature is broken, and the one-shot `_tts_warned` slot is burned on it.

2. **The Dockerfile change was never build-tested and likely misses `libgomp1`.** `ctranslate2`
   (pulled by `faster-whisper`) links OpenMP; `python:3.12-slim` does not ship `libgomp1`, and the
   apt line only adds `curl ca-certificates git ripgrep ffmpeg espeak-ng` (`Dockerfile:6-7`). If it is
   indeed absent, `import faster_whisper` raises `ImportError: libgomp.so.1: cannot open shared object
   file` — caught at `stt.py:83-87` and degraded to a bare *"could not transcribe"*, because
   `unavailable_reason()` probes with `importlib.util.find_spec("faster_whisper")` (`stt.py:51`),
   which locates the module **without executing it** and therefore reports STT as available. That is
   exactly the silent-ish failure mode reviews 1 and 2 were about, reintroduced through the back door.
   I could not verify (no Docker daemon in this sandbox) — but the change is new, unbuilt, and the
   fix is one package name.

3. **`!voice voice <name>` is silently inert under `engine local`.** `_maybe_speak` passes
   `voice=room.tts_voice` into `synthesize()` (`session.py:328`), which routes to `_synthesize_local`
   (`tts.py:165-166`) — that function ignores `voice` entirely and uses `PIPER_MODEL_PATH`/espeak
   defaults. `!voice` still prints `**tts voice** \`de-DE-KatjaNeural\`` while nothing honours it.
   Cosmetic, but it is a state display that lies.

4. **No test covers either behaviour change in `bd26996`.** Still 34 tests, same as review 2. The two
   things this commit actually changed in logic — `_is_openai_compatible()` gating `STT_API_MODEL`,
   and the single-notice `too_long` path — are pure functions / easily faked and would have been
   cheap to pin. `_is_openai_compatible` in particular is now load-bearing in two places.

5. **README's command table is stale.** `README.md:90` still describes `!status` as
   *"mode / model / cwd / session / tokens / cost"*, but `cmd_status` has printed a `**voice**` line
   since `2b961b4` — and the issue's own wording ("a setting in the chat **in status**") makes that
   the one line worth documenting.

#### Style / conventions

Conventions are followed throughout; nothing structural to complain about.
- `from __future__ import annotations` in every new module ✅
- Room state mutated only via `upsert_room` / `audit`, never direct table writes ✅
- New settings are typed `Field`s in `app/config.py`, no ad-hoc `os.environ` ✅
- Voice deps imported lazily; base install verified importable without the extra ✅
- New `Room` columns are nullable-or-defaulted and backfilled by `_add_missing_columns`,
  covered by `tests/test_db_migration.py` (idempotent + row-preserving) ✅
- `app.voice` in `tool.setuptools.packages` ✅; systemd unit untouched ✅; `messaging-bot` untouched ✅
- HMAC check in `app/matrix/inbox.py` untouched; no secret is logged ✅
- Commit messages English, single line ✅

Nits (carried over, still acceptable):
- `_tts_warned` (`session.py:313`) is process-global mutable state with no eviction.
- `_MD_EMPHASIS` runs `.+?` with `DOTALL` over the *full* answer before truncation (`tts.py:35-48`).

#### Suggested fixes

1. Distinguish "nothing to say" from "synthesis broke". Cheapest: in `_maybe_speak`, compute
   `body = tts.speakable(text)` first and `return` silently when it is empty, then call
   `tts.synthesize()` — or give `synthesize()` a dedicated `EMPTY` sentinel. Add a regression test
   asserting a code-fence-only answer produces no warning.
2. `Dockerfile:7`: add `libgomp1` to the apt list (a few hundred KB) and actually run
   `dc build` + `python -c "import faster_whisper"` in the image once. Optionally make
   `stt.unavailable_reason(local=True)` do a real `import faster_whisper` in a `to_thread` (or cache
   the first failure) so a broken native dependency is reported as a reason instead of a generic
   "could not transcribe".
3. Either pass the room voice through to piper/espeak, or drop the `**tts voice**` line from `!voice`
   when `engine local` is active (e.g. append `(cloud only)`), so the displayed state is truthful.
4. Add two small tests: `_is_openai_compatible()` for the HF vs. Groq URLs, and
   `unavailable_reason(local=False)` returning the `STT_API_MODEL` hint for a Groq-shaped URL.
5. `README.md:90`: `mode / model / cwd / voice / session / tokens / cost`.

