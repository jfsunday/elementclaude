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

**Interactive shell (TTY — sudo, yay, vim, prompts all work):**
`!run <cmd>` — start a real PTY shell; the next messages become stdin
`!enter [n]` — send Enter (n times, default 1) — for "press enter to continue"
`!end` — kill the running shell
`!sig int|term|kill` — send SIGINT / SIGTERM / SIGKILL
`!eof` — send Ctrl-D to the shell (closes stdin)

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
    from app.agent.permissions import _auto_allowed
    from app.agent.session import get_session_snapshot

    room = await get_room(room_id)
    if room is None:
        await _reply(room_id, "no room state yet")
        return
    sess = get_session_snapshot(room_id)
    auto = sorted(_auto_allowed.get(room_id) or set())
    lines = [
        f"**room** `{room.room_id}`",
        f"**enabled** {room.enabled}",
        f"**mode** {room.mode}",
        f"**model** {room.model}",
        f"**cwd** {room.cwd or '(unset)'}",
        f"**claude session** {room.claude_session_id or '(none)'}",
        f"**last activity** {room.last_activity.isoformat(timespec='seconds')}",
    ]
    if sess is not None:
        lines += [
            "",
            f"**turns** {sess.turns}",
            f"**tokens in/out** {sess.total_input_tokens:,} / {sess.total_output_tokens:,}",
            f"**cost** ${sess.total_cost_usd:.4f}",
            f"**connected** {sess.client is not None}",
            f"**running** {sess.current_task is not None and not sess.current_task.done()}",
        ]
    if auto:
        lines.append(f"**auto-allowed tools** {', '.join(auto)}")
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


def _cwd_is_safe(p: Path) -> tuple[bool, str]:
    """Returns (ok, reason_if_not_ok)."""
    try:
        resolved = p.resolve()
    except Exception as exc:
        return False, f"cannot resolve path: {exc}"
    if not resolved.is_dir():
        return False, f"`{resolved}` is not an existing directory"
    if settings.workspace_root is not None:
        try:
            resolved.relative_to(settings.workspace_root.resolve())
        except ValueError:
            return False, f"`{resolved}` is outside WORKSPACE_ROOT (`{settings.workspace_root}`)"
    return True, ""


async def cmd_cwd(room_id: str, args: str, sender: str) -> None:
    arg = args.strip()
    if not arg:
        room = await get_room(room_id)
        await _reply(room_id, f"cwd: `{(room and room.cwd) or '(unset)'}`")
        return
    p = Path(arg).expanduser()
    ok, reason = _cwd_is_safe(p)
    if not ok:
        await _reply(room_id, f"cwd rejected: {reason}")
        return
    await upsert_room(room_id, cwd=str(p.resolve()))
    await audit("cwd_change", room_id=room_id, actor=sender, cwd=str(p.resolve()))
    await _reply(room_id, f"cwd → `{p.resolve()}`")


async def cmd_clear(room_id: str, _args: str, sender: str) -> None:
    from app.agent.session import clear_room

    await clear_room(room_id)
    await audit("session_clear", room_id=room_id, actor=sender)
    await _reply(room_id, "🧹 session cleared")


async def cmd_cancel(room_id: str, _args: str, _sender: str) -> None:
    from app.agent.session import cancel_room

    cancelled = await cancel_room(room_id)
    await _reply(room_id, "🛑 cancelled" if cancelled else "no active run to cancel")


import signal


async def cmd_run(room_id: str, args: str, sender: str) -> None:
    from app.shell.interactive import InteractiveShell, get_active, set_active

    if get_active(room_id) is not None:
        await _reply(room_id, "a shell is already running — use `!end` first")
        return

    cmd = args.strip()
    if not cmd:
        await _reply(room_id, "usage: `!run <command>` — e.g. `!run sudo apt update`")
        return

    room = await get_room(room_id)
    if room is None or not room.cwd:
        await _reply(room_id, "set `!cwd <path>` first")
        return

    async def on_output(text: str) -> None:
        # Use a code block so whitespace and prompts render verbatim in Element.
        await _reply(room_id, f"```\n{text}\n```")

    async def on_exit(code: int) -> None:
        set_active(room_id, None)
        await audit("shell_exit", room_id=room_id, actor=sender, exit_code=code, command=cmd[:200])
        await _reply(room_id, f"🏁 shell exited (code `{code}`)")

    sh = InteractiveShell(
        room_id=room_id, cwd=room.cwd, command=cmd, on_output=on_output, on_exit=on_exit
    )
    set_active(room_id, sh)
    await audit("shell_start", room_id=room_id, actor=sender, command=cmd[:200])
    await _reply(room_id, f"▶️ running in `{room.cwd}` — send messages to type into the shell, `!end` to stop")
    try:
        await sh.start()
    except Exception as exc:
        set_active(room_id, None)
        await _reply(room_id, f"❌ could not start shell: {exc}")


