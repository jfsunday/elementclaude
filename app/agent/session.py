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


_stderr_buffer: dict[str, list[str]] = {}


def _make_stderr_sink(room_id: str):
    def sink(line: str) -> None:
        buf = _stderr_buffer.setdefault(room_id, [])
        buf.append(line)
        # keep only last 40 lines to avoid unbounded growth
        if len(buf) > 40:
            del buf[: len(buf) - 40]
        # Also surface to journal for post-mortem
        logger.warning("claude cli stderr [%s]: %s", room_id, line.rstrip())

    return sink


async def _load_hooks_for_room(room_id: str):
    """Build a claude-agent-sdk hooks dict from the room's enabled RoomHook rows.
    Each hook fires a user-provided shell command; SDK ignores anything the
    hook returns beyond the empty JSON output."""
    import asyncio as _asyncio

    from claude_agent_sdk.types import HookMatcher
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import RoomHook

    async with session_scope() as s:
        rows = list((await s.scalars(
            select(RoomHook).where(
                RoomHook.room_id == room_id, RoomHook.enabled.is_(True)
            )
        )).all())

    if not rows:
        return None

    def make_cb(command: str, event: str):
        async def cb(hook_input, tool_use_id, context):  # noqa: ANN001
            try:
                proc = await _asyncio.create_subprocess_shell(
                    command,
                    stdout=_asyncio.subprocess.DEVNULL,
                    stderr=_asyncio.subprocess.DEVNULL,
                )
                try:
                    await _asyncio.wait_for(proc.wait(), timeout=30.0)
                except _asyncio.TimeoutError:
                    proc.kill()
                    logger.warning("hook %s for room=%s timed out", event, room_id)
            except Exception:
                logger.exception("hook %s crashed (cmd=%s)", event, command[:120])
            return {}
        return cb

    hooks: dict[str, list[HookMatcher]] = {}
    for row in rows:
        hooks.setdefault(row.event, []).append(
            HookMatcher(matcher=None, hooks=[make_cb(row.command, row.event)])
        )
    return hooks


async def _build_client(sess: RoomSession, *, resume: str | None) -> ClaudeSDKClient:
    # Forward ANTHROPIC_API_KEY (always) and ANTHROPIC_BASE_URL (if set) into
    # the spawned `claude` CLI so a proxy override is honored.
    env: dict[str, str] = {"ANTHROPIC_API_KEY": settings.anthropic_api_key}
    if settings.anthropic_base_url:
        env["ANTHROPIC_BASE_URL"] = settings.anthropic_base_url

    hooks = await _load_hooks_for_room(sess.room_id)

    _stderr_buffer[sess.room_id] = []
    options = ClaudeAgentOptions(
        cwd=sess.cwd,
        model=sess.model,
        permission_mode=sdk_mode(sess.mode),
        resume=resume,
        skills="all",
        setting_sources=["user", "project"],
        can_use_tool=make_can_use_tool(sess.room_id),
        env=env,
        stderr=_make_stderr_sink(sess.room_id),
        hooks=hooks,
    )
    client = ClaudeSDKClient(options=options)
    await client.connect()
    logger.info(
        "agent connected room=%s cwd=%s model=%s mode=%s resume=%s",
        sess.room_id, sess.cwd, sess.model, sess.mode, resume,
    )
    return client


async def _build_client_with_resume_recovery(sess: RoomSession) -> ClaudeSDKClient:
    """Build the client; if the requested resume session is gone from the local
    store, drop the stale id and retry with a fresh session so the room isn't
    stuck in a crash loop."""
    try:
        return await _build_client(sess, resume=sess.claude_session_id)
    except Exception as exc:
        stderr = "\n".join(_stderr_buffer.get(sess.room_id, []))
        stale_resume = bool(sess.claude_session_id) and (
            "No conversation found" in stderr
            or "Session not found" in stderr
            or "session id" in stderr.lower() and sess.claude_session_id in stderr
        )
        if not stale_resume:
            raise
        logger.warning(
            "resume session %s missing for room %s — retrying without resume",
            sess.claude_session_id, sess.room_id,
        )
        await outbox.send_text(
            sess.room_id,
            f"⚠️ previous session `{sess.claude_session_id[:8]}…` was gone from Claude's store — starting a fresh session.",
            notice=True,
        )
        sess.claude_session_id = None
        await upsert_room(sess.room_id, claude_session_id=None)
        return await _build_client(sess, resume=None)


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
    await upsert_room(room_id, claude_session_id=None, mode=settings.default_mode)


