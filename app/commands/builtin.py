from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.config import settings
from app.matrix import outbox
from app.rooms.auth import add_admin, is_admin, list_admins, remove_admin
from app.rooms.state import audit, get_room, list_enabled_rooms, upsert_room

logger = logging.getLogger(__name__)


VALID_MODES = {"default", "acceptEdits", "plan", "auto"}

MODEL_ALIASES = {
    "opus": "claude-opus-4-7",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}


HELP_TEXT = """**elementclaude — slash commands** (use `!` because `/` is Element's)

`!help` — this help
`!status` — show current mode, model, cwd, session
`!clear` — start a new session in this room
`!cancel` — stop the current run
`!mode default|acceptEdits|plan|auto` — permission mode
`!model haiku|sonnet|opus` — switch model
`!cwd <path>` — working directory (must be under `{root}`)
`!resume [n|<session-id>]` — list / resume sessions in current cwd

**Admin only:**
`!auth add <room_id>` / `!auth remove <room_id>` / `!auth list`
`!admin add <user_id>` / `!admin remove <user_id>` / `!admin list`

**GSD:**
`!gsd:<command> [args]` — forwarded to Claude as `/gsd:<command>`
"""


async def _reply(room_id: str, text: str, *, html: str | None = None) -> None:
    if html:
        await outbox.send_markdown(room_id, text, html, notice=True)
    else:
        await outbox.send_text(room_id, text, notice=True)


# -------- handlers --------


async def cmd_help(room_id: str, _args: str, _sender: str) -> None:
    text = HELP_TEXT.format(root=str(settings.workspace_root))
    await _reply(room_id, text)


async def cmd_status(room_id: str, _args: str, _sender: str) -> None:
    room = await get_room(room_id)
    if room is None:
        await _reply(room_id, "no room state yet")
        return
    lines = [
        f"**room** `{room.room_id}`",
        f"**enabled** {room.enabled}",
        f"**mode** {room.mode}",
        f"**model** {room.model}",
        f"**cwd** {room.cwd or '(unset)'}",
        f"**session** {room.claude_session_id or '(none)'}",
        f"**last activity** {room.last_activity.isoformat(timespec='seconds')}",
    ]
    await _reply(room_id, "\n".join(lines))


async def cmd_mode(room_id: str, args: str, sender: str) -> None:
    arg = args.strip()
    if arg not in VALID_MODES:
        await _reply(room_id, f"mode must be one of: {', '.join(sorted(VALID_MODES))}")
        return
    await upsert_room(room_id, mode=arg)
    await audit("mode_change", room_id=room_id, actor=sender, mode=arg)
    await _reply(room_id, f"mode → `{arg}`")


async def cmd_model(room_id: str, args: str, sender: str) -> None:
    arg = args.strip()
    target = MODEL_ALIASES.get(arg, arg)
    if target not in MODEL_ALIASES.values():
        await _reply(
            room_id,
            f"model must be one of: {', '.join(MODEL_ALIASES)} (or a full model id)",
        )
        return
    await upsert_room(room_id, model=target)
    await audit("model_change", room_id=room_id, actor=sender, model=target)
    await _reply(room_id, f"model → `{target}`")


def _cwd_is_safe(p: Path) -> bool:
    try:
        resolved = p.resolve()
    except Exception:
        return False
    try:
        resolved.relative_to(settings.workspace_root.resolve())
    except ValueError:
        return False
    return resolved.is_dir()


async def cmd_cwd(room_id: str, args: str, sender: str) -> None:
    arg = args.strip()
    if not arg:
        room = await get_room(room_id)
        await _reply(room_id, f"cwd: `{(room and room.cwd) or '(unset)'}`")
        return
    p = Path(arg)
    if not _cwd_is_safe(p):
        await _reply(
            room_id,
            f"cwd must be an existing directory under `{settings.workspace_root}` — got `{arg}`",
        )
        return
    await upsert_room(room_id, cwd=str(p.resolve()))
    await audit("cwd_change", room_id=room_id, actor=sender, cwd=str(p.resolve()))
    await _reply(room_id, f"cwd → `{p.resolve()}`")


