from __future__ import annotations

# elementclaude mode (DB) → claude-agent-sdk permission_mode
MODE_TO_SDK: dict[str, str] = {
    "default": "default",
    "acceptEdits": "acceptEdits",
    "plan": "plan",
    "auto": "bypassPermissions",
}


def sdk_mode(mode: str) -> str:
    return MODE_TO_SDK.get(mode, "default")
