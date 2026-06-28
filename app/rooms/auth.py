from __future__ import annotations

from sqlalchemy import select

from app.config import settings
from app.db import session_scope
from app.models import Admin


async def ensure_initial_admin() -> None:
    """Make sure INITIAL_ADMIN_USER from env is present in the admins table."""
    user_id = settings.initial_admin_user
    async with session_scope() as s:
        existing = await s.get(Admin, user_id)
        if existing is None:
            s.add(Admin(matrix_user_id=user_id))
            await s.commit()


async def is_admin(matrix_user_id: str) -> bool:
    async with session_scope() as s:
        return (await s.get(Admin, matrix_user_id)) is not None


async def list_admins() -> list[str]:
    async with session_scope() as s:
        rows = await s.scalars(select(Admin.matrix_user_id))
        return list(rows.all())


async def add_admin(matrix_user_id: str) -> bool:
    async with session_scope() as s:
        if await s.get(Admin, matrix_user_id):
            return False
        s.add(Admin(matrix_user_id=matrix_user_id))
        await s.commit()
        return True


async def remove_admin(matrix_user_id: str) -> bool:
    async with session_scope() as s:
        existing = await s.get(Admin, matrix_user_id)
        if existing is None:
            return False
        if matrix_user_id == settings.initial_admin_user:
            # don't let admins lock themselves out of the initial admin
            return False
        await s.delete(existing)
        await s.commit()
        return True
