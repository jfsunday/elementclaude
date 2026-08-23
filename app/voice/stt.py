from __future__ import annotations

import asyncio
import importlib.util
import logging
import mimetypes
from pathlib import Path
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


def resolve_local(engine: str) -> tuple[bool, str | None]:
    """Decide whether STT runs offline. Returns (use_local, note_for_the_room).

    There is no keyless free Whisper API, so `engine cloud` without a configured
    token silently degrades to local instead of failing.
    """
    if engine == "local":
        return True, None
    if settings.stt_api_token:
        return False, None
    return True, "no `STT_API_TOKEN` set — transcribing locally"


# faster-whisper loads a multi-hundred-MB model; keep one per process and let only
# one transcription run at a time (it saturates the CPU anyway).
_model: Any = None
_local_lock = asyncio.Lock()


def unavailable_reason(*, local: bool) -> str | None:
    """Why STT cannot run right now, phrased for the room. None = good to go."""
    if not local:
        return None
    if importlib.util.find_spec("faster_whisper") is None:
        return "`faster-whisper` is not installed — start with `EXTRAS=voice ./run-host.sh`"
    return None


def model_loaded() -> bool:
    """False until the first local transcription has pulled the model into memory —
    that first run may also download a multi-GB model, so the room deserves a heads-up."""
    return _model is not None


def _load_model() -> Any:
    global _model
    if _model is None:
        from faster_whisper import WhisperModel  # type: ignore[import-not-found]

        logger.info("loading faster-whisper model %r", settings.stt_model)
        _model = WhisperModel(settings.stt_model, compute_type=settings.stt_compute_type)
    return _model


def _transcribe_blocking(path: Path) -> str:
    segments, _info = _load_model().transcribe(
        str(path), language=settings.stt_language, vad_filter=True
    )
    return " ".join(seg.text.strip() for seg in segments).strip()


async def _transcribe_local(path: Path) -> str | None:
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        logger.warning("faster-whisper not installed — install the `voice` extra for local STT")
        return None
    async with _local_lock:
        return await asyncio.to_thread(_transcribe_blocking, path)


def _extract_text(payload: Any) -> str | None:
    if isinstance(payload, dict):
        text = payload.get("text")
        return text.strip() if isinstance(text, str) else None
    if isinstance(payload, list) and payload:
        return _extract_text(payload[0])
    return None


async def _transcribe_cloud(path: Path) -> str | None:
    url = settings.stt_api_url
    headers = {"Authorization": f"Bearer {settings.stt_api_token}"}
    data = path.read_bytes()
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"

    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
        if url.rstrip("/").endswith("/audio/transcriptions"):
            # OpenAI-compatible endpoint (Groq, local whisper.cpp servers, …)
            form = {"model": settings.stt_api_model or settings.stt_model}
            resp = await client.post(
                url, headers=headers, files={"file": (path.name, data, mime)}, data=form
            )
        else:
            # HuggingFace inference API: raw audio body
            resp = await client.post(url, headers={**headers, "Content-Type": mime}, content=data)
        resp.raise_for_status()
        try:
            return _extract_text(resp.json())
        except ValueError:
            return resp.text.strip() or None


async def transcribe(path: Path, *, local: bool) -> str | None:
    """Transcribe an audio file. Returns the transcript, or None on failure."""
    try:
        text = await _transcribe_local(path) if local else await _transcribe_cloud(path)
    except Exception:
        logger.exception("STT failed (local=%s) for %s", local, path)
        return None
    return text or None
