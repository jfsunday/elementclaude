# elementclaude

Claude Code, but the terminal is a Matrix room.

You DM your bot in Element. It streams its answer back into the room, asks for
permission before every tool call (✅/❌ reactions), supports mode switches
(`!mode plan`, `!auto`, …), session reset (`!clear`), session resume from any
project you've ever opened with `claude` (`!resume`), exposes every one of
your local GSD slash commands (`!gsd:plan-phase`, …), and gives you a full
interactive shell in the room (`!run sudo pacman -Syu` → next message becomes
stdin).

It is **not** itself a Matrix client. It rides on top of
[`messaging-bot`](../messaging-bot) — messaging-bot delivers room events here
via a webhook, and we send replies back through its REST API.

## How it runs — **host mode** (recommended)

The bot runs on your host directly so `!run` reaches your real PATH (`yay`,
`pacman`, `sudo`, …) and Claude can touch any directory you can. messaging-bot
keeps running in Docker; the two talk over the docker bridge.

```bash
# 0. Make sure messaging-bot is running, you know its API key, and you have uv installed.

# 1. Reserve the port if you haven't
ports --reserve elementclaude   # → 7075

# 2. Copy env and fill secrets
cp .env.example .env
# Edit:
#   ANTHROPIC_API_KEY      — your real key
#   ANTHROPIC_BASE_URL     — optional proxy / alt endpoint
#   MESSAGING_BOT_URL=http://localhost:9985
#   MESSAGING_BOT_API_KEY  — messaging-bot's key
#   ELEMENTCLAUDE_INTERNAL_URL=http://172.16.0.1:7075   # docker bridge IP (Linux)
#   WEBHOOK_SECRET         — `openssl rand -hex 32`
#   INITIAL_ADMIN_USER     — your matrix user id
#   WORKSPACE_ROOT         — leave commented = no restriction
#   DATA_DIR=./data

# 3. One-time foreground run to see the bootstrap
./run-host.sh

# 4. Once that works, install as a systemd user service
./run-host.sh --install
./run-host.sh --logs        # tail journal
./run-host.sh --stop        # stop
./run-host.sh --uninstall   # remove
```

The first start (or any URL change) rewrites the webhook rule in
messaging-bot to point at `ELEMENTCLAUDE_INTERNAL_URL`.

> **Tip:** find the docker bridge IP with `ip -4 addr show docker0`. On
> standard Docker it's `172.17.0.1`; with custom bridges it may differ
> (mine is `172.16.0.1`).

## How it ran before — **docker mode** (still possible)

`docker-compose.yml` is still there. The catch: from inside the container,
`!run` only sees the *container's* binaries (no `yay`, no host `sudo`), and
`/etc`, `/var`, `/home/foo` are container paths. Use this mode only if you
want elementclaude sandboxed.

```bash
dc up --build -d
```

In this mode set `MESSAGING_BOT_URL=http://messaging-bot:8000` and
`ELEMENTCLAUDE_INTERNAL_URL=http://elementclaude:8000`.

## Onboard a room

1. Invite the bot to a Matrix room from Element; it auto-joins.
2. From your admin Matrix account, type `!auth add` — admin users can
   whitelist any room they're in.
3. `!cwd /home/js/Projekte/my-project`  *(or anywhere — see WORKSPACE_ROOT)*
4. Ask: `make me a smoke test for the api`

## Slash commands

Element already eats `/`, so elementclaude uses `!`.

### Session

| Command | What it does |
|---|---|
| `!help` | List all commands |
| `!status` | mode / model / cwd / session / tokens / cost |
| `!mode default\|acceptEdits\|plan\|auto` | Permission mode |
| `!model haiku\|sonnet\|opus` | Switch model |
| `!cwd <path>` | Working directory (must live under `WORKSPACE_ROOT` if set) |
| `!clear` | New Claude session in this room |
| `!cancel` | Stop the current run |
| `!resume` | List sessions for this cwd (also ones from your own `claude` CLI) |
| `!resume <n>\|<session-id>` | Attach to a specific session |
| `!voice …` | Speech in/out — see below |

### Voice (STT + TTS)

Off by default. `!voice on` makes the room listen and talk:
Element voice messages are transcribed and routed exactly like typed text, and
the final answer of each run comes back as an `m.audio` message.