async def cmd_clear(room_id: str, _args: str, sender: str) -> None:
    # Real session reset lands in Phase 04 (kills agent session, drops claude_session_id).
    await upsert_room(room_id, claude_session_id=None)
    await audit("session_clear", room_id=room_id, actor=sender)
    await _reply(room_id, "🧹 session cleared")


async def cmd_cancel(room_id: str, _args: str, _sender: str) -> None:
    # Real cancel lands in Phase 04 (asyncio.Task.cancel()).
    await _reply(room_id, "no active run to cancel (phase 04)")


async def cmd_resume(room_id: str, _args: str, _sender: str) -> None:
    # Phase 04.5
    await _reply(room_id, "`!resume` lands with the agent integration (phase 04.5)")


# -------- !auth --------


async def cmd_auth(room_id: str, args: str, sender: str) -> None:
    if not await is_admin(sender):
        await _reply(room_id, "only admins can manage room whitelist")
        return

    parts = args.strip().split(maxsplit=1)
    if not parts:
        await _reply(room_id, "usage: `!auth add|remove|list [room_id]`")
        return
    sub = parts[0].lower()
    target = parts[1].strip() if len(parts) > 1 else room_id  # default to current room

    if sub == "list":
        rooms = await list_enabled_rooms()
        if not rooms:
            await _reply(room_id, "no whitelisted rooms")
            return
        await _reply(room_id, "whitelisted rooms:\n" + "\n".join(f"• `{r.room_id}`" for r in rooms))
        return

    if sub == "add":
        await upsert_room(target, enabled=True)
        await audit("auth_room_add", room_id=target, actor=sender)
        await _reply(room_id, f"✅ room `{target}` whitelisted")
        return

    if sub == "remove":
        await upsert_room(target, enabled=False)
        await audit("auth_room_remove", room_id=target, actor=sender)
        await _reply(room_id, f"🚫 room `{target}` removed from whitelist")
        return

    await _reply(room_id, f"unknown subcommand: `{sub}` (add | remove | list)")


async def cmd_admin(room_id: str, args: str, sender: str) -> None:
    if not await is_admin(sender):
        await _reply(room_id, "only admins can manage admins")
        return
    parts = args.strip().split(maxsplit=1)
    if not parts:
        await _reply(room_id, "usage: `!admin add|remove|list [user_id]`")
        return
    sub = parts[0].lower()

    if sub == "list":
        admins = await list_admins()
        await _reply(room_id, "admins:\n" + "\n".join(f"• `{a}`" for a in admins))
        return

    if len(parts) < 2:
        await _reply(room_id, "usage: `!admin add|remove <user_id>`")
        return
    target = parts[1].strip()

    if sub == "add":
        added = await add_admin(target)
        await audit("admin_add", room_id=room_id, actor=sender, target=target)
        await _reply(room_id, f"{'✅' if added else 'ℹ️'} `{target}` is admin")
        return
    if sub == "remove":
        removed = await remove_admin(target)
        await audit("admin_remove", room_id=room_id, actor=sender, target=target)
        await _reply(
            room_id,
            f"{'🚫' if removed else 'ℹ️'} `{target}` removed"
            if removed
            else f"could not remove `{target}` (not admin, or is the initial admin)",
        )
        return

    await _reply(room_id, f"unknown subcommand: `{sub}`")


# -------- registry --------


Handler = Any  # callable(room_id, args_str, sender_id) -> Awaitable[None]

BUILTINS: dict[str, Handler] = {
    "help": cmd_help,
    "status": cmd_status,
    "mode": cmd_mode,
    "model": cmd_model,
    "cwd": cmd_cwd,
    "clear": cmd_clear,
    "cancel": cmd_cancel,
    "resume": cmd_resume,
    "auth": cmd_auth,
    "admin": cmd_admin,
}
