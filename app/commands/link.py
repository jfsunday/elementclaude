from __future__ import annotations

import logging
import shlex

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.commands.aliases import (
    add_global_alias,
    add_room_alias,
    build_alias_maps,
    get_alias,
    list_aliases,
    list_global,
    remove_global_alias,
    remove_room_alias,
)
from app.db import session_scope
from app.matrix import outbox
from app.models import CommandAlias
from app.rooms.auth import is_admin
from app.rooms.state import audit

logger = logging.getLogger(__name__)


async def _reply(room_id: str, text: str) -> None:
    await outbox.send_text(room_id, text, notice=True)


def _extract_flags(tokens: list[str]) -> tuple[list[str], bool, bool, str]:
    """Consume --exact, --global, --desc "..." from tokens (in any position).
    Returns (remaining_tokens, exact, is_global, description).
    """
    exact = False
    is_global = False
    description = ""
    out: list[str] = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t == "--exact":
            exact = True
        elif t == "--global":
            is_global = True
        elif t == "--desc":
            if i + 1 < len(tokens):
                description = tokens[i + 1]
                i += 1
            else:
                description = ""
        else:
            out.append(t)
        i += 1
    return out, exact, is_global, description


async def _cmd_list(room_id: str, args: list[str]) -> None:
    only_global = "--global" in args
    only_room = "--room" in args
    if only_global:
        rows = await list_global()
        scope_label = "global"
    elif only_room:
        rows = await list_aliases(room_id, include_global=False)
        scope_label = "this room"
    else:
        rows = await list_aliases(room_id, include_global=True)
        scope_label = "this room + global"

    if not rows:
        await _reply(
            room_id,
            f"no aliases ({scope_label}). Usage:\n"
            "`!link <name> <target> [--desc \"...\"] [--exact] [--global]`\n"
            "`!link list [--global|--room]` · `!link remove <name> [--global]`",
        )
        return

    lines = [f"**aliases ({scope_label}):**"]
    for r in rows:
        marks = []
        if r.exact:
            marks.append("exact")
        if r.scope == "global":
            marks.append("global")
        mark_str = f" _({', '.join(marks)})_" if marks else ""
        desc_part = f" — {r.description}" if r.description else ""
        lines.append(f"`!{r.name}` → `{r.target}`{mark_str}{desc_part}")
    await _reply(room_id, "\n".join(lines))


async def _cmd_show(room_id: str, name: str) -> None:
    alias = await get_alias(room_id, name)
    if alias is None:
        await _reply(room_id, f"no alias `{name}` visible in this room")
        return
    marks = []
    if alias.exact:
        marks.append("exact")
    if alias.scope == "global":
        marks.append("global")
    mark_str = f" _({', '.join(marks)})_" if marks else ""
    desc_part = f"\ndescription: {alias.description}" if alias.description else ""
    await _reply(
        room_id,
        f"`!{alias.name}` → `{alias.target}`{mark_str}\n"
        f"scope: {alias.scope}\n"
        f"created_by: {alias.created_by}{desc_part}",
    )


async def _cmd_remove(room_id: str, args: list[str], sender: str) -> None:
    is_global = "--global" in args
    positional = [a for a in args if a != "--global"]
    if not positional:
        await _reply(room_id, "usage: `!link remove <name> [--global]`")
        return
    name = positional[0]
    if is_global:
        if not await is_admin(sender):
            await _reply(room_id, "only admins can remove global aliases")
            return
        removed = await remove_global_alias(name)
        if removed:
            await audit("alias_remove", room_id=room_id, actor=sender, name=name, scope="global")
            await _reply(room_id, f"🗑 removed global alias `!{name.lstrip('!')}`")
        else:
            await _reply(room_id, f"no global alias `!{name.lstrip('!')}`")
        return

    removed = await remove_room_alias(room_id, name)
    if removed:
        await audit("alias_remove", room_id=room_id, actor=sender, name=name, scope="room")
        await _reply(room_id, f"🗑 removed alias `!{name.lstrip('!')}` from this room")
        return

    # No room-scoped match. If a global with the same name exists, hint.
    async with session_scope() as s:
        g = (
            await s.scalars(
                select(CommandAlias).where(
                    CommandAlias.scope == "global",
                    CommandAlias.name == name.lstrip("!").lower(),
                )
            )
        ).first()
    if g is not None:
        await _reply(
            room_id,
            f"no room-scoped `!{name.lstrip('!')}`, but a global one exists — "
            f"remove with `!link remove !{name.lstrip('!')} --global` (admin only)",
        )
    else:
        await _reply(room_id, f"no alias `!{name.lstrip('!')}` in this room")


async def _cmd_add(room_id: str, name: str, tokens: list[str], sender: str) -> None:
    tokens_no_flags, exact, is_global, description = _extract_flags(tokens)
    if not tokens_no_flags:
        await _reply(room_id, "usage: `!link <name> <target> [--desc \"...\"] [--exact] [--global]`")
        return

    target = " ".join(tokens_no_flags).strip()
    if not target.startswith("!"):
        await _reply(
            room_id,
            f"target must start with `!` (got `{target[:80]}`)",
        )
        return

    if is_global and not await is_admin(sender):
        await _reply(room_id, "only admins can create global aliases")
        return

    from app.commands.builtin import BUILTINS

    name_stripped = name.lstrip("!").lower()
    if not name_stripped:
        await _reply(room_id, "alias name cannot be empty")
        return

    builtin_warn = name_stripped in BUILTINS

    try:
        if is_global:
            row = await add_global_alias(name_stripped, target, description, exact, sender)
        else:
            row = await add_room_alias(room_id, name_stripped, target, description, exact, sender)
    except IntegrityError:
        await _reply(
            room_id,
            f"alias `!{name_stripped}` already exists in this scope. "
            f"Remove with `!link remove !{name_stripped}"
            f"{' --global' if is_global else ''}` first.",
        )
        return

    await audit(
        "alias_add", room_id=room_id, actor=sender,
        name=name_stripped, target=target[:200], scope=row.scope, exact=exact,
    )

    marks = []
    if exact:
        marks.append("exact")
    if is_global:
        marks.append("global")
    else:
        marks.append("room")
    mark_str = f" _({', '.join(marks)})_"
    warn = ""
    if builtin_warn:
        warn = f"\n⚠️ `{name_stripped}` is a builtin — the builtin will fire, this alias won't trigger."
    await _reply(room_id, f"🔗 alias `!{name_stripped}` → `{target}`{mark_str}{warn}")


async def cmd_link(room_id: str, args: str, sender: str) -> None:
    body = args.strip()
    if not body:
        body = "list"
    try:
        tokens = shlex.split(body)
    except ValueError as exc:
        await _reply(room_id, f"parse error: {exc}")
        return
    if not tokens:
        await _reply(room_id, "usage: `!link <name> <target> [--desc \"...\"] [--exact] [--global]`")
        return

    sub = tokens[0].lower()
    rest = tokens[1:]

    if sub == "list":
        await _cmd_list(room_id, rest)
        return
    if sub == "show":
        if not rest:
            await _reply(room_id, "usage: `!link show <name>`")
            return
        await _cmd_show(room_id, rest[0])
        return
    if sub in {"remove", "delete", "rm"}:
        await _cmd_remove(room_id, rest, sender)
        return

    # Otherwise assume: <name> <target...> [flags]
    await _cmd_add(room_id, tokens[0], tokens[1:], sender)
