from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def dispatch(event: dict[str, Any]) -> None:
    """Phase 02 stub. Phase 03 turns this into a real ! command parser."""
    body = (event.get("body") or "").strip()
    sender = (event.get("sender") or {}).get("id")
    room_id = (event.get("room") or {}).get("id")
    logger.info("router stub: %s @ %s — %r", sender, room_id, body[:120])
