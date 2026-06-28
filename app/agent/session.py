from __future__ import annotations

import logging

from app.matrix import outbox

logger = logging.getLogger(__name__)


async def handle_prompt(room_id: str, sender_id: str, prompt: str) -> None:
    """Phase 03 stub. Phase 04 wires this to the Claude Agent SDK."""
    preview = prompt[:120]
    logger.info("prompt from %s in %s: %r", sender_id, room_id, preview)
    await outbox.send_text(
        room_id,
        f"📥 received (agent not yet wired up — phase 04)\n> {preview}",
        notice=True,
    )
