from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def handle_inbound(event: dict[str, Any]) -> None:
    """Entry point for inbound Matrix events from messaging-bot.

    Phase 01: just log. Later phases wire up auth, commands, and the agent.
    """
    event_type = event.get("event_type")
    room = event.get("room") or {}
    sender = event.get("sender") or {}
    body = event.get("body")
    logger.info(
        "inbound %s in %s from %s: %r",
        event_type,
        room.get("id"),
        sender.get("id"),
        (body or "")[:120],
    )
