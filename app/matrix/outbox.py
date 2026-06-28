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


async def list_rooms() -> list[dict[str, Any]]:
    resp = await _http().get("/api/rooms")
    resp.raise_for_status()
    return resp.json()
