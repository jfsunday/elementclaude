from __future__ import annotations

import asyncio
import importlib.util
import logging
import re
import shutil
import uuid
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)


_FENCED_CODE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`]*)`")
_URL = re.compile(r"https?://\S+")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_MD_BULLET = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)
_MD_EMPHASIS = re.compile(r"(\*{1,3}|~~)(?=\S)(.+?)(?<=\S)\1", re.DOTALL)
# `_` only counts as emphasis when it wraps a single word from the outside — this is a
# coding assistant, so `snake_case` identifiers and `__dunder__` names must survive intact.
_MD_UNDERSCORE = re.compile(r"(?<!\w)_(?=\S)([^_\n]+?)(?<=\S)_(?!\w)")
_BLANK_LINES = re.compile(r"\n{2,}")
_SPACES = re.compile(r"[ \t]{2,}")


def speakable(text: str, *, max_chars: int | None = None) -> str:
    """Turn assistant markdown into something worth listening to: no code blocks,
    no URLs, no markdown noise, truncated to a sane length."""
    limit = settings.tts_max_chars if max_chars is None else max_chars

    out = _FENCED_CODE.sub(" ", text)
    out = _MD_LINK.sub(r"\1", out)
    out = _URL.sub(" ", out)
    out = _INLINE_CODE.sub(r"\1", out)
    out = _MD_HEADING.sub("", out)
    out = _MD_BULLET.sub("", out)
    out = _MD_EMPHASIS.sub(r"\2", out)
    out = _MD_UNDERSCORE.sub(r"\1", out)
    out = _BLANK_LINES.sub("\n", out)
    out = _SPACES.sub(" ", out)
    out = "\n".join(line.strip() for line in out.splitlines())
    out = out.strip()

    if limit > 0 and len(out) > limit:
        head = out[:limit]
        cut = max(head.rfind(". "), head.rfind("! "), head.rfind("? "), head.rfind("\n"))
        if cut < limit // 2:
            cut = head.rfind(" ")
        if cut > 0:
            head = head[:cut]
        out = head.rstrip(" .,;:\n") + " …"
    return out


def unavailable_reason(*, local: bool) -> str | None:
    """Why TTS cannot run right now, phrased for the room. None = good to go."""
    if local:
        model = settings.piper_model_path
        if (model is not None and model.is_file() and shutil.which("piper")) or shutil.which(
            "espeak-ng"
        ):
            return None
        return "local TTS needs `espeak-ng` on PATH (or `piper` + `PIPER_MODEL_PATH`)"
    if importlib.util.find_spec("edge_tts") is None:
        return (
            "`edge-tts` is not installed — install the `voice` extra "
            "(`EXTRAS=voice ./run-host.sh`, or rebuild the image)"
        )
    return None


def _tts_dir() -> Path:
    d = settings.data_dir / "tts"
    d.mkdir(parents=True, exist_ok=True)
    return d


_KEEP_FILES = 50


def _prune(directory: Path) -> None:
    """Every answer produces an audio file — keep only the most recent ones.

    Best effort: two rooms answering at once can make a file vanish mid-sort, and
    housekeeping must never cost the caller its freshly rendered audio.
    """
    try:
        files = sorted(directory.glob("*"), key=_mtime, reverse=True)
        for stale in files[_KEEP_FILES:]:
            stale.unlink(missing_ok=True)
    except OSError:
        logger.debug("pruning %s failed", directory, exc_info=True)


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


async def _synthesize_cloud(text: str, voice: str, dest: Path) -> Path | None:
    try:
        import edge_tts  # type: ignore[import-not-found]
    except ImportError:
        logger.warning("edge-tts not installed — install the `voice` extra for cloud TTS")
        return None

    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(str(dest))
    if not dest.is_file() or dest.stat().st_size == 0:
        logger.warning("edge-tts produced an empty file")
        return None
    return dest


async def _run(cmd: list[str], *, stdin: bytes | None = None) -> bool:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate(stdin)
    if proc.returncode != 0:
        logger.warning(
            "%s failed (%s): %s", cmd[0], proc.returncode, err.decode(errors="replace")[:400]
        )
        return False
    return True


async def _synthesize_local(text: str, dest: Path) -> Path | None:
    model = settings.piper_model_path
    if model is not None and model.is_file() and shutil.which("piper"):
        ok = await _run(
            ["piper", "--model", str(model), "--output_file", str(dest)],
            stdin=text.encode("utf-8"),
        )
    elif shutil.which("espeak-ng"):
        ok = await _run(["espeak-ng", "-w", str(dest), "--stdin"], stdin=text.encode("utf-8"))
    else:
        logger.warning("local TTS unavailable — install piper (+ PIPER_MODEL_PATH) or espeak-ng")
        return None

    if not ok or not dest.is_file() or dest.stat().st_size == 0:
        return None
    return dest


async def synthesize(text: str, *, local: bool, voice: str | None = None) -> Path | None:
    """Render `text` to an audio file. Returns the path, or None if unavailable."""
    body = speakable(text)
    if not body:
        return None

    directory = _tts_dir()
    suffix = ".wav" if local else ".mp3"
    dest = directory / f"{uuid.uuid4().hex}{suffix}"
    try:
        if local:
            result = await _synthesize_local(body, dest)
        else:
            result = await _synthesize_cloud(body, voice or settings.tts_voice, dest)
    except Exception:
        logger.exception("TTS failed (local=%s)", local)
        result = None

    if result is None:
        dest.unlink(missing_ok=True)
        return None
    _prune(directory)
    return result
