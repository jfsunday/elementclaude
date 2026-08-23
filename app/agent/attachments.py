from __future__ import annotations

import asyncio
import logging
import mimetypes
import re
from pathlib import Path
from typing import Any

from app.config import settings
from app.matrix import outbox

logger = logging.getLogger(__name__)


# Per-room queue of downloaded attachment paths waiting to be handed to Claude
# on the next prompt.
_pending_attachments: dict[str, list[Path]] = {}
_lock = asyncio.Lock()


def _uploads_root() -> Path:
    d = settings.data_dir / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    return d


_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_name(s: str, default: str = "file") -> str:
    s = s.strip() or default
    s = _SAFE.sub("_", s)
    return s[:80] or default


async def handle_inbound_media(event: dict[str, Any], *, queue: bool = True) -> Path | None:
    """Download an inbound m.image/m.file/etc and cache the path for the next
    prompt in this room. Returns the local path if successful.

    `queue=False` downloads without enqueuing — used by the voice path, where the
    audio becomes a transcript rather than an attachment."""
    room = event.get("room") or {}
    room_id = room.get("id")
    if not room_id:
        return None

    content = event.get("content") or {}
    mxc = content.get("url")
    file_info = content.get("file") if isinstance(content.get("file"), dict) else None
    if not mxc and not file_info:
        logger.debug("media event without url/file, skipping")
        return None

    body = event.get("body") or content.get("body") or "attachment"
    event_id = event.get("event_id") or "no-id"
    room_slug = _safe_name(room_id.strip("!").split(":")[0], "room")
    filename = _safe_name(body, "attachment")

    dl = await outbox.download_media(mxc, file_info=file_info)
    if dl is None:
        return None
    data, ctype = dl

    ext = mimetypes.guess_extension(ctype.split(";")[0].strip()) or ""
    if not filename.lower().endswith(ext.lower()) and ext:
        filename = f"{filename}{ext}"

    dest_dir = _uploads_root() / room_slug
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{event_id}-{filename}"
    dest.write_bytes(data)

    if queue:
        await queue_attachment(room_id, dest)

    logger.info("downloaded attachment for room=%s: %s (%d bytes)", room_id, dest, len(data))
    return dest


async def queue_attachment(room_id: str, path: Path) -> None:
    async with _lock:
        _pending_attachments.setdefault(room_id, []).append(path)


async def pop_attachments(room_id: str) -> list[Path]:
    async with _lock:
        return _pending_attachments.pop(room_id, [])


def format_prompt_with_attachments(prompt: str, paths: list[Path]) -> str:
    """Prepend attached-file references so Claude reads them via its Read tool."""
    if not paths:
        return prompt
    lines = ["The user attached the following files — read them if relevant:"]
    for p in paths:
        lines.append(f"- {p}")
    return "\n".join(lines) + "\n\n" + prompt
