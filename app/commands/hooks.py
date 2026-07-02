from __future__ import annotations

import shlex

from sqlalchemy import select

from app.db import session_scope
from app.matrix import outbox
from app.models import RoomHook
from app.rooms.state import audit


VALID_EVENTS = {
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "UserPromptSubmit",
    "Stop",
    "SubagentStop",
    "SubagentStart",
    "PreCompact",
    "Notification",
    "PermissionRequest",
}


async def _reply(room_id: str, text: str) -> None:
    await outbox.send_text(room_id, text, notice=True)


async def list_for_room(room_id: str) -> list[RoomHook]:
    async with session_scope() as s:
        rows = await s.scalars(
            select(RoomHook).where(RoomHook.room_id == room_id).order_by(RoomHook.created_at)
        )
        return list(rows.all())


async def cmd_hook(room_id: str, args: str, sender: str) -> None:
    arg = args.strip()
    if not arg:
        arg = "list"
    parts = shlex.split(arg) if arg else []
    sub = parts[0].lower() if parts else "list"

    if sub == "list":
        rows = await list_for_room(room_id)
        if not rows:
            events = " · ".join(sorted(VALID_EVENTS))
            await _reply(
                room_id,
                "no hooks. Usage:\n"
                "`!hook add <event> <shell-command>`\n"
                f"events: {events}\n"
                "e.g. `!hook add Stop \"notify-send 'claude fertig'\"`",
            )
            return
        lines = ["**room hooks:**"]
        for r in rows:
            state = "🟢" if r.enabled else "⏸"
            cmd_prev = r.command if len(r.command) <= 80 else r.command[:77] + "…"
            lines.append(f"{state} `{r.id[:8]}` **{r.event}** → `{cmd_prev}`")
        lines.append("\n_run `!clear` after changes so the agent picks them up_")
        await _reply(room_id, "\n".join(lines))
        return

    if sub == "add":
        if len(parts) < 3:
            await _reply(room_id, "usage: `!hook add <event> <shell-command>`")
            return
        event = parts[1]
        if event not in VALID_EVENTS:
            await _reply(room_id, f"unknown event `{event}` — see `!hook list`")
            return
        command = " ".join(parts[2:])
        async with session_scope() as s:
            row = RoomHook(room_id=room_id, event=event, command=command, created_by=sender)
            s.add(row)
            await s.commit()
            await s.refresh(row)
        await audit("hook_add", room_id=room_id, actor=sender, event=event, command=command[:200])
        await _reply(room_id, f"🟢 hook `{row.id[:8]}` on **{event}** added. Run `!clear` to activate.")
        return

    if sub in {"remove", "delete", "rm"}:
        if len(parts) < 2:
            await _reply(room_id, "usage: `!hook remove <id-prefix>`")
            return
        prefix = parts[1]
        async with session_scope() as s:
            rows = await s.scalars(
                select(RoomHook).where(RoomHook.room_id == room_id)
            )
            target = next((r for r in rows.all() if r.id == prefix or r.id.startswith(prefix)), None)
            if target is None:
                await _reply(room_id, f"no hook matching `{prefix}`")
                return
            await s.delete(target)
            await s.commit()
        await audit("hook_remove", room_id=room_id, actor=sender, hook_id=target.id)
        await _reply(room_id, f"🗑 removed hook `{target.id[:8]}` — run `!clear` to reload")
        return

    if sub in {"enable", "disable"}:
        if len(parts) < 2:
            await _reply(room_id, f"usage: `!hook {sub} <id-prefix>`")
            return
        prefix = parts[1]
        async with session_scope() as s:
            rows = await s.scalars(
                select(RoomHook).where(RoomHook.room_id == room_id)
            )
            target = next((r for r in rows.all() if r.id == prefix or r.id.startswith(prefix)), None)
            if target is None:
                await _reply(room_id, f"no hook matching `{prefix}`")
                return
            target.enabled = (sub == "enable")
            await s.commit()
        await audit(f"hook_{sub}", room_id=room_id, actor=sender, hook_id=target.id)
        await _reply(room_id, f"{'🟢' if sub == 'enable' else '⏸'} `{target.id[:8]}` — run `!clear` to reload")
        return

    await _reply(room_id, f"unknown subcommand: `{sub}`")
