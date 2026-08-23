# Review 3 — Issue #4 (STT & TTS support)

Branch: `auto/issue-4-feature-add-stt-and-tts-support` · `2b961b4` + `6eeb574` + `bd26996` vs `develop`
Tests: `uv run --extra dev pytest -q` → **34 passed** (0.75s). `speakable()` probed live against
6 real-world inputs. Working tree clean.

## Verdict — one sentence

NEEDS_FIX — every finding from reviews 1 and 2 is genuinely fixed and the feature now matches the
issue end to end, but `speakable()` returning an empty string for a code-only or URL-only answer is
reported to the room as *"tts is on but silent: synthesis failed"*, and the brand-new
`pip install '.[voice]'` in the Dockerfile pulls `ctranslate2` into `python:3.12-slim` without
`libgomp1` — neither the build nor the import was ever exercised.

## What was requested vs what was built

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

## Bugs / gaps

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

## Style / conventions

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

## Suggested fixes

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
