from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from claude_agent_sdk.types import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from app.db import session_scope
from app.matrix import outbox
from app.models import PendingApproval
from app.rooms.state import audit

logger = logging.getLogger(__name__)


# matrix_event_id (of the approval prompt) → Future awaited by canUseTool
_pending: dict[str, asyncio.Future[dict[str, Any]]] = {}

# Reactions to recognise
_ALLOW = {"✅", "👍", "y", "yes"}
_DENY = {"❌", "👎", "n", "no"}
_ALLOW_ALWAYS = {"🔁", "♾", "always"}

APPROVAL_TIMEOUT_SECONDS = 60 * 30  # 30 min


def _format_input_for_approval(name: str, tool_input: dict[str, Any]) -> tuple[str, str]:
    if name == "Bash":
        cmd = tool_input.get("command", "")
        plain = f"🔧 **Bash**\n```\n{cmd}\n```\n✅ approve · ❌ deny · 🔁 allow-always-this-session"
        html = (
            f"🔧 <b>Bash</b><br><pre><code>{_html_escape(cmd)}</code></pre>"
            "<br>✅ approve · ❌ deny · 🔁 allow-always"
        )
        return plain, html
    if name in ("Write", "Edit"):
        fp = tool_input.get("file_path") or "?"
        plain = (
            f"🔧 **{name}** `{fp}`\n"
            f"✅ approve · ❌ deny · 🔁 allow-always"
        )
        html = (
            f"🔧 <b>{name}</b> <code>{_html_escape(fp)}</code>"
            "<br>✅ approve · ❌ deny · 🔁 allow-always"
        )
        return plain, html
    # generic
    preview = ", ".join(f"{k}={_truncate(str(v), 60)!r}" for k, v in list(tool_input.items())[:3])
    plain = f"🔧 **{name}** ({preview})\n✅ approve · ❌ deny · 🔁 allow-always"
    html = f"🔧 <b>{name}</b> <i>({_html_escape(preview)})</i><br>✅ approve · ❌ deny · 🔁 allow-always"
    return plain, html


def _html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


async def _record_pending(room_id: str, matrix_event_id: str, tool_name: str, tool_input: dict[str, Any]) -> None:
    async with session_scope() as s:
        s.add(
            PendingApproval(
                room_id=room_id,
                matrix_event_id=matrix_event_id,
                tool_name=tool_name,
                tool_input=tool_input,
            )
        )
        await s.commit()


async def _resolve_pending(matrix_event_id: str, decision: str, decided_by: str) -> None:
    async with session_scope() as s:
        from sqlalchemy import select

        row = (
            await s.scalars(
                select(PendingApproval).where(PendingApproval.matrix_event_id == matrix_event_id)
            )
        ).first()
        if row is None:
            return
        row.decision = decision
        row.decided_by = decided_by
        row.resolved_at = datetime.now(timezone.utc)
        await s.commit()


def make_can_use_tool(room_id: str):
    """Returns a canUseTool callback bound to a specific room."""

    async def can_use_tool(
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        plain, html = _format_input_for_approval(tool_name, tool_input)
        event_id = await outbox.send_markdown(room_id, plain, html, notice=True)

        if not event_id:
            # No way to ask the user — fail closed.
            logger.warning("approval: outbox failed; denying %s", tool_name)
            return PermissionResultDeny(
                behavior="deny",
                message="approval request could not be sent",
                interrupt=False,
            )

        # Add tappable reactions in Element
        await outbox.react(room_id, event_id, "✅")
        await outbox.react(room_id, event_id, "❌")
        await outbox.react(room_id, event_id, "🔁")

        await _record_pending(room_id, event_id, tool_name, tool_input)

        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        _pending[event_id] = fut

        try:
            result = await asyncio.wait_for(fut, timeout=APPROVAL_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            await outbox.send_text(room_id, f"⌛ approval for **{tool_name}** timed out — denied", notice=True)
            await _resolve_pending(event_id, "timeout", "")
            return PermissionResultDeny(behavior="deny", message="approval timed out", interrupt=False)
        finally:
            _pending.pop(event_id, None)

        decision = result["decision"]
        decided_by = result.get("decided_by", "")
        await _resolve_pending(event_id, decision, decided_by)
        await audit(
            "tool_decision",
            room_id=room_id,
            actor=decided_by,
            tool=tool_name,
            decision=decision,
        )

        if decision in ("allow", "allow_always"):
            return PermissionResultAllow(behavior="allow", updated_input=None, updated_permissions=None)
        return PermissionResultDeny(behavior="deny", message=f"denied by {decided_by or 'user'}", interrupt=False)

    return can_use_tool


# -------- reaction inbox plug --------


async def handle_reaction_event(target_event_id: str, key: str, sender: str) -> bool:
    """Called by reactions/tracker. Returns True if the reaction resolved a pending approval."""
    fut = _pending.get(target_event_id)
    if fut is None or fut.done():
        return False

    key_norm = key.strip().lower()
    if key in _ALLOW or key_norm in _ALLOW:
        fut.set_result({"decision": "allow", "decided_by": sender})
        return True
    if key in _DENY or key_norm in _DENY:
        fut.set_result({"decision": "deny", "decided_by": sender})
        return True
    if key in _ALLOW_ALWAYS or key_norm in _ALLOW_ALWAYS:
        fut.set_result({"decision": "allow_always", "decided_by": sender})
        return True
    return False
