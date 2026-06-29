from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
)
from claude_agent_sdk.types import TextBlock, ThinkingBlock, ToolResultBlock, ToolUseBlock

from app.agent.modes import sdk_mode
from app.agent.permissions import clear_auto_allowed, make_can_use_tool
from app.config import settings
from app.matrix import outbox
from app.rooms.state import get_room, upsert_room

logger = logging.getLogger(__name__)


@dataclass
class RoomSession:
    room_id: str
    cwd: str
    model: str
    mode: str  # elementclaude mode (default/acceptEdits/plan/auto)
    claude_session_id: str | None = None
    client: ClaudeSDKClient | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    current_task: asyncio.Task | None = None
    total_cost_usd: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    turns: int = 0


def get_session_snapshot(room_id: str) -> RoomSession | None:
    """Return the in-memory session if present. Used by !status."""
    return _sessions.get(room_id)


_sessions: dict[str, RoomSession] = {}
_sessions_lock = asyncio.Lock()


async def _get_session(room_id: str) -> RoomSession | None:
    """Get or create the in-memory session record for a room. Returns None if cwd unset."""
    async with _sessions_lock:
        sess = _sessions.get(room_id)
        room = await get_room(room_id)
        if room is None or not room.cwd:
            return None

        if sess is None:
            sess = RoomSession(
                room_id=room_id,
                cwd=room.cwd,
                model=room.model,
                mode=room.mode,
                claude_session_id=room.claude_session_id,
            )
            _sessions[room_id] = sess
        else:
            # Reconfigure if room state changed (cwd / model / mode).
            if sess.cwd != room.cwd:
                # cwd change forces a fresh client — sessions are bound to a cwd.
                await _disconnect(sess)
                sess.cwd = room.cwd
                sess.claude_session_id = None
            if sess.model != room.model and sess.client is not None:
                try:
                    await sess.client.set_model(room.model)
                except Exception:
                    logger.exception("set_model failed; will reconnect on next prompt")
                    await _disconnect(sess)
                sess.model = room.model
            if sess.mode != room.mode and sess.client is not None:
                try:
                    await sess.client.set_permission_mode(sdk_mode(room.mode))
                except Exception:
                    logger.exception("set_permission_mode failed; will reconnect on next prompt")
                    await _disconnect(sess)
                sess.mode = room.mode

        return sess


async def _build_client(sess: RoomSession) -> ClaudeSDKClient:
    # Forward ANTHROPIC_API_KEY (always) and ANTHROPIC_BASE_URL (if set) into
    # the spawned `claude` CLI so a proxy override is honored.
    env: dict[str, str] = {"ANTHROPIC_API_KEY": settings.anthropic_api_key}
    if settings.anthropic_base_url:
        env["ANTHROPIC_BASE_URL"] = settings.anthropic_base_url

    options = ClaudeAgentOptions(
        cwd=sess.cwd,
        model=sess.model,
        permission_mode=sdk_mode(sess.mode),
        resume=sess.claude_session_id,
        skills="all",
        setting_sources=["user", "project"],
        can_use_tool=make_can_use_tool(sess.room_id),
        env=env,
    )
    client = ClaudeSDKClient(options=options)
    await client.connect()
    logger.info(
        "agent connected room=%s cwd=%s model=%s mode=%s resume=%s",
        sess.room_id, sess.cwd, sess.model, sess.mode, sess.claude_session_id,
    )
    return client


async def _disconnect(sess: RoomSession) -> None:
    if sess.client is not None:
        try:
            await sess.client.disconnect()
        except Exception:
            logger.exception("disconnect failed for room=%s", sess.room_id)
        sess.client = None


async def resume_room(room_id: str, session_id: str) -> None:
    """Attach to an existing Claude session by ID. Disconnect any current client."""
    async with _sessions_lock:
        sess = _sessions.pop(room_id, None)
    if sess:
        if sess.current_task and not sess.current_task.done():
            sess.current_task.cancel()
        await _disconnect(sess)
    await upsert_room(room_id, claude_session_id=session_id)


async def clear_room(room_id: str) -> None:
    async with _sessions_lock:
        sess = _sessions.pop(room_id, None)
    if sess:
        if sess.current_task and not sess.current_task.done():
            sess.current_task.cancel()
        await _disconnect(sess)
    clear_auto_allowed(room_id)
    await upsert_room(room_id, claude_session_id=None)


async def cancel_room(room_id: str) -> bool:
    """Try to interrupt the current run in this room. Returns True if something was running."""
    sess = _sessions.get(room_id)
    if not sess or sess.current_task is None or sess.current_task.done():
        return False
    if sess.client is not None:
        try:
            await sess.client.interrupt()
        except Exception:
            logger.exception("interrupt failed")
    sess.current_task.cancel()
    return True


