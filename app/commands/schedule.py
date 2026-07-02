from __future__ import annotations

import shlex

from app.matrix import outbox
from app.rooms.state import audit
from app.scheduler import (
    create as schedule_create,
    delete as schedule_delete,
    find as schedule_find,
    list_for_room as schedule_list,
    set_enabled as schedule_set_enabled,
    validate_cron,
)


async def _reply(room_id: str, text: str) -> None:
    await outbox.send_text(room_id, text, notice=True)


async def cmd_schedule(room_id: str, args: str, sender: str) -> None:
    arg = args.strip()
    if not arg:
        arg = "list"
    parts = shlex.split(arg) if arg else []

    sub = parts[0].lower() if parts else "list"

    if sub == "list":
        rows = await schedule_list(room_id)
        if not rows:
            await _reply(
                room_id,
                "no scheduled tasks. Usage:\n"
                "`!schedule <cron> <command>` — e.g. `!schedule \"*/15 * * * *\" !status`\n"
                "`!schedule remove <id-prefix>` · `!schedule enable|disable <id-prefix>`",
            )
            return
        lines = ["**scheduled tasks:**"]
        for r in rows:
            state = "🟢" if r.enabled else "⏸"
            next_str = r.next_run_at.strftime("%Y-%m-%d %H:%M UTC") if r.next_run_at else "-"
            cmd_prev = r.command if len(r.command) <= 80 else r.command[:77] + "…"
            lines.append(f"{state} `{r.id[:8]}` `{r.cron_expr}` → `{cmd_prev}`  _(next: {next_str})_")
        await _reply(room_id, "\n".join(lines))
        return

    if sub in {"remove", "delete", "rm"}:
        if len(parts) < 2:
            await _reply(room_id, "usage: `!schedule remove <id-prefix>`")
            return
        task = await schedule_find(room_id, parts[1])
        if task is None:
            await _reply(room_id, f"no scheduled task matching `{parts[1]}`")
            return
        await schedule_delete(task)
        await audit("schedule_delete", room_id=room_id, actor=sender, task_id=task.id)
        await _reply(room_id, f"🗑 removed `{task.id[:8]}`")
        return

    if sub in {"enable", "disable"}:
        if len(parts) < 2:
            await _reply(room_id, f"usage: `!schedule {sub} <id-prefix>`")
            return
        task = await schedule_find(room_id, parts[1])
        if task is None:
            await _reply(room_id, f"no scheduled task matching `{parts[1]}`")
            return
        await schedule_set_enabled(task, sub == "enable")
        await audit(f"schedule_{sub}", room_id=room_id, actor=sender, task_id=task.id)
        await _reply(room_id, f"{'🟢' if sub == 'enable' else '⏸'} `{task.id[:8]}`")
        return

    # Otherwise assume: <cron> <command…>
    #  - Cron expr always has 5 whitespace-separated fields.
    tokens = arg.split()
    if len(tokens) < 6:
        await _reply(
            room_id,
            "usage: `!schedule <cron-5-fields> <command>` — e.g. `!schedule 0 9 * * * !status`",
        )
        return
    cron_expr = " ".join(tokens[:5])
    command = " ".join(tokens[5:])
    if not validate_cron(cron_expr):
        await _reply(room_id, f"invalid cron expression: `{cron_expr}`")
        return
    task = await schedule_create(room_id, cron_expr, command, sender)
    await audit(
        "schedule_create",
        room_id=room_id,
        actor=sender,
        task_id=task.id,
        cron=cron_expr,
        command=command[:200],
    )
    next_str = task.next_run_at.strftime("%Y-%m-%d %H:%M UTC") if task.next_run_at else "-"
    await _reply(
        room_id,
        f"🟢 scheduled `{task.id[:8]}` — `{cron_expr}` → `{command}`\nnext fire: {next_str}",
    )
