from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import settings


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

    # DB init, bootstrap, matrix bridge, etc. land here in later phases.

    try:
        yield
    finally:
        log.info("Shutting down elementclaude")


app = FastAPI(
    title="elementclaude",
    description="Claude Code in a Matrix/Element room — bridged through messaging-bot.",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