| Command | What it does |
|---|---|
| `!voice` | Show the current voice state |
| `!voice on\|off` | Toggle STT **and** TTS |
| `!voice stt on\|off` | Only inbound transcription |
| `!voice tts on\|off` | Only spoken answers |
| `!voice engine local\|cloud` | Where speech is processed |
| `!voice voice <name>\|default` | Override the edge-tts voice for this room |

| Engine | STT | TTS |
|---|---|---|
| `cloud` | `STT_API_URL` (default: HuggingFace `whisper-large-v3`, free tier) | `edge-tts` — Microsoft Edge read-aloud, **no API key** |
| `local` | `faster-whisper`, `STT_MODEL=medium`, fully offline | `piper` if `PIPER_MODEL_PATH` is set, else `espeak-ng` |

Everything is free. There is no keyless free Whisper API though, so `engine cloud`
without an `STT_API_TOKEN` falls back to local transcription and says so in the room.

Voice needs the optional `voice` extra. `uv run` re-syncs the venv on every start,
so pass it via `EXTRAS` instead of installing by hand:

```bash
EXTRAS=voice ./run-host.sh             # foreground
EXTRAS=voice ./run-host.sh --install   # bakes it into the systemd unit
```

`faster-whisper` downloads the `medium` model (~1.5 GB) on the first local
transcription. Local TTS wants `espeak-ng` (or `piper` + `PIPER_MODEL_PATH`).

### Interactive shell (real TTY)

| Command | What it does |
|---|---|
| `!run <cmd>` | Spawn a PTY subprocess; the next plain messages become stdin |
| `!end` | Kill the running shell |
| `!sig int\|term\|kill\|hup\|quit` | Send a signal |
| `!eof` | Send Ctrl-D |

So `sudo pacman -Syu` → password prompt comes back as a chat message →
type your password as the next message → done. `yay -S foo` → `[Y/n]`
comes back → answer `y`.

### Admin

| Command | What it does |
|---|---|
| `!auth add\|remove\|list [room_id]` | Manage whitelisted rooms |
| `!admin add\|remove\|list <user_id>` | Manage admin Matrix users |

### GSD

| Command | What it does |
|---|---|
| `!gsd:<cmd> [args]` | Forwarded as `/gsd:<cmd>` — all GSD skills work |

## Permission modes

| Mode | Behaviour |
|---|---|
| `default` | Every tool call posts an approval message; ✅ allow once, ❌ deny, 🔁 allow-this-tool-until-`!clear` |
| `acceptEdits` | File edits auto-approved; Bash/others still ask |
| `plan` | Read-only planning; no edits, no shell |
| `auto` | Bypass all approvals — use carefully |

## Sessions

- Each room has at most one Claude session at a time. The session id is
  persisted to `data/elementclaude.sqlite3`, so a restart picks it up again.
- `!clear` drops the session id and the auto-allow set.
- `!resume` reads `~/.claude/projects/<encoded-cwd>/` and lets you jump back
  into anything you've ever worked on with the standalone `claude` CLI for
  that same cwd. In host mode this just works; in docker mode the workspace
  path on host and in the container must match (see `docker-compose.yml`).

## Architecture

```
Element ─▶ messaging-bot ─(webhook)─▶ elementclaude ─▶ Claude Agent SDK
                ▲                            │
                └────── REST (send/react) ───┘
```

- `messaging-bot` is **untouched**.
- Inbound events arrive HMAC-signed at `/webhook/inbox`.
- Outbound goes through messaging-bot's `/api/messages` and `…/react`.
- The Anthropic API key never leaves this process.

## Layout

```
app/
├── main.py             # FastAPI lifespan: db init, admin bootstrap, webhook rule
├── bootstrap.py        # idempotent rule creation in messaging-bot
├── config.py / db.py / models.py
├── matrix/             # inbox (HMAC) + outbox (REST wrapper) + dispatcher
├── rooms/              # auth (admin list) + state (per-room mode/cwd/model)
├── commands/           # ! parser + builtins + gsd: forwarding
├── agent/              # ClaudeSDKClient per room, streaming, sessions store, modes
├── shell/              # interactive PTY subprocess for !run
├── voice/              # stt (whisper) + tts (edge-tts / piper / espeak)
└── reactions/          # m.reaction events → approval futures
```
