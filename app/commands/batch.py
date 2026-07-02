from __future__ import annotations

import logging
import re
from typing import Any

from app.matrix import outbox

logger = logging.getLogger(__name__)


# Split on `&&` (stop on failure) or `;` / `&` (continue on failure). We do NOT
# use shlex.split — commands can contain their own quotes / operators.
_SPLIT_RE = re.compile(r"(\s*&&\s*|\s*;\s*|\s+&\s+)")


def _split_batch(body: str) -> list[tuple[str, str]]:
    """Returns [(cmd, sep_before_cmd), ...] where sep is '&&' | ';' | '&' | ''."""
    parts = _SPLIT_RE.split(body)
    out: list[tuple[str, str]] = []
    prev_sep = ""
    for i, p in enumerate(parts):
        if i % 2 == 0:
            out.append((p.strip(), prev_sep))
        else:
            prev_sep = p.strip()
    return [(cmd, sep) for cmd, sep in out if cmd]


async def cmd_batch(room_id: str, args: str, sender: str) -> None:
    body = args.strip()
    if not body:
        await outbox.send_text(
            room_id,
            "usage: `!batch <cmd1> && <cmd2> ; <cmd3>` — `&&` stops on fail, `;` or `&` continues",
            notice=True,
        )
        return

    steps = _split_batch(body)
    if not steps:
        return

    from app.commands.router import dispatch

    for i, (cmd, sep) in enumerate(steps, start=1):
        await outbox.send_text(
            room_id,
            f"▶️ batch [{i}/{len(steps)}]: `{cmd[:200]}`",
            notice=True,
        )
        # Build a synthetic event so !-commands go through the parser and free
        # text goes to Claude, just like a normal Element message would.
        synth_event: dict[str, Any] = {
            "event_id": f"$batch-{room_id}-{i}",
            "event_type": "m.room.message",
            "room": {"id": room_id},
            "sender": {"id": sender},
            "content": {"msgtype": "m.text", "body": cmd},
            "body": cmd,
        }
        try:
            await dispatch(synth_event)
        except Exception:
            logger.exception("batch step %d failed: %r", i, cmd)
            await outbox.send_text(room_id, f"❌ batch step {i} raised — stopping", notice=True)
            return
