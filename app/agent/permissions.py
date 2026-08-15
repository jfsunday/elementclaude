from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
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


# --------- pending registry ---------
#
# One entry per outstanding user prompt (tool approval, AskUserQuestion, or
# ExitPlanMode plan review). Reactions on the prompt message and free-text
# replies in the same room both feed the same future.


@dataclass
class Pending:
    room_id: str
    kind: str  # "approval" | "question" | "plan"
    future: asyncio.Future[dict[str, Any]]
    # Extra state per kind:
    options: list[dict[str, Any]] = field(default_factory=list)  # for question
    multi_select: bool = False  # for question
    selected: set[int] = field(default_factory=set)  # for question multi-select buffer
    header: str = ""  # for question
    question_text: str = ""  # for question


# event_id (of the prompt message the user reacts to) → Pending
_pending_by_event: dict[str, Pending] = {}
# room_id → list of Pending (most-recent first) — used to route free-text replies
_pending_by_room: dict[str, list[Pending]] = defaultdict(list)

# room_id → set[tool_name] approved "always" until !clear
_auto_allowed: dict[str, set[str]] = {}


def clear_auto_allowed(room_id: str) -> None:
    _auto_allowed.pop(room_id, None)


# Reactions to recognise for approvals
_ALLOW = {"✅", "👍", "y", "yes"}
_DENY = {"❌", "👎", "n", "no"}
_ALLOW_ALWAYS = {"🔁", "♾", "always"}
_ALLOW_AUTO = {"🚀", "auto"}  # plan approval: switch to auto mode afterwards

# Emoji digits used for numbered question options (up to 9)
_DIGIT_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣"]
_OTHER_EMOJI = "🅾️"
_DONE_EMOJI = "✅"

APPROVAL_TIMEOUT_SECONDS = 60 * 30  # 30 min
QUESTION_TIMEOUT_SECONDS = 60 * 30
PLAN_TIMEOUT_SECONDS = 60 * 60


def _register_pending(event_id: str, pending: Pending) -> None:
    _pending_by_event[event_id] = pending
    _pending_by_room[pending.room_id].append(pending)


def _unregister_pending(pending: Pending, event_ids: list[str]) -> None:
    for eid in event_ids:
        _pending_by_event.pop(eid, None)
    if pending in _pending_by_room.get(pending.room_id, []):
        _pending_by_room[pending.room_id].remove(pending)


def get_active_pending(room_id: str) -> Pending | None:
    """Newest still-open pending for the room, if any. Used by dispatcher to
    route a plain text message as an answer instead of a new prompt."""
    for p in reversed(_pending_by_room.get(room_id, [])):
        if not p.future.done():
            return p
    return None


# --------- formatting ---------


def _html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


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
        plain = f"🔧 **{name}** `{fp}`\n✅ approve · ❌ deny · 🔁 allow-always"
        html = (
            f"🔧 <b>{name}</b> <code>{_html_escape(fp)}</code>"
            "<br>✅ approve · ❌ deny · 🔁 allow-always"
        )
        return plain, html
    preview = ", ".join(f"{k}={_truncate(str(v), 60)!r}" for k, v in list(tool_input.items())[:3])
    plain = f"🔧 **{name}** ({preview})\n✅ approve · ❌ deny · 🔁 allow-always"
    html = f"🔧 <b>{name}</b> <i>({_html_escape(preview)})</i><br>✅ approve · ❌ deny · 🔁 allow-always"
    return plain, html


# --------- db helpers (for approval kind only) ---------


async def _record_pending_db(room_id: str, matrix_event_id: str, tool_name: str, tool_input: dict[str, Any]) -> None:
    async with session_scope() as s:
        s.add(PendingApproval(
            room_id=room_id, matrix_event_id=matrix_event_id,
            tool_name=tool_name, tool_input=tool_input,
        ))
        await s.commit()


async def _resolve_pending_db(matrix_event_id: str, decision: str, decided_by: str) -> None:
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


# --------- approval flow (existing behaviour) ---------


