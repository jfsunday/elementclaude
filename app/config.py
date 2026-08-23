from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Claude
    anthropic_api_key: str = Field(..., description="Anthropic API key")
    anthropic_base_url: str | None = Field(
        default=None, description="Override https://api.anthropic.com (proxy etc.)"
    )
    default_model: str = Field(default="claude-opus-4-7")
    default_mode: str = Field(default="default")  # default | acceptEdits | plan | auto

    # Messaging-bot bridge
    messaging_bot_url: str = Field(..., description="Internal URL of messaging-bot")
    messaging_bot_api_key: str = Field(..., description="API key for messaging-bot")
    elementclaude_internal_url: str = Field(
        default="http://elementclaude:8000",
        description="URL messaging-bot uses to reach us back",
    )
    webhook_secret: str = Field(..., description="HMAC secret for inbound webhook from messaging-bot")

    # Auth
    initial_admin_user: str = Field(..., description="Matrix user ID of the initial admin, e.g. @js:matrix.org")

    # Workspace
    # Empty / unset = no restriction (cwd may be any directory the bot can read).
    # Set to a path to require all !cwd paths live under it.
    workspace_root: Path | None = Field(default=None)

    # Voice (STT / TTS) — see app/voice/
    # Per-room toggles live on the Room row; these are the defaults + backend config.
    voice_enabled_default: bool = Field(
        default=False, description="Whether new rooms start with STT+TTS on"
    )
    voice_engine_default: str = Field(default="cloud")  # cloud | local

    # STT
    stt_model: str = Field(default="medium", description="faster-whisper model size for local STT")
    stt_compute_type: str = Field(default="int8", description="faster-whisper compute type")
    stt_language: str | None = Field(default=None, description="Force a language, None = autodetect")
    stt_max_seconds: int = Field(default=300, description="Reject voice messages longer than this")
    stt_api_url: str = Field(
        default="https://api-inference.huggingface.co/models/openai/whisper-large-v3",
        description="OpenAI-compatible /audio/transcriptions endpoint (or HF inference URL)",
    )
    stt_api_token: str | None = Field(
        default=None,
        description="Token for STT_API_URL. Unset = cloud STT falls back to local.",
    )
    stt_api_model: str | None = Field(
        default=None, description="`model` form field for OpenAI-compatible STT endpoints"
    )

    # TTS
    tts_voice: str = Field(default="de-DE-KatjaNeural", description="edge-tts voice name")
    tts_max_chars: int = Field(default=1200, description="Truncate spoken text at this length")
    piper_model_path: Path | None = Field(
        default=None, description="Path to a piper .onnx voice; unset = fall back to espeak-ng"
    )

    # Misc
    data_dir: Path = Field(default=Path("./data"))
    log_level: str = Field(default="INFO")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "elementclaude.sqlite3"


settings = Settings()  # type: ignore[call-arg]
