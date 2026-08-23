# Review 2 — Issue #4 (STT & TTS support)

Branch: `auto/issue-4-feature-add-stt-and-tts-support` · `2b961b4` + `6eeb574` vs `develop`
Tests: `uv run --extra dev pytest -q` → **34 passed** (0.71s), +2 regression tests since review 1.
Import smoke test of `app.main`, `app.voice.*`, `app.matrix.dispatcher`, `app.agent.session`,
`app.commands.builtin` on a base install (no `voice` extra) → OK.

## Verdict — one sentence

NEEDS_FIX — every substantive finding from review 1 is genuinely fixed and verified, but this
commit adds voice *system* packages to the Dockerfile plus a comment claiming "docker mode is
cloud-voice by default" while never installing the `[voice]` extra, so cloud TTS is dead in the
documented `dc up` deployment, and `.env.example` hands out an install command the README
explicitly says does not work.

## What was requested vs what was built

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

## Bugs / gaps

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

## Style / conventions

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

## Suggested fixes

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
