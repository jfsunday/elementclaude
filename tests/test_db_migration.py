from __future__ import annotations

import pytest

from app.db import engine, init_db

# Columns added to `rooms` after the first release — `create_all` alone never
# adds these to an existing sqlite file.
_NEW_ROOM_COLUMNS = {"stt_enabled", "tts_enabled", "voice_engine", "tts_voice"}

_OLD_ROOMS_SCHEMA = """
CREATE TABLE rooms (
    room_id VARCHAR(255) NOT NULL PRIMARY KEY,
    enabled BOOLEAN NOT NULL,
    mode VARCHAR(32) NOT NULL,
    model VARCHAR(64) NOT NULL,
    cwd TEXT,
    claude_session_id VARCHAR(64),
    created_at DATETIME,
    last_activity DATETIME
)
"""


async def _columns(table: str) -> set[str]:
    async with engine.begin() as conn:
        rows = await conn.exec_driver_sql(f"PRAGMA table_info('{table}')")
        return {r[1] for r in rows.fetchall()}


@pytest.fixture
async def legacy_db():
    """A sqlite file that still has the pre-voice `rooms` schema plus one row."""
    async with engine.begin() as conn:
        await conn.exec_driver_sql("DROP TABLE IF EXISTS rooms")
        await conn.exec_driver_sql(_OLD_ROOMS_SCHEMA)
        await conn.exec_driver_sql(
            "INSERT INTO rooms (room_id, enabled, mode, model, cwd) VALUES "
            "('!old:example.org', 1, 'plan', 'claude-opus-4-7', '/tmp/proj')"
        )
    yield
    async with engine.begin() as conn:
        await conn.exec_driver_sql("DROP TABLE IF EXISTS rooms")


async def test_adds_missing_columns_to_existing_table(legacy_db) -> None:
    assert _NEW_ROOM_COLUMNS & await _columns("rooms") == set()

    await init_db()

    assert _NEW_ROOM_COLUMNS <= await _columns("rooms")


async def test_migration_preserves_existing_rows(legacy_db) -> None:
    await init_db()

    async with engine.begin() as conn:
        rows = await conn.exec_driver_sql(
            "SELECT mode, cwd, stt_enabled, tts_enabled, voice_engine, tts_voice "
            "FROM rooms WHERE room_id = '!old:example.org'"
        )
        mode, cwd, stt, tts, voice_engine, tts_voice = rows.fetchone()

    assert (mode, cwd) == ("plan", "/tmp/proj")
    # Backfilled with the column defaults, not NULL.
    assert (stt, tts, voice_engine, tts_voice) == (0, 0, "cloud", None)


async def test_migration_is_idempotent(legacy_db) -> None:
    await init_db()
    before = await _columns("rooms")

    await init_db()
    await init_db()

    assert await _columns("rooms") == before
