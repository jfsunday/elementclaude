from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=settings.messaging_bot_url,
            headers={"X-API-Key": settings.messaging_bot_api_key},
            timeout=httpx.Timeout(30.0),
        )
    return _client


async def close_http() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def _post(path: str, payload: dict[str, Any]) -> str | None:
    """Best-effort POST. Returns event_id on success, None on failure (logged)."""
    try:
        resp = await _http().post(path, json=payload)
        resp.raise_for_status()
        return resp.json().get("event_id")
    except Exception as exc:
        logger.warning("outbox %s failed: %s", path, exc)
        return None


async def send_text(room_id: str, body: str, *, notice: bool = False) -> str | None:
    return await _post(
        "/api/messages",
        {"room_id": room_id, "body": body, "msgtype": "m.notice" if notice else "m.text"},
    )


async def send_markdown(room_id: str, body: str, html: str, *, notice: bool = False) -> str | None:
    return await _post(
        "/api/messages",
        {
            "room_id": room_id,
            "body": body,
            "msgtype": "m.notice" if notice else "m.text",
            "formatted_body": html,
            "format": "org.matrix.custom.html",
        },
    )


async def react(room_id: str, target_event_id: str, key: str) -> str | None:
    return await _post(
        "/api/messages/react",
        {"room_id": room_id, "event_id": target_event_id, "key": key},
    )


async def edit_text(room_id: str, event_id: str, body: str, *, notice: bool = False) -> str | None:
    return await _post(
        "/api/messages/edit",
        {
            "room_id": room_id, "event_id": event_id, "body": body,
            "msgtype": "m.notice" if notice else "m.text",
        },
    )


async def edit_markdown(
    room_id: str, event_id: str, body: str, html: str, *, notice: bool = False
) -> str | None:
    return await _post(
        "/api/messages/edit",
        {
            "room_id": room_id, "event_id": event_id, "body": body,
            "msgtype": "m.notice" if notice else "m.text",
            "formatted_body": html,
            "format": "org.matrix.custom.html",
        },
    )


async def set_typing(room_id: str, typing: bool, timeout_ms: int = 30000) -> None:
    try:
        resp = await _http().post(
            "/api/messages/typing",
            json={"room_id": room_id, "typing": typing, "timeout_ms": timeout_ms if typing else 0},
        )
        resp.raise_for_status()
    except Exception as exc:
        logger.debug("set_typing failed: %s", exc)


async def download_media(mxc: str | None, file_info: dict[str, Any] | None = None) -> tuple[bytes, str] | None:
    """Fetch an mxc uri (encrypted or not). Returns (bytes, content_type) or None on failure."""
    if not mxc and not file_info:
        return None
    try:
        resp = await _http().post(
            "/api/media/download",
            json={"mxc": mxc, "file": file_info},
            timeout=httpx.Timeout(60.0),
        )
        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "application/octet-stream")
        return resp.content, ctype
    except Exception as exc:
        logger.warning("download_media failed: %s", exc)
        return None


async def send_media(
    room_id: str, path: str, *, msgtype: str = "m.image", caption: str | None = None
) -> str | None:
    """Upload a local file and post it as a media message. Returns event_id or None."""
    import mimetypes
    from pathlib import Path

    p = Path(path)
    if not p.is_file():
        logger.warning("send_media: file not found: %s", path)
        return None
    mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    try:
        with p.open("rb") as f:
            data = f.read()
        files = {"file": (p.name, data, mime)}
        form = {"room_id": room_id, "msgtype": msgtype}
        if caption:
            form["caption"] = caption
        resp = await _http().post(
            "/api/messages/media",
            files=files,
            data=form,
            timeout=httpx.Timeout(60.0),
        )
        resp.raise_for_status()
        return resp.json().get("event_id")
    except Exception as exc:
        logger.warning("send_media failed for %s: %s", path, exc)
        return None


async def list_rooms() -> list[dict[str, Any]]:
    resp = await _http().get("/api/rooms")
    resp.raise_for_status()
    return resp.json()
