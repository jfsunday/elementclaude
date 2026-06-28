from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

RULE_NAME = "elementclaude-inbox"


def _desired_rule() -> dict[str, Any]:
    return {
        "name": RULE_NAME,
        "enabled": True,
        "filter_mode": "OR",
        "filters": {
            # No filters → catch everything. Room-level auth happens inside elementclaude.
        },
        "webhook": {
            "url": f"{settings.elementclaude_internal_url.rstrip('/')}/webhook/inbox",
            "method": "POST",
            "secret": settings.webhook_secret,
            "headers": {},
            "timeout_seconds": 30,
            "max_attempts": 1,
            "backoff_base_seconds": 1,
        },
    }


def _webhook_matches(existing: dict[str, Any], desired: dict[str, Any]) -> bool:
    e_wh = existing.get("webhook") or {}
    d_wh = desired["webhook"]
    return (
        str(e_wh.get("url")).rstrip("/") == str(d_wh["url"]).rstrip("/")
        and e_wh.get("secret") == d_wh["secret"]
        and existing.get("enabled") is True
    )


async def ensure_webhook_rule() -> None:
    """Make sure messaging-bot has an enabled rule pointing at us.

    Idempotent: creates the rule if missing, updates it if URL or secret drifted.
    """
    desired = _desired_rule()
    headers = {"X-API-Key": settings.messaging_bot_api_key}

    async with httpx.AsyncClient(
        base_url=settings.messaging_bot_url, headers=headers, timeout=10.0
    ) as client:
        try:
            resp = await client.get("/api/rules")
            resp.raise_for_status()
        except Exception:
            logger.exception(
                "bootstrap: could not reach messaging-bot at %s — inbound webhooks will not arrive",
                settings.messaging_bot_url,
            )
            return

        rules = resp.json()
        existing = next((r for r in rules if r.get("name") == RULE_NAME), None)

        if existing is None:
            logger.info("bootstrap: creating webhook rule %r", RULE_NAME)
            r = await client.post("/api/rules", json=desired)
            if r.status_code >= 400:
                logger.error("bootstrap: rule create failed: %s %s", r.status_code, r.text[:300])
                return
            logger.info("bootstrap: rule created id=%s", r.json().get("id"))
            return

        if _webhook_matches(existing, desired):
            logger.info("bootstrap: webhook rule %r already up to date", RULE_NAME)
            return

        logger.info("bootstrap: updating webhook rule %r (url or secret drifted)", RULE_NAME)
        r = await client.put(
            f"/api/rules/{existing['id']}",
            json={
                "enabled": True,
                "webhook": desired["webhook"],
                "filter_mode": desired["filter_mode"],
                "filters": desired["filters"],
            },
        )
        if r.status_code >= 400:
            logger.error("bootstrap: rule update failed: %s %s", r.status_code, r.text[:300])
        else:
            logger.info("bootstrap: rule updated")