async def _approval_flow(
    room_id: str, tool_name: str, tool_input: dict[str, Any]
) -> PermissionResultAllow | PermissionResultDeny:
    if tool_name in _auto_allowed.get(room_id, set()):
        logger.info("auto-allow %s in %s", tool_name, room_id)
        return PermissionResultAllow(behavior="allow", updated_input=None, updated_permissions=None)

    plain, html = _format_input_for_approval(tool_name, tool_input)
    event_id = await outbox.send_markdown(room_id, plain, html, notice=True)
    if not event_id:
        logger.warning("approval: outbox failed; denying %s", tool_name)
        return PermissionResultDeny(behavior="deny", message="approval request could not be sent", interrupt=False)

    for emoji in ("✅", "❌", "🔁"):
        await outbox.react(room_id, event_id, emoji)
    await _record_pending_db(room_id, event_id, tool_name, tool_input)

    loop = asyncio.get_running_loop()
    fut: asyncio.Future[dict[str, Any]] = loop.create_future()
    pending = Pending(room_id=room_id, kind="approval", future=fut)
    _register_pending(event_id, pending)

    try:
        result = await asyncio.wait_for(fut, timeout=APPROVAL_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        await outbox.send_text(room_id, f"⌛ approval for **{tool_name}** timed out — denied", notice=True)
        await _resolve_pending_db(event_id, "timeout", "")
        return PermissionResultDeny(behavior="deny", message="approval timed out", interrupt=False)
    finally:
        _unregister_pending(pending, [event_id])

    decision = result["decision"]
    decided_by = result.get("decided_by", "")
    await _resolve_pending_db(event_id, decision, decided_by)
    await audit("tool_decision", room_id=room_id, actor=decided_by, tool=tool_name, decision=decision)

    if decision == "allow_always":
        _auto_allowed.setdefault(room_id, set()).add(tool_name)
        await outbox.send_text(room_id, f"🔁 will auto-allow **{tool_name}** until `!clear`", notice=True)
        return PermissionResultAllow(behavior="allow", updated_input=None, updated_permissions=None)
    if decision == "allow":
        return PermissionResultAllow(behavior="allow", updated_input=None, updated_permissions=None)
    return PermissionResultDeny(behavior="deny", message=f"denied by {decided_by or 'user'}", interrupt=False)


# --------- AskUserQuestion ---------


def _render_question(q: dict[str, Any], multi_select_hint: bool) -> tuple[str, str]:
    question = q.get("question", "")
    header = q.get("header", "")
    options = q.get("options") or []

    lines_plain = [f"**❓ {question}**"]
    lines_html = [f"<b>❓ {_html_escape(question)}</b>"]
    if header:
        lines_plain.append(f"_({header})_")
        lines_html.append(f"<i>({_html_escape(header)})</i>")

    for i, opt in enumerate(options[:9], start=1):
        digit = _DIGIT_EMOJI[i - 1]
        label = opt.get("label", "?")
        desc = opt.get("description", "")
        lines_plain.append(f"\n{digit} **{label}**")
        lines_html.append(f"<br>{digit} <b>{_html_escape(label)}</b>")
        if desc:
            lines_plain.append(f"    {desc}")
            lines_html.append(f"<br><i>{_html_escape(desc)}</i>")

    lines_plain.append(f"\n{_OTHER_EMOJI} Other (reply with free text)")
    lines_html.append(f"<br>{_OTHER_EMOJI} Other (reply with free text)")

    if multi_select_hint:
        lines_plain.append(f"\n_Multi-select: react to each choice, then {_DONE_EMOJI} when done._")
        lines_html.append(f"<br><i>Multi-select: react to each choice, then {_DONE_EMOJI} when done.</i>")
    else:
        lines_plain.append("\n_React with a digit OR reply with the label / number / free text._")
        lines_html.append("<br><i>React with a digit OR reply with the label / number / free text.</i>")

    return "\n".join(lines_plain), "".join(lines_html)


async def handle_ask_user_question(
    room_id: str, tool_input: dict[str, Any]
) -> PermissionResultAllow | PermissionResultDeny:
    """Post AskUserQuestion prompts, collect answers via reactions or text."""
    questions = tool_input.get("questions") or []
    if not questions:
        return PermissionResultDeny(behavior="deny", message="empty questions", interrupt=False)

    answers: dict[str, str] = {}

    for q in questions:
        multi = bool(q.get("multiSelect"))
        options = q.get("options") or []
        plain, html = _render_question(q, multi)
        event_id = await outbox.send_markdown(room_id, plain, html, notice=True)
        if not event_id:
            return PermissionResultDeny(behavior="deny", message="could not post question", interrupt=False)

        # Reaction buttons: digits + Other. For multi-select we also add ✅ for "done".
        for i, _ in enumerate(options[:9]):
            await outbox.react(room_id, event_id, _DIGIT_EMOJI[i])
        await outbox.react(room_id, event_id, _OTHER_EMOJI)
        if multi:
            await outbox.react(room_id, event_id, _DONE_EMOJI)

        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        pending = Pending(
            room_id=room_id,
            kind="question",
            future=fut,
            options=list(options),
            multi_select=multi,
            question_text=q.get("question", ""),
        )
        _register_pending(event_id, pending)

        try:
            result = await asyncio.wait_for(fut, timeout=QUESTION_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            await outbox.send_text(room_id, "⌛ question timed out", notice=True)
            return PermissionResultDeny(behavior="deny", message="question timed out", interrupt=False)
        finally:
            _unregister_pending(pending, [event_id])

        answer_label = result.get("answer", "")
        answers[q.get("question", "")] = answer_label
        await audit(
            "question_answered",
            room_id=room_id,
            actor=result.get("decided_by", ""),
            question=q.get("question", "")[:200],
            answer=answer_label[:200],
        )

    return PermissionResultAllow(
        behavior="allow",
        updated_input={
            "questions": questions,
            "answers": answers,
        },
        updated_permissions=None,
    )


# --------- ExitPlanMode ---------


def _chunk_markdown(text: str, size: int = 3500) -> list[str]:
    """Split a long markdown blob into Element-friendly chunks at line boundaries
    when possible, so nothing gets truncated."""
    if len(text) <= size:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > size:
        cut = remaining.rfind("\n", 0, size)
        if cut < size // 2:  # no good break — hard cut
            cut = size
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


async def handle_exit_plan_mode(
    room_id: str, tool_input: dict[str, Any]
) -> PermissionResultAllow | PermissionResultDeny:
    """Post the full plan (multi-message if needed), wait for approval."""
    plan_md = str(tool_input.get("plan") or "")
    if not plan_md.strip():
        return PermissionResultDeny(behavior="deny", message="empty plan", interrupt=False)

    header_plain = "📋 **Plan ready — review below**"
    header_html = "📋 <b>Plan ready — review below</b>"
    await outbox.send_markdown(room_id, header_plain, header_html, notice=True)

    # Post every chunk of the plan as a normal (non-notice) message so it renders
    # like Claude wrote it. NO truncation.
    posted_ids: list[str] = []
    for chunk in _chunk_markdown(plan_md):
        # Very naive markdown → html: preserve newlines. Element renders md in body too.
        html = _html_escape(chunk).replace("\n", "<br>")
        eid = await outbox.send_markdown(room_id, chunk, html)
        if eid:
            posted_ids.append(eid)

    # Final ask-message that carries the reactions
    ask_plain = (
        "✅ approve — manual (default mode) · 🚀 approve — auto mode · "
        "❌ reject · or reply with feedback to iterate"
    )
    ask_html = ask_plain
    ask_id = await outbox.send_markdown(room_id, ask_plain, ask_html, notice=True)
    if not ask_id:
        return PermissionResultDeny(behavior="deny", message="could not post approval prompt", interrupt=False)

    for emoji in ("✅", "🚀", "❌"):
        await outbox.react(room_id, ask_id, emoji)

    loop = asyncio.get_running_loop()
    fut: asyncio.Future[dict[str, Any]] = loop.create_future()
    pending = Pending(room_id=room_id, kind="plan", future=fut)
    _register_pending(ask_id, pending)
    # Any of the plan chunks also count for reactions
    for eid in posted_ids:
        _pending_by_event[eid] = pending

    try:
        result = await asyncio.wait_for(fut, timeout=PLAN_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        await outbox.send_text(room_id, "⌛ plan approval timed out", notice=True)
        return PermissionResultDeny(behavior="deny", message="plan approval timed out", interrupt=False)
    finally:
        _unregister_pending(pending, [ask_id, *posted_ids])

    decision = result.get("decision")
    decided_by = result.get("decided_by", "")
    next_mode = result.get("next_mode", "default")
    await audit(
        "plan_decision", room_id=room_id, actor=decided_by, decision=decision or "?", next_mode=next_mode
    )

    if decision == "allow":
        # Switch the room out of `plan` mode into whatever the user picked
        # (manual/default or auto) — keeps DB and the live SDK client in sync.
        from app.agent.session import set_room_mode

        await set_room_mode(room_id, next_mode)
        return PermissionResultAllow(behavior="allow", updated_input=None, updated_permissions=None)
    feedback = result.get("feedback", "")
    msg = f"user rejected: {feedback}" if feedback else "user rejected the plan"
    return PermissionResultDeny(behavior="deny", message=msg, interrupt=False)


# --------- entry point wired into ClaudeAgentOptions.can_use_tool ---------


def make_can_use_tool(room_id: str):
    """Returns a canUseTool callback bound to a specific room."""

    async def can_use_tool(
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        if tool_name == "AskUserQuestion":
            return await handle_ask_user_question(room_id, tool_input)
        if tool_name == "ExitPlanMode":
            return await handle_exit_plan_mode(room_id, tool_input)
        return await _approval_flow(room_id, tool_name, tool_input)

    return can_use_tool


# --------- reaction / text answer routing ---------


async def handle_reaction_event(target_event_id: str, key: str, sender: str) -> bool:
    """Resolve a pending future from a matrix reaction. Returns True if consumed."""
    pending = _pending_by_event.get(target_event_id)
    if pending is None or pending.future.done():
        return False

    key_stripped = key.strip()
    key_norm = key_stripped.lower()

    if pending.kind == "approval":
        if key in _ALLOW or key_norm in _ALLOW:
            pending.future.set_result({"decision": "allow", "decided_by": sender})
            return True
        if key in _DENY or key_norm in _DENY:
            pending.future.set_result({"decision": "deny", "decided_by": sender})
            return True
        if key in _ALLOW_ALWAYS or key_norm in _ALLOW_ALWAYS:
            pending.future.set_result({"decision": "allow_always", "decided_by": sender})
            return True
        return False

    if pending.kind == "plan":
        if key in _ALLOW or key_norm in _ALLOW:
            pending.future.set_result({"decision": "allow", "decided_by": sender, "next_mode": "default"})
            return True
        if key in _ALLOW_AUTO or key_norm in _ALLOW_AUTO:
            pending.future.set_result({"decision": "allow", "decided_by": sender, "next_mode": "auto"})
            return True
        if key in _DENY or key_norm in _DENY:
            pending.future.set_result({"decision": "deny", "decided_by": sender})
            return True
        return False

    if pending.kind == "question":
        # Digit → single-select answer (or multi-select toggle)
        if key_stripped in _DIGIT_EMOJI:
            idx = _DIGIT_EMOJI.index(key_stripped)
            if idx >= len(pending.options):
                return False
            if pending.multi_select:
                pending.selected.add(idx)
                # Don't resolve yet; wait for the ✅ done button
                return True
            label = pending.options[idx].get("label", "?")
            pending.future.set_result({"answer": label, "decided_by": sender})
            return True
        if key_stripped == _OTHER_EMOJI:
            # User will follow up with free text; don't resolve yet — but signal
            # in the log that we expect text.
            logger.info("question in %s: user chose Other, awaiting text", pending.room_id)
            return True
        if pending.multi_select and (key in _ALLOW or key_norm in _ALLOW):
            if not pending.selected:
                return False
            labels = ", ".join(pending.options[i].get("label", "?") for i in sorted(pending.selected))
            pending.future.set_result({"answer": labels, "decided_by": sender})
            return True
        return False

    return False


async def handle_text_answer(room_id: str, text: str, sender: str) -> bool:
    """Resolve the room's newest still-open pending using a free-text reply.
    Returns True if consumed."""
    pending = get_active_pending(room_id)
    if pending is None:
        return False

    text_stripped = text.strip()
    text_norm = text_stripped.lower()

    if pending.kind == "approval":
        if text_norm in _ALLOW:
            pending.future.set_result({"decision": "allow", "decided_by": sender})
            return True
        if text_norm in _DENY:
            pending.future.set_result({"decision": "deny", "decided_by": sender})
            return True
        if text_norm in _ALLOW_ALWAYS:
            pending.future.set_result({"decision": "allow_always", "decided_by": sender})
            return True
        return False

    if pending.kind == "plan":
        if text_norm in _ALLOW:
            pending.future.set_result({"decision": "allow", "decided_by": sender, "next_mode": "default"})
            return True
        if text_norm in _ALLOW_AUTO:
            pending.future.set_result({"decision": "allow", "decided_by": sender, "next_mode": "auto"})
            return True
        if text_norm in _DENY:
            pending.future.set_result({"decision": "deny", "decided_by": sender, "feedback": ""})
            return True
        # anything else = iteration feedback
        pending.future.set_result({"decision": "deny", "decided_by": sender, "feedback": text_stripped})
        return True

    if pending.kind == "question":
        # Digit "1", "2", ...
        if text_stripped.isdigit():
            idx = int(text_stripped) - 1
            if 0 <= idx < len(pending.options):
                label = pending.options[idx].get("label", "?")
                pending.future.set_result({"answer": label, "decided_by": sender})
                return True
        # Label match (case-insensitive)
        for opt in pending.options:
            if opt.get("label", "").strip().lower() == text_norm:
                pending.future.set_result({"answer": opt["label"], "decided_by": sender})
                return True
        # Anything else = free text answer ("Other")
        pending.future.set_result({"answer": text_stripped, "decided_by": sender})
        return True

    return False
