from __future__ import annotations

import logging
from typing import Any

from app.agent.permissions import handle_reaction_event
from app.rooms.auth import is_admin
from app.rooms.state import is_room_enabled

logger = logging.getLogger(__name__)


async def handle_reaction(event: dict[str, Any]) -> None:
    """Inbound m.reaction event. If it targets a pending approval message, resolve it."""
    content = event.get("content") or {}
    key = content.get("key")
    target = content.get("reacts_to")
    sender = (event.get("sender") or {}).get("id") or ""
    room_id = (event.get("room") or {}).get("id") or ""

    if not key or not target or not sender:
        return

    # Only accept reactions from senders allowed to act in this room:
    # whitelisted-room members + admins. Anyone else's reactions are ignored.
    if not (await is_room_enabled(room_id) or await is_admin(sender)):
        return

    resolved = await handle_reaction_event(target, key, sender)
    if resolved:
        logger.info("approval resolved: target=%s key=%s by=%s", target, key, sender)
