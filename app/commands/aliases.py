from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.db import session_scope
from app.models import CommandAlias

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


def _strip_bang(name: str) -> str:
    return name[1:] if name.startswith("!") else name


async def get_alias(room_id: str, name: str) -> CommandAlias | None:
    """Room-scoped alias wins over global."""
    name = _strip_bang(name).lower()
    async with session_scope() as s:
        room_hit = (
            await s.scalars(
                select(CommandAlias).where(
                    CommandAlias.scope == "room",
                    CommandAlias.room_id == room_id,
                    CommandAlias.name == name,
                )
            )
        ).first()
        if room_hit is not None:
            return room_hit
        global_hit = (
            await s.scalars(
                select(CommandAlias).where(
                    CommandAlias.scope == "global",
                    CommandAlias.name == name,
                )
            )
        ).first()
        return global_hit


async def list_aliases(room_id: str, *, include_global: bool = True) -> list[CommandAlias]:
    async with session_scope() as s:
        rows = list((await s.scalars(
            select(CommandAlias).where(
                CommandAlias.scope == "room", CommandAlias.room_id == room_id
            ).order_by(CommandAlias.name)
        )).all())
        if include_global:
            g = list((await s.scalars(
                select(CommandAlias).where(CommandAlias.scope == "global").order_by(CommandAlias.name)
            )).all())
            rows.extend(g)
        return rows


async def list_global() -> list[CommandAlias]:
    async with session_scope() as s:
        return list((await s.scalars(
            select(CommandAlias).where(CommandAlias.scope == "global").order_by(CommandAlias.name)
        )).all())


async def _add(
    *, scope: str, room_id: str, name: str, target: str,
    description: str, exact: bool, by: str,
) -> CommandAlias:
    async with session_scope() as s:
        row = CommandAlias(
            scope=scope, room_id=room_id, name=_strip_bang(name).lower(),
            target=target, description=description, exact=exact, created_by=by,
        )
        s.add(row)
        await s.commit()
        await s.refresh(row)
        return row


async def add_room_alias(
    room_id: str, name: str, target: str, description: str, exact: bool, by: str
) -> CommandAlias:
    return await _add(
        scope="room", room_id=room_id, name=name, target=target,
        description=description, exact=exact, by=by,
    )


async def add_global_alias(
    name: str, target: str, description: str, exact: bool, by: str
) -> CommandAlias:
    return await _add(
        scope="global", room_id="", name=name, target=target,
        description=description, exact=exact, by=by,
    )


async def remove_room_alias(room_id: str, name: str) -> bool:
    name = _strip_bang(name).lower()
    async with session_scope() as s:
        row = (
            await s.scalars(
                select(CommandAlias).where(
                    CommandAlias.scope == "room",
                    CommandAlias.room_id == room_id,
                    CommandAlias.name == name,
                )
            )
        ).first()
        if row is None:
            return False
        await s.delete(row)
        await s.commit()
        return True


async def remove_global_alias(name: str) -> bool:
    name = _strip_bang(name).lower()
    async with session_scope() as s:
        row = (
            await s.scalars(
                select(CommandAlias).where(
                    CommandAlias.scope == "global", CommandAlias.name == name,
                )
            )
        ).first()
        if row is None:
            return False
        await s.delete(row)
        await s.commit()
        return True


def _target_builtin(target: str) -> str | None:
    """If target is exactly `!<one-token>` and that token names a builtin,
    return the canonical builtin name. Otherwise None."""
    from app.commands.builtin import BUILTINS

    stripped = target.strip()
    if not stripped.startswith("!"):
        return None
    body = stripped[1:]
    if not body or " " in body or "\t" in body:
        return None
    if body.startswith("gsd:"):
        return None
    lower = body.lower()
    return lower if lower in BUILTINS else None


async def build_alias_maps(
    room_id: str,
) -> tuple[dict[str, list[str]], list[tuple[str, str, str, bool, bool]]]:
    """Returns:
      - inline_map: {canonical_builtin_name: [alias_name, ...]}
      - customs: list of (name, target, description, exact, is_global)
    Room-scoped alias hides a global alias of the same name (dedup by name).
    """
    rows = await list_aliases(room_id, include_global=True)

    # Dedup: room wins over global for identical names
    seen: dict[str, CommandAlias] = {}
    for row in rows:
        if row.name in seen:
            # room-scoped comes first in list_aliases; if it's already there,
            # skip the global one
            continue
        seen[row.name] = row

    inline: dict[str, list[str]] = {}
    customs: list[tuple[str, str, str, bool, bool]] = []
    for row in seen.values():
        canonical = _target_builtin(row.target)
        if canonical is not None:
            inline.setdefault(canonical, []).append(row.name)
        else:
            customs.append(
                (row.name, row.target, row.description, row.exact, row.scope == "global")
            )
    return inline, customs
