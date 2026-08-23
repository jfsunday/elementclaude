from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.config import settings
from app.db import session_scope
from app.models import AuditLog, Room


async def get_room(room_id: str) -> Room | None:
    async with session_scope() as s:
        return await s.get(Room, room_id)


async def upsert_room(room_id: str, **fields: Any) -> Room:
    """Get-or-create a Room row and update any provided fields."""
    async with session_scope() as s:
        room = await s.get(Room, room_id)
        if room is None:
            room = Room(
                room_id=room_id,
                enabled=fields.get("enabled", False),
                mode=fields.get("mode", settings.default_mode),
                model=fields.get("model", settings.default_model),
                cwd=fields.get("cwd"),
                claude_session_id=fields.get("claude_session_id"),
                stt_enabled=fields.get("stt_enabled", settings.voice_enabled_default),
                tts_enabled=fields.get("tts_enabled", settings.voice_enabled_default),
                voice_engine=fields.get("voice_engine", settings.voice_engine_default),
                tts_voice=fields.get("tts_voice"),
            )
            s.add(room)
        else:
            for k, v in fields.items():
                setattr(room, k, v)
            room.last_activity = datetime.now(timezone.utc)
        await s.commit()
        await s.refresh(room)
        return room


async def is_room_enabled(room_id: str) -> bool:
    room = await get_room(room_id)
    return bool(room and room.enabled)


async def list_enabled_rooms() -> list[Room]:
    async with session_scope() as s:
        rows = await s.scalars(select(Room).where(Room.enabled.is_(True)))
        return list(rows.all())


async def audit(action: str, *, room_id: str | None = None, actor: str | None = None, **payload: Any) -> None:
    async with session_scope() as s:
        s.add(AuditLog(action=action, room_id=room_id, actor=actor, payload=payload))
        await s.commit()
