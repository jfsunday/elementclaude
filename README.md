# elementclaude

Claude Code, but the terminal is a Matrix room.

You talk to it in Element. It streams its answer back into the room, asks for
permission before running tools (✅/❌ reactions), supports mode switches
(`!mode plan`, `!auto`, …), session reset (`!clear`), and all of your local
GSD slash-commands (`!gsd:plan-phase`, `!gsd:execute-phase`, …).

It is **not** itself a Matrix client. It rides on top of
[`messaging-bot`](../messaging-bot) over the shared `matrix-tools` Docker
network: messaging-bot delivers room events here via a webhook, and we send
replies back through its REST API.

## Quick start

```bash
# 1. Make sure messaging-bot is running and you have its API key.

# 2. Reserve a port
ports --reserve elementclaude   # → e.g. 7075

# 3. Copy env, fill secrets
cp .env.example .env
# edit: ANTHROPIC_API_KEY, MESSAGING_BOT_API_KEY, WEBHOOK_SECRET, INITIAL_ADMIN_USER

# 4. Build & start
dc up --build
```

## Slash-commands (use `!` because `/` is taken by Element)

| Command | What it does |
|---|---|
| `!help` | Show available commands |
| `!clear` | New session in this room |
| `!mode default\|acceptEdits\|plan\|auto` | Permission mode |
| `!cancel` | Stop current run |
| `!status` | mode / model / cwd / session / pending |
| `!cwd <path>` | Set working directory (must be under `/host/projekte`) |
| `!model haiku\|sonnet\|opus` | Switch model |
| `!auth add <room_id>` | Whitelist a room (admin only) |
| `!auth remove <room_id>` | Un-whitelist a room |
| `!auth list` | List whitelisted rooms |
| `!gsd:<cmd>` | Forwarded to Claude as `/gsd:<cmd>` — all GSD skills work |

## Architecture

```
Element ─▶ messaging-bot ─(webhook)─▶ elementclaude ─▶ Claude Agent SDK
                ▲                            │
                └────── REST (send/edit/react)
```
