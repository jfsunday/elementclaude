from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def handle_reaction(event: dict[str, Any]) -> None:
    """Phase 02 stub. Filled in during Phase 05 (approval flow)."""
    content = event.get("content") or {}
    target = content.get("reacts_to")
    key = content.get("key")
    logger.debug("reaction → %s (target=%s)", key, target)
