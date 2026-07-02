from __future__ import annotations

import hashlib
import hmac
import logging
from collections import OrderedDict
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, status

from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook", tags=["webhook"])


# Belt-and-suspenders idempotency: drop repeats of the same Matrix event_id.
# messaging-bot's callback registrations were the primary duplicate source, but
# a webhook retry or a resync sweep could still deliver the same event twice.
_SEEN_MAX = 2048
_seen_events: OrderedDict[str, None] = OrderedDict()


def _mark_seen(event_id: str) -> bool:
    """Returns True if this event_id was already seen recently."""
    if event_id in _seen_events:
        # Refresh recency so a re-seen id stays remembered
        _seen_events.move_to_end(event_id)
        return True
    _seen_events[event_id] = None
    if len(_seen_events) > _SEEN_MAX:
        _seen_events.popitem(last=False)
    return False


def _verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    if not signature:
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


@router.post("/inbox")
async def inbox(
    request: Request,
    x_webhook_signature: str | None = Header(default=None),
) -> dict[str, Any]:
    raw = await request.body()
    if not _verify_signature(settings.webhook_secret, raw, x_webhook_signature):
        logger.warning("inbox: bad or missing HMAC signature")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bad signature")

    try:
        event = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid json: {exc}") from exc

    event_id = event.get("event_id")
    if event_id and _mark_seen(event_id):
        logger.debug("dropped duplicate event_id=%s", event_id)
        return {"status": "duplicate"}

    # Dispatch is owned by the dispatcher module. Whatever happens downstream,
    # we ack 200 so messaging-bot does not retry.
    from app.matrix.dispatcher import handle_inbound

    try:
        await handle_inbound(event)
    except Exception:
        logger.exception("inbound dispatch crashed (event_id=%s)", event_id)
    return {"status": "accepted"}