async def cmd_end(room_id: str, _args: str, sender: str) -> None:
    from app.shell.interactive import get_active, set_active

    sh = get_active(room_id)
    if sh is None:
        await _reply(room_id, "no shell running")
        return
    await audit("shell_end", room_id=room_id, actor=sender)
    await sh.kill()
    set_active(room_id, None)


_SIG_MAP = {
    "int": signal.SIGINT,
    "term": signal.SIGTERM,
    "kill": signal.SIGKILL,
    "hup": signal.SIGHUP,
    "quit": signal.SIGQUIT,
}


async def cmd_sig(room_id: str, args: str, sender: str) -> None:
    from app.shell.interactive import get_active

    sh = get_active(room_id)
    if sh is None:
        await _reply(room_id, "no shell running")
        return
    name = args.strip().lower().removeprefix("sig")
    sig = _SIG_MAP.get(name)
    if sig is None:
        await _reply(room_id, f"signal must be one of: {', '.join(_SIG_MAP)}")
        return
    ok = sh.send_signal(sig)
    await audit("shell_signal", room_id=room_id, actor=sender, signal=name)
    await _reply(room_id, f"{'📡' if ok else '⚠️'} sent SIG{name.upper()}")


async def cmd_eof(room_id: str, _args: str, _sender: str) -> None:
    from app.shell.interactive import get_active

    sh = get_active(room_id)
    if sh is None:
        await _reply(room_id, "no shell running")
        return
    sh.write("\x04")  # Ctrl-D
    await _reply(room_id, "📡 sent EOF")


async def cmd_enter(room_id: str, args: str, _sender: str) -> None:
    from app.shell.interactive import get_active

    sh = get_active(room_id)
    if sh is None:
        await _reply(room_id, "no shell running")
        return
    # Repeat count: `!enter 3` sends three newlines
    n = 1
    arg = args.strip()
    if arg.isdigit():
        n = max(1, min(int(arg), 50))
    sh.write("\n" * n)


async def cmd_resume(room_id: str, args: str, sender: str) -> None:
    from datetime import datetime, timezone

    from app.agent.session import resume_room
    from app.agent.sessions_store import list_sessions_for_cwd, session_exists

    room = await get_room(room_id)
    if room is None or not room.cwd:
        await _reply(room_id, "set `!cwd <path>` first")
        return

    arg = args.strip()
    sessions = list_sessions_for_cwd(room.cwd)

    if not arg:
        if not sessions:
            await _reply(room_id, f"no sessions found for `{room.cwd}`")
            return
        lines = [f"**sessions in** `{room.cwd}`", ""]
        for i, s in enumerate(sessions, start=1):
            ts = datetime.fromtimestamp(s.mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            preview = (s.first_user_text or "(no user text)").splitlines()[0][:90]
            lines.append(f"{i}. `{s.session_id[:8]}…` · {ts} · {preview}")
        lines.append("")
        lines.append("`!resume <n>` or `!resume <session-id>` to attach")
        await _reply(room_id, "\n".join(lines))
        return

    # Index?
    target_id: str | None = None
    if arg.isdigit():
        idx = int(arg)
        if 1 <= idx <= len(sessions):
            target_id = sessions[idx - 1].session_id
        else:
            await _reply(room_id, f"out of range — there are {len(sessions)} sessions")
            return
    else:
        # treat as session id (full or prefix)
        for s in sessions:
            if s.session_id == arg or s.session_id.startswith(arg):
                target_id = s.session_id
                break
        if target_id is None and session_exists(room.cwd, arg):
            target_id = arg

    if target_id is None:
        await _reply(room_id, f"no session matching `{arg}` in `{room.cwd}`")
        return

    await resume_room(room_id, target_id)
    await audit("session_resume", room_id=room_id, actor=sender, session_id=target_id)
    await _reply(room_id, f"⏪ resumed session `{target_id[:8]}…`")


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
    "run": cmd_run,
    "end": cmd_end,
    "sig": cmd_sig,
    "eof": cmd_eof,
    "enter": cmd_enter,
}