def _format_tool_use(name: str, tool_input: dict[str, Any]) -> tuple[str, str]:
    """Render a ToolUseBlock as (plain, html) for Matrix."""
    if name == "Bash":
        cmd = tool_input.get("command", "")
        plain = f"🔧 Bash: {cmd[:300]}"
        html = f"🔧 <b>Bash</b><pre><code>{cmd[:1000]}</code></pre>"
        return plain, html
    if name in ("Read", "Edit", "Write"):
        fp = tool_input.get("file_path") or tool_input.get("path") or ""
        plain = f"🔧 {name}: {fp}"
        html = f"🔧 <b>{name}</b> <code>{fp}</code>"
        return plain, html
    if name in ("Glob", "Grep"):
        pat = tool_input.get("pattern") or tool_input.get("query") or ""
        plain = f"🔧 {name}: {pat}"
        html = f"🔧 <b>{name}</b> <code>{pat}</code>"
        return plain, html
    # generic
    keys = ", ".join(list(tool_input.keys())[:3])
    plain = f"🔧 {name} ({keys})"
    html = f"🔧 <b>{name}</b> <i>({keys})</i>"
    return plain, html


def _format_text(text: str) -> tuple[str, str]:
    """Plain + minimal HTML rendering of a TextBlock."""
    # naive: rely on Element rendering plain markdown-ish; HTML keeps newlines as <br>.
    safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    html = safe.replace("\n", "<br>")
    return text, html


async def _run_prompt(room_id: str, sess: RoomSession, prompt: str) -> None:
    if sess.client is None:
        sess.client = await _build_client(sess)

    await sess.client.query(prompt)

    text_buf: list[str] = []

    async def flush_text() -> None:
        if not text_buf:
            return
        joined = "".join(text_buf).strip()
        text_buf.clear()
        if joined:
            plain, html = _format_text(joined)
            await outbox.send_markdown(room_id, plain, html)

    async for msg in sess.client.receive_response():
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    text_buf.append(block.text)
                elif isinstance(block, ToolUseBlock):
                    await flush_text()
                    plain, html = _format_tool_use(block.name, block.input)
                    await outbox.send_markdown(room_id, plain, html, notice=True)
                elif isinstance(block, ThinkingBlock):
                    # Don't spam thinking into the room.
                    pass
                elif isinstance(block, ToolResultBlock):
                    # Tool results in an assistant message are rare — skip.
                    pass
        elif isinstance(msg, ResultMessage):
            await flush_text()
            new_id = msg.session_id
            if new_id and new_id != sess.claude_session_id:
                sess.claude_session_id = new_id
                await upsert_room(room_id, claude_session_id=new_id)
            if msg.total_cost_usd:
                sess.total_cost_usd += float(msg.total_cost_usd)
            usage = msg.usage or {}
            sess.total_input_tokens += int(usage.get("input_tokens") or 0)
            sess.total_output_tokens += int(usage.get("output_tokens") or 0)
            sess.turns += 1
            if msg.is_error:
                await outbox.send_text(
                    room_id,
                    f"⚠️ run ended with error (stop_reason={msg.stop_reason})",
                    notice=True,
                )

    await flush_text()


async def handle_prompt(room_id: str, sender_id: str, prompt: str) -> None:
    room = await get_room(room_id)
    if room is None:
        await outbox.send_text(room_id, "no room state — try `!auth add` first", notice=True)
        return
    if not room.cwd:
        hint = settings.workspace_root if settings.workspace_root else "~/some-project"
        await outbox.send_text(
            room_id,
            f"set a working directory first: `!cwd {hint}`",
            notice=True,
        )
        return

    sess = await _get_session(room_id)
    if sess is None:
        await outbox.send_text(room_id, "could not start session", notice=True)
        return

    if sess.lock.locked():
        await outbox.send_text(
            room_id,
            "⏳ a run is already in progress — use `!cancel` to stop it",
            notice=True,
        )
        return

    async with sess.lock:
        sess.current_task = asyncio.current_task()
        try:
            await _run_prompt(room_id, sess, prompt)
        except asyncio.CancelledError:
            await outbox.send_text(room_id, "🛑 cancelled", notice=True)
            # Don't re-raise — we want the room to stay usable.
        except Exception:
            logger.exception("agent run crashed for room=%s", room_id)
            await outbox.send_text(room_id, "❌ run crashed — check container logs", notice=True)
        finally:
            sess.current_task = None
