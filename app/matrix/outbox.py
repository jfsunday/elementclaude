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


async def send_text(room_id: str, body: str, *, notice: bool = False) -> str:
    """Send a text/notice message to a room. Returns the matrix event_id."""
    payload: dict[str, Any] = {
        "room_id": room_id,
        "body": body,
        "msgtype": "m.notice" if notice else "m.text",
    }
    resp = await _http().post("/api/messages", json=payload)
    resp.raise_for_status()
    return resp.json()["event_id"]


async def send_markdown(room_id: str, body: str, html: str, *, notice: bool = False) -> str:
    """Send a message with both plain and HTML body — Element renders the HTML."""
    payload: dict[str, Any] = {
        "room_id": room_id,
        "body": body,
        "msgtype": "m.notice" if notice else "m.text",
        "formatted_body": html,
        "format": "org.matrix.custom.html",
    }
    resp = await _http().post("/api/messages", json=payload)
    resp.raise_for_status()
    return resp.json()["event_id"]


async def react(room_id: str, target_event_id: str, key: str) -> str:
    """Add a reaction. Returns the reaction event_id."""
    payload = {"room_id": room_id, "event_id": target_event_id, "key": key}
    resp = await _http().post("/api/messages/react", json=payload)
    resp.raise_for_status()
    return resp.json()["event_id"]


async def list_rooms() -> list[dict[str, Any]]:
    resp = await _http().get("/api/rooms")
    resp.raise_for_status()
    return resp.json()
