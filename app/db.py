from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


def _make_engine():
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    url = f"sqlite+aiosqlite:///{settings.db_path}"
    return create_async_engine(url, echo=False, future=True)


engine = _make_engine()
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


def _sql_literal(value: object) -> str | None:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("'", "''")
        return f"'{escaped}'"
    return None


def _add_column_ddl(table_name: str, column) -> str | None:  # noqa: ANN001
    """Render `ALTER TABLE … ADD COLUMN …` for a column SQLite doesn't have yet.
    Returns None when the column can't be added non-destructively (NOT NULL
    without a usable default)."""
    default = None
    if column.default is not None and not column.default.is_callable:
        default = _sql_literal(column.default.arg)

    parts = [f'"{column.name}"', column.type.compile(dialect=engine.dialect)]
    if not column.nullable:
        if default is None:
            return None
        parts.append("NOT NULL")
    if default is not None:
        parts.append(f"DEFAULT {default}")
    return f'ALTER TABLE "{table_name}" ADD COLUMN ' + " ".join(parts)


def _add_missing_columns(conn) -> None:  # noqa: ANN001 — sync connection from run_sync
    """`create_all` never touches existing tables, so an existing sqlite file keeps
    the old schema when we add model columns. Backfill them idempotently."""
    for table in Base.metadata.sorted_tables:
        rows = conn.exec_driver_sql(f"PRAGMA table_info('{table.name}')").fetchall()
        existing = {r[1] for r in rows}
        if not existing:
            continue
        for column in table.columns:
            if column.name in existing:
                continue
            ddl = _add_column_ddl(table.name, column)
            if ddl is None:
                logger.warning(
                    "cannot add column %s.%s automatically (NOT NULL without default)",
                    table.name, column.name,
                )
                continue
            conn.exec_driver_sql(ddl)
            logger.info("migrated: added column %s.%s", table.name, column.name)


async def init_db() -> None:
    from app import models  # noqa: F401 — register models

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_missing_columns)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
