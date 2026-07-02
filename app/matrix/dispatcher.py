from __future__ import annotations

import logging
from typing import Any

from app.rooms.auth import is_admin
from app.rooms.state import audit, is_room_enabled

logger = logging.getLogger(__name__)


def _is_text_message(event: dict[str, Any]) -> bool:
    if event.get("event_type") != "m.room.message":
        return False
    content = event.get("content") or {}
    return content.get("msgtype") == "m.text" and bool(event.get("body"))


def _is_reaction(event: dict[str, Any]) -> bool:
    return event.get("event_type") == "m.reaction"


async def handle_inbound(event: dict[str, Any]) -> None:
    """Route an inbound Matrix event from messaging-bot."""
    room = event.get("room") or {}
    sender = event.get("sender") or {}
    room_id = room.get("id")
    sender_id = sender.get("id")
    if not room_id or not sender_id:
        return

    if _is_reaction(event):
        # Reactions land in their own queue (Phase 05). Even if the room isn't whitelisted,
        # reactions to an existing approval message should still resolve — but admin check
        # is enforced in the tracker. For now just hand off.
        from app.reactions.tracker import handle_reaction

        await handle_reaction(event)
        return

    if not _is_text_message(event):
        return

    body: str = event["body"]
    sender_is_admin = await is_admin(sender_id)
    room_enabled = await is_room_enabled(room_id)

    # Admins can always run !auth, even in non-whitelisted rooms — that's how a fresh room
    # gets onboarded. Everything else requires the room to be whitelisted.
    is_auth_cmd = body.lstrip().startswith("!auth")

    if not room_enabled and not (sender_is_admin and is_auth_cmd):
        logger.info(
            "drop: room %s not whitelisted (sender=%s, body=%r)",
            room_id, sender_id, body[:60],
        )
        return

    await audit("inbound_message", room_id=room_id, actor=sender_id, body_preview=body[:200])

    # Routing priority for plain messages (no `!` prefix):
    #   1. Active PTY shell → stdin
    #   2. Open AskUserQuestion / ExitPlanMode / approval pending → answer
    #   3. Command router (agent prompt)
    # `!`-commands always go through the router so control (`!end`, `!cancel`, …)
    # works during any of these states.
    body_starts_with_bang = body.lstrip().startswith("!")

    if not body_starts_with_bang:
        from app.shell.interactive import get_active

        sh = get_active(room_id)
        if sh is not None:
            if not sh.write(body + "\n"):
                await audit("shell_stdin_failed", room_id=room_id, actor=sender_id)
            return

        from app.agent.permissions import handle_text_answer

        if await handle_text_answer(room_id, body, sender_id):
            return

    from app.commands.router import dispatch

    await dispatch(event)
