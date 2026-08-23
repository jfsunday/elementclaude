from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.config import settings
from app.rooms.auth import is_admin
from app.rooms.state import audit, get_room, is_room_enabled

logger = logging.getLogger(__name__)


def _is_text_message(event: dict[str, Any]) -> bool:
    if event.get("event_type") != "m.room.message":
        return False
    content = event.get("content") or {}
    return content.get("msgtype") == "m.text" and bool(event.get("body"))


def _is_media_message(event: dict[str, Any]) -> bool:
    if event.get("event_type") != "m.room.message":
        return False
    content = event.get("content") or {}
    return content.get("msgtype") in {"m.image", "m.file", "m.video", "m.audio"}


def _is_reaction(event: dict[str, Any]) -> bool:
    return event.get("event_type") == "m.reaction"


async def _route_text(event: dict[str, Any], body: str, room_id: str, sender_id: str) -> None:
    """Route message text through the shell / pending-question / command layers.

    Shared by real `m.text` events and transcripts of voice messages, so a spoken
    prompt behaves exactly like a typed one.
    """
    await audit("inbound_message", room_id=room_id, actor=sender_id, body_preview=body[:200])

    # Routing priority for plain messages (no `!` prefix):
    #   1. Active PTY shell → stdin
    #   2. Open AskUserQuestion / ExitPlanMode / approval pending → answer
    #   3. Command router (agent prompt)
    # `!`-commands always go through the router so control (`!end`, `!cancel`, …)
    # works during any of these states.
    if not body.lstrip().startswith("!"):
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

    if event.get("body") != body:
        event = {
            **event,
            "body": body,
            "content": {**(event.get("content") or {}), "body": body, "msgtype": "m.text"},
        }
    await dispatch(event)


# Rough ceiling so a missing `info.duration` can't smuggle an hour of audio into
# Whisper: 32 KiB/s is far above any Matrix voice-message bitrate.
_STT_BYTES_PER_SECOND = 32 * 1024


async def _handle_media(event: dict[str, Any], room_id: str, sender_id: str) -> None:
    from app.agent.attachments import handle_inbound_media, queue_attachment
    from app.matrix import outbox

    content = event.get("content") or {}
    room = await get_room(room_id)
    want_stt = content.get("msgtype") == "m.audio" and bool(room and room.stt_enabled)

    duration_ms = (content.get("info") or {}).get("duration")
    if (
        want_stt
        and isinstance(duration_ms, (int, float))
        and duration_ms > settings.stt_max_seconds * 1000
    ):
        want_stt = False
        await outbox.send_text(
            room_id,
            f"🎙️ longer than {settings.stt_max_seconds}s — attaching instead of transcribing.",
            notice=True,
        )

    path = await handle_inbound_media(event, queue=not want_stt)
    if path is None:
        return

    # Clients that don't send `info.duration` bypass the check above, so guard on size too.
    if want_stt and path.stat().st_size > settings.stt_max_seconds * _STT_BYTES_PER_SECOND:
        await queue_attachment(room_id, path)
        await outbox.send_text(
            room_id,
            f"🎙️ too large to transcribe (cap is ~{settings.stt_max_seconds}s) — "
            "📎 attached instead.",
            notice=True,
        )
        return

    if not want_stt:
        await outbox.send_text(
            room_id,
            "📎 attached — will hand it to Claude with your next message.",
            notice=True,
        )
        return

    from app.voice import stt

    # `resolve_local`'s "no token → local" note is shown by `!voice`, not per message.
    use_local, _note = stt.resolve_local(room.voice_engine if room else "cloud")

    reason = stt.unavailable_reason(local=use_local)
    if reason is None and use_local and not stt.model_loaded():
        # First local run in this process may download a multi-GB model.
        await outbox.send_text(
            room_id,
            "🎙️ transcribing locally — the first run downloads the Whisper model, "
            "this can take a few minutes.",
            notice=True,
        )

    if reason is not None:
        transcript = None
    else:
        typing_task = asyncio.create_task(outbox.typing_keepalive(room_id))
        try:
            transcript = await stt.transcribe(path, local=use_local)
        finally:
            typing_task.cancel()
            try:
                await typing_task
            except (asyncio.CancelledError, Exception):
                pass

    if transcript is None:
        await queue_attachment(room_id, path)
        detail = reason or "could not transcribe"
        await outbox.send_text(
            room_id, f"⚠️ {detail} — 📎 attached the audio instead.", notice=True
        )
        return

    await audit("stt_transcript", room_id=room_id, actor=sender_id, local=use_local)
    await outbox.send_text(room_id, f'🎙️ "{transcript}"', notice=True)
    await _route_text(event, transcript, room_id, sender_id)


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

    if _is_media_message(event):
        if not await is_room_enabled(room_id):
            return
        await _handle_media(event, room_id, sender_id)
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

    await _route_text(event, body, room_id, sender_id)
