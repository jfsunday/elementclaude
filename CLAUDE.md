# elementclaude

## Purpose
elementclaude turns a Matrix/Element DM into a full Claude Code terminal. `messaging-bot`
(a separate, untouched sibling project) delivers Matrix room events here via an HMAC-signed
webhook; elementclaude drives the Claude Agent SDK per room (mode, model, cwd, session id
persisted in SQLite), streams answers back through messaging-bot's REST API, asks for tool
permission via ✅/❌ reactions, and exposes an interactive PTY shell (`!run`) plus all local
GSD slash commands (`!gsd:...`).

## Tech stack
- Python 3.12, FastAPI + uvicorn, pydantic-settings, SQLAlchemy (async) + aiosqlite
- `claude-agent-sdk` (shells out to the `claude` CLI, Node 20 required in the container)
- httpx for the messaging-bot REST bridge, croniter for `!schedule`
- Dependency/venv management via `uv` (see `pyproject.toml` / `uv.lock`)
- Runs either directly on the host (`./run-host.sh`, installs a systemd **user** service) or
  in Docker (`docker-compose.yml`)

## Directory layout
```
app/
├── main.py             # FastAPI lifespan: db init, admin bootstrap, webhook rule
├── bootstrap.py        # idempotent rule creation in messaging-bot
├── config.py            # pydantic Settings, env-driven
├── db.py / models.py    # async SQLAlchemy engine/session + ORM models
├── matrix/              # inbox (HMAC webhook) + outbox (REST wrapper) + dispatcher
├── rooms/                # auth (admin/whitelist) + state (per-room mode/cwd/model/session)
├── commands/             # "!" command parser + builtins + aliases/link/batch/hooks/schedule
├── agent/                # ClaudeSDKClient per room: session.py, modes.py, permissions.py,
│                          # sessions_store.py, attachments.py
├── shell/                # interactive.py — PTY subprocess for !run
├── voice/                # stt.py (faster-whisper / HTTP API) + tts.py (edge-tts / piper)
└── reactions/            # tracker.py — m.reaction events → approval futures
```

## Conventions
- `from __future__ import annotations` at the top of every module.
- Async-first: DB access via `session_scope()` (see `app/db.py`), route handlers and room
  logic are `async def`.
- Per-room persistent state (mode, model, cwd, `claude_session_id`, enabled) lives in the
  `Room` SQLAlchemy model, mutated only through `app/rooms/state.py` helpers
  (`get_room`, `upsert_room`, `audit`) — never write to the table directly elsewhere.
- Permission modes are `default | acceptEdits | plan | auto`, mapped to the SDK's
  `default | acceptEdits | plan | bypassPermissions` in `app/agent/modes.py`
  (`MODE_TO_SDK` / `sdk_mode()`). Keep this mapping as the single source of truth for mode
  logic — do not hardcode SDK mode strings elsewhere.
- Chat commands are `!`-prefixed (Element eats `/`); GSD skills are forwarded as
  `!gsd:<cmd>` → `/gsd:<cmd>`. New commands go through `app/commands/router.py`.
- Settings are env-driven via `pydantic-settings` (`app/config.py`, backed by `.env`,
  see `.env.example`). Add new config as typed `Field`s there, not ad-hoc `os.environ` reads.
- Model columns added after the fact are backfilled by `_add_missing_columns()` in
  `app/db.py` (`create_all` never alters existing tables). New columns must therefore be
  nullable or carry a scalar default, otherwise the migration skips them with a warning.
- Voice deps (`edge-tts`, `faster-whisper`) live in the optional `[voice]` extra and are
  imported lazily inside `app/voice/*` — a base install must keep working without them.

## Running / testing
- Copy `.env.example` → `.env` and fill secrets (Anthropic key, messaging-bot URL/key,
  webhook secret, admin Matrix user id).
- Host mode (recommended, needed for `!run` to reach real host binaries):
  `./run-host.sh` (foreground) or `./run-host.sh --install` (systemd user service).
- Docker mode (sandboxed): `dc up --build -d` (project alias `dc` = `docker compose`).
- Optional extras are declared in `pyproject.toml` (`voice`, `dev`) and selected via the
  `EXTRAS` env var: `EXTRAS=voice ./run-host.sh` (also `EXTRAS="voice dev"`, and it gets
  baked into the unit on `--install`). `uv run` re-syncs the venv on every start, so a
  manual `uv pip install '.[voice]'` is pruned again — always name the extra via `EXTRAS`.
- Tests: `uv run --extra dev pytest` (`pytest` + `pytest-asyncio`, `asyncio_mode = "auto"`,
  `testpaths = ["tests"]`). `app.config` validates and `app.db` builds its engine at import
  time, so required env vars must be set in `tests/conftest.py` *before* anything from `app`
  is imported — real env vars outrank `.env`, which also isolates runs from local config.

## Out of scope / do NOT
- **Never** start/stop/restart/enable/disable the `elementclaude` systemd user service, and
  never edit the unit file directly — the user restarts it manually after pulling new code.
  Only `run-host.sh` may generate/manage the unit file, and only when the user runs it.
- Do not modify `messaging-bot` — it's a separate, untouched sibling project; only interact
  with it through its REST API / webhook contract.
- Do not weaken the HMAC webhook signature check in `app/matrix/inbox.py`, and do not log or
  persist the Anthropic API key — it must never leave this process.
