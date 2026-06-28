from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.bootstrap import ensure_webhook_rule
from app.config import settings
from app.db import init_db
from app.matrix.inbox import router as inbox_router
from app.matrix.outbox import close_http as close_outbox_http
from app.rooms.auth import ensure_initial_admin


def _configure_logging() -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_logging()
    log = logging.getLogger("app.main")
    log.info("Starting elementclaude")

    await init_db()
    await ensure_initial_admin()
    await ensure_webhook_rule()

    try:
        yield
    finally:
        log.info("Shutting down elementclaude")
        await close_outbox_http()


app = FastAPI(
    title="elementclaude",
    description="Claude Code in a Matrix/Element room — bridged through messaging-bot.",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(inbox_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
