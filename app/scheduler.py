from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from croniter import croniter
from sqlalchemy import select

from app.db import session_scope
from app.models import ScheduledTask

logger = logging.getLogger(__name__)

_task: asyncio.Task[None] | None = None
_stop = asyncio.Event()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(dt: datetime | None) -> datetime | None:
    """SQLite drops timezone info on read; re-anchor to UTC so we can compare
    against tz-aware `now()`."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def next_from(cron_expr: str, base: datetime | None = None) -> datetime:
    it = croniter(cron_expr, base or _now())
    return it.get_next(datetime)


def validate_cron(cron_expr: str) -> bool:
    try:
        croniter(cron_expr)
        return True
    except Exception:
        return False


async def _fire(task_row: ScheduledTask) -> None:
    """Inject a synthetic Matrix event into the dispatcher so the command runs
    through the exact same path an Element message would."""
    from app.matrix.dispatcher import handle_inbound

    synthetic = {
        "event_id": f"$sched-{uuid.uuid4()}",
        "event_type": "m.room.message",
        "room": {"id": task_row.room_id},
        "sender": {"id": "@scheduler:elementclaude.local"},
        "content": {"msgtype": "m.text", "body": task_row.command},
        "body": task_row.command,
    }
    try:
        await handle_inbound(synthetic)
    except Exception:
        logger.exception("scheduled task %s (%s) crashed", task_row.id, task_row.command[:60])


async def _tick() -> None:
    now = _now()
    async with session_scope() as s:
        rows = list((await s.scalars(
            select(ScheduledTask).where(ScheduledTask.enabled.is_(True))
        )).all())

        due: list[ScheduledTask] = []
        for row in rows:
            next_at = _as_aware(row.next_run_at)
            if next_at is None:
                row.next_run_at = next_from(row.cron_expr, now)
                continue
            if next_at <= now:
                due.append(row)
                row.last_run_at = now
                row.next_run_at = next_from(row.cron_expr, now)
        await s.commit()

    for row in due:
        await _fire(row)


async def _loop() -> None:
    logger.info("scheduler loop started")
    while not _stop.is_set():
        try:
            await _tick()
        except Exception:
            logger.exception("scheduler tick failed")
        try:
            await asyncio.wait_for(_stop.wait(), timeout=15.0)
        except asyncio.TimeoutError:
            pass
    logger.info("scheduler loop stopped")


async def start() -> None:
    global _task
    if _task is not None:
        return
    _stop.clear()
    _task = asyncio.create_task(_loop(), name="scheduler")


async def stop() -> None:
    global _task
    _stop.set()
    if _task is not None:
        try:
            await asyncio.wait_for(_task, timeout=5.0)
        except asyncio.TimeoutError:
            _task.cancel()
        _task = None


async def create(room_id: str, cron_expr: str, command: str, created_by: str) -> ScheduledTask:
    if not validate_cron(cron_expr):
        raise ValueError(f"invalid cron expression: {cron_expr!r}")
    async with session_scope() as s:
        task = ScheduledTask(
            room_id=room_id, cron_expr=cron_expr, command=command,
            created_by=created_by, next_run_at=next_from(cron_expr),
        )
        s.add(task)
        await s.commit()
        await s.refresh(task)
        return task


async def list_for_room(room_id: str) -> list[ScheduledTask]:
    async with session_scope() as s:
        rows = await s.scalars(
            select(ScheduledTask).where(ScheduledTask.room_id == room_id).order_by(ScheduledTask.created_at)
        )
        return list(rows.all())


async def find(room_id: str, id_prefix: str) -> ScheduledTask | None:
    async with session_scope() as s:
        rows = await s.scalars(
            select(ScheduledTask).where(ScheduledTask.room_id == room_id)
        )
        for row in rows.all():
            if row.id == id_prefix or row.id.startswith(id_prefix):
                return row
        return None


async def delete(task: ScheduledTask) -> None:
    async with session_scope() as s:
        obj = await s.get(ScheduledTask, task.id)
        if obj is not None:
            await s.delete(obj)
            await s.commit()


async def set_enabled(task: ScheduledTask, enabled: bool) -> None:
    async with session_scope() as s:
        obj = await s.get(ScheduledTask, task.id)
        if obj is not None:
            obj.enabled = enabled
            if enabled and obj.next_run_at is None:
                obj.next_run_at = next_from(obj.cron_expr)
            await s.commit()
