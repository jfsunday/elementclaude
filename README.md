# elementclaude

Claude Code, but the terminal is a Matrix room.

You DM your bot in Element. It streams its answer back into the room, asks for
permission before every tool call (✅/❌ reactions), supports mode switches
(`!mode plan`, `!auto`, …), session reset (`!clear`), session resume from any
project you've ever opened with `claude` (`!resume`), and exposes every one
of your local GSD slash commands (`!gsd:plan-phase`, …).

It is **not** itself a Matrix client. It rides on top of
[`messaging-bot`](../messaging-bot) over the shared `matrix-tools` Docker
network: messaging-bot delivers room events here via a webhook, and we send
replies back through its REST API.

## Quick start

```bash
# 0. Make sure messaging-bot is running and you know its API key.

# 1. Reserve a port (already done if you see 7075 in your `ports --list`)
ports --reserve elementclaude

# 2. Copy env and fill secrets
cp .env.example .env
# Edit:
#   ANTHROPIC_API_KEY      — your real key
#   MESSAGING_BOT_API_KEY  — messaging-bot's mb_… key
#   WEBHOOK_SECRET         — `openssl rand -hex 32`
#   INITIAL_ADMIN_USER     — your matrix user id, e.g. @js:matrix.org
#   WORKSPACE_ROOT         — leave blank to use ~/Projekte

# 3. Build & start
dc up --build -d

# 4. Watch the logs while it bootstraps the webhook rule
dc logs -f elementclaude
```

On the first start, elementclaude calls `messaging-bot`'s REST API to
create a webhook rule pointing back at itself. From that point on, every
Matrix message that hits messaging-bot is forwarded here.

## Onboard a room

1. From your admin Matrix account, message your messaging-bot in any room or
   in a DM. The bot needs to be in the room — invite it from Element, it
   auto-joins.
2. Type `!auth add` — your admin user is allowed to whitelist a room even
   before it's enabled.
3. Set a working directory: `!cwd /home/js/Projekte/my-project`
4. Start asking: `make me a smoke test for the api`

## Slash commands

Element already eats `/`, so elementclaude uses `!`.

| Command | What it does |
|---|---|
| `!help` | List all commands |
| `!status` | mode / model / cwd / session / token usage / cost |
| `!mode default\|acceptEdits\|plan\|auto` | Permission mode |
| `!model haiku\|sonnet\|opus` | Switch model |
| `!cwd <path>` | Working directory (must live under `$WORKSPACE_ROOT`) |
| `!clear` | New Claude session in this room |
| `!cancel` | Stop the current run |
| `!resume` | List sessions for this cwd (incl. ones started outside elementclaude) |
| `!resume <n>\|<session-id>` | Attach to a specific session |
| `!auth add\|remove\|list [room_id]` | Whitelist (admin) |
| `!admin add\|remove\|list <user_id>` | Manage admins (admin) |
| `!gsd:<cmd> [args]` | Forwarded as `/gsd:<cmd>` — all your GSD skills work |

## Permission modes

| Mode | Behaviour |
|---|---|
| `default` | Every tool call posts an approval message; ✅ allow once, ❌ deny, 🔁 allow-this-tool-until-`!clear` |
| `acceptEdits` | File edits auto-approved; Bash/others still ask |
| `plan` | Read-only planning; no edits, no shell |
| `auto` | Bypass all approvals (`bypassPermissions` in the SDK) — use carefully |

## Sessions

- Each room has at most one Claude session at a time. The session id is
  persisted to elementclaude's SQLite, so a container restart picks it up
  again.
- `!clear` drops the session id and the auto-allow set.
- `!resume` reads `~/.claude/projects/<encoded-cwd>/` and lets you jump back
  into anything you've ever worked on with the standalone `claude` CLI for
  the same cwd. That's why we mount the workspace at an identical absolute
  path on host and in the container (`$WORKSPACE_ROOT`).

## Architecture

```
Element ─▶ messaging-bot ─(webhook)─▶ elementclaude ─▶ Claude Agent SDK
                ▲                            │
                └────── REST (send/react)
```

- `messaging-bot` is left **untouched**.
- All inbound events arrive HMAC-signed at `/webhook/inbox`.
- All outbound (text + reactions) goes through messaging-bot's `/api/messages`
  and `/api/messages/react`.
- The Anthropic API key never leaves this container; messaging-bot does not
  know it exists.

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
└── reactions/          # m.reaction events → approval futures
```

## Operational notes

- `dc logs -f elementclaude` will show outbox failures as warnings — that's
  on purpose, we never let a failed reply crash the inbox.
- Inbox always returns 200 on a valid signature so messaging-bot doesn't
  retry; downstream failures are logged.
- `~/.claude` is mounted read-write so new sessions persist back to your
  host. Files written by the container will be owned by root unless you
  bake a `UID` into the image — that's the next polish item.