async def set_room_mode(room_id: str, mode: str) -> None:
    """Update the room's mode in the DB and, if a live session/client exists,
    switch the SDK client's permission mode too — so an in-flight run (e.g.
    right after a plan approval) doesn't stay stuck in the old mode until the
    process reconnects."""
    await upsert_room(room_id, mode=mode)
    sess = _sessions.get(room_id)
    if sess is None:
        return
    sess.mode = mode
    if sess.client is not None:
        try:
            await sess.client.set_permission_mode(sdk_mode(mode))
        except Exception:
            logger.exception("set_permission_mode failed in set_room_mode; will reconnect on next prompt")
            await _disconnect(sess)


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


# Rooms already told that TTS is broken — the warning is worth saying once, not
# after every single answer.
_tts_warned: set[str] = set()


async def _maybe_speak(room_id: str, text: str) -> None:
    """Best-effort: read the final assistant answer back as an m.audio message."""
    if not text.strip():
        return
    room = await get_room(room_id)
    if room is None or not room.tts_enabled:
        return
    try:
        from app.voice import tts

        local = room.voice_engine == "local"
        reason = tts.unavailable_reason(local=local)
        path = None if reason else await tts.synthesize(text, local=local, voice=room.tts_voice)
        if path is None:
            if room_id not in _tts_warned:
                _tts_warned.add(room_id)
                detail = reason or "synthesis failed — see the logs"
                await outbox.send_text(
                    room_id,
                    f"⚠️ tts is on but silent: {detail}. `!voice tts off` to stop trying.",
                    notice=True,
                )
            return
        _tts_warned.discard(room_id)
        await outbox.send_media(room_id, str(path), msgtype="m.audio")
    except Exception:
        logger.exception("TTS failed for room=%s", room_id)


async def _run_prompt(room_id: str, sess: RoomSession, prompt: str) -> None:
    if sess.client is None:
        sess.client = await _build_client_with_resume_recovery(sess)

    # If images/files were dropped into the room since the last prompt, prepend
    # their paths so Claude reads them via its Read tool.
    from app.agent.attachments import format_prompt_with_attachments, pop_attachments

    attachments = await pop_attachments(room_id)
    prompt_for_claude = format_prompt_with_attachments(prompt, attachments)
    await sess.client.query(prompt_for_claude)

    # One live-updating Matrix message per assistant text stream. Every text
    # chunk appends to `stream_text` and re-edits the same event, throttled to
    # 1 edit per 1.2s so the homeserver doesn't groan.
    stream_event_id: str | None = None
    stream_text = ""
    last_edit_time = 0.0
    EDIT_MIN_INTERVAL = 1.2
    # Last completed text stream of the run — the bit worth speaking aloud.
    final_text = ""

    async def flush_stream(final: bool = False) -> None:
        nonlocal stream_event_id, stream_text, last_edit_time, final_text
        if not stream_text.strip():
            if final:
                stream_event_id = None
                stream_text = ""
            return
        plain, html = _format_text(stream_text)
        now = asyncio.get_running_loop().time()
        if stream_event_id is None:
            stream_event_id = await outbox.send_markdown(room_id, plain, html)
            last_edit_time = now
        else:
            if final or (now - last_edit_time) >= EDIT_MIN_INTERVAL:
                await outbox.edit_markdown(room_id, stream_event_id, plain, html)
                last_edit_time = now
        if final:
            final_text = stream_text
            stream_event_id = None
            stream_text = ""

    typing_task = asyncio.create_task(outbox.typing_keepalive(room_id))

    try:
        async for msg in sess.client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        stream_text += block.text
                        await flush_stream()
                    elif isinstance(block, ToolUseBlock):
                        await flush_stream(final=True)
                        plain, html = _format_tool_use(block.name, block.input)
                        await outbox.send_markdown(room_id, plain, html, notice=True)
                    elif isinstance(block, ThinkingBlock):
                        pass  # don't spam thinking into the room
                    elif isinstance(block, ToolResultBlock):
                        pass
            elif isinstance(msg, ResultMessage):
                await flush_stream(final=True)
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

        await flush_stream(final=True)
        await _maybe_speak(room_id, final_text)
    finally:
        typing_task.cancel()
        try:
            await typing_task
        except (asyncio.CancelledError, Exception):
            pass


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
            # Shield the outbox call from cancellation so the message actually gets sent
            try:
                await asyncio.shield(outbox.send_text(room_id, "🛑 cancelled", notice=True))
            except asyncio.CancelledError:
                pass  # Ignore if shield itself is cancelled
            # Don't re-raise — we want the room to stay usable.
        except Exception:
            logger.exception("agent run crashed for room=%s", room_id)
            tail = "\n".join(_stderr_buffer.get(room_id, [])[-5:]).strip()
            hint = f"\n```\n{tail}\n```" if tail else ""
            try:
                await asyncio.shield(outbox.send_text(
                    room_id,
                    f"❌ run crashed — `./run-host.sh --logs` for full trace{hint}",
                    notice=True,
                ))
            except asyncio.CancelledError:
                pass
        finally:
            sess.current_task = None
