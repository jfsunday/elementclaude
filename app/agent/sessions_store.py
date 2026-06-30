from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


CLAUDE_HOME = Path(os.environ.get("CLAUDE_HOME", Path.home() / ".claude"))


@dataclass
class SessionInfo:
    session_id: str
    cwd: str
    mtime: float
    first_user_text: str | None


def encode_cwd(cwd: str | Path) -> str:
    """Mirrors how Claude Code encodes a cwd into a project-store dir name:
    every '/' in an absolute path becomes '-' (so /home/js/foo → -home-js-foo).
    """
    s = str(Path(cwd).resolve())
    return s.replace("/", "-")


_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _looks_like_session_id(name: str) -> bool:
    stem = name[:-6] if name.endswith(".jsonl") else name
    return bool(_UUID_RE.match(stem))


def _extract_first_user_text(path: Path) -> str | None:
    """Best-effort: read the first plain-text user message out of a session file.
    Tolerates both jsonl-per-line and concatenated formats.
    """
    try:
        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "user":
                    continue
                msg = obj.get("message") or obj
                content = msg.get("content") if isinstance(msg, dict) else None
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "text":
                            return b.get("text") or None
        return None
    except OSError:
        return None


def _proj_dir_for(cwd: str | Path) -> Path:
    return CLAUDE_HOME / "projects" / encode_cwd(cwd)


def list_sessions_for_cwd(cwd: str | Path, limit: int = 20) -> list[SessionInfo]:
    """Return sessions in Claude's local store that belong to the given cwd,
    newest first. Handles both `<uuid>.jsonl` and bare `<uuid>` filenames.
    """
    proj_dir = _proj_dir_for(cwd)
    if not proj_dir.is_dir():
        return []

    out: list[SessionInfo] = []
    for entry in proj_dir.iterdir():
        if not entry.is_file():
            continue
        if not _looks_like_session_id(entry.name):
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        session_id = entry.name[:-6] if entry.name.endswith(".jsonl") else entry.name
        first = _extract_first_user_text(entry)
        out.append(SessionInfo(session_id=session_id, cwd=str(cwd), mtime=mtime, first_user_text=first))

    out.sort(key=lambda s: s.mtime, reverse=True)
    return out[:limit]


def session_exists(cwd: str | Path, session_id: str) -> bool:
    proj_dir = _proj_dir_for(cwd)
    return (proj_dir / session_id).is_file() or (proj_dir / f"{session_id}.jsonl").is_file()
