from __future__ import annotations

import logging
from typing import Any

from app.commands.builtin import BUILTINS
from app.matrix import outbox

logger = logging.getLogger(__name__)


_MAX_ALIAS_DEPTH = 10


def _parse(body: str) -> tuple[str | None, str]:
    """Returns (command_name, rest). command_name is None if no leading `!`.

    `!cmd args…` → ("cmd", "args…")
    `!gsd:plan-phase 7` → ("gsd:plan-phase", "7")
    `hello` → (None, "hello")
    """
    body = body.lstrip()
    if not body.startswith("!"):
        return None, body
    rest = body[1:]
    if not rest:
        return None, ""
    parts = rest.split(maxsplit=1)
    name = parts[0]
    args = parts[1] if len(parts) > 1 else ""
    return name, args


async def dispatch(
    event: dict[str, Any],
    _alias_chain: frozenset[str] = frozenset(),
) -> None:
    body = (event.get("body") or "").strip()
    sender_id = (event.get("sender") or {}).get("id") or ""
    room_id = (event.get("room") or {}).get("id") or ""

    cmd, args = _parse(body)

    if cmd is None:
        # Free text → prompt for the Claude agent
        from app.agent.session import handle_prompt

        await handle_prompt(room_id, sender_id, body)
        return

    # !gsd:* is special — forwarded to the agent as /gsd:* so the skill resolves
    if cmd.startswith("gsd:"):
        from app.agent.session import handle_prompt

        forwarded = "/" + cmd
        if args:
            forwarded = f"{forwarded} {args}"
        await handle_prompt(room_id, sender_id, forwarded)
        return

    # Builtin wins over aliases
    cmd_lower = cmd.lower()
    handler = BUILTINS.get(cmd_lower)
    if handler is not None:
        try:
            await handler(room_id, args, sender_id)
        except Exception:
            logger.exception("command %r failed", cmd)
            await outbox.send_text(
                room_id, f"error running `!{cmd}` — check logs", notice=True,
            )
        return

    # Alias lookup: room-scoped first, then global.
    from app.commands.aliases import get_alias

    alias = await get_alias(room_id, cmd_lower)
    if alias is None:
        await outbox.send_text(
            room_id, f"unknown command `!{cmd}` — try `!help`", notice=True,
        )
        return

    if cmd_lower in _alias_chain or len(_alias_chain) >= _MAX_ALIAS_DEPTH:
        chain = " → ".join([*_alias_chain, cmd_lower])
        await outbox.send_text(
            room_id, f"alias loop detected: {chain}", notice=True,
        )
        return

    # Expand and re-dispatch
    new_body = alias.target if alias.exact else f"{alias.target} {args}".rstrip()
    new_event = {
        **event,
        "body": new_body,
        "content": {**(event.get("content") or {}), "body": new_body},
    }
    await dispatch(new_event, _alias_chain=_alias_chain | {cmd_lower})
