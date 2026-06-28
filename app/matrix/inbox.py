from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, status

from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook", tags=["webhook"])


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

    # Dispatch is owned by the dispatcher module — phases 02+ will plug in here.
    # Whatever happens downstream, we ack 200 so messaging-bot does not retry.
    from app.matrix.dispatcher import handle_inbound

    try:
        await handle_inbound(event)
    except Exception:
        logger.exception("inbound dispatch crashed (event_id=%s)", event.get("event_id"))
    return {"status": "accepted"}
