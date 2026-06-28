from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Claude
    anthropic_api_key: str = Field(..., description="Anthropic API key")
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
    workspace_root: Path = Field(default=Path("/workspace"))

    # Misc
    data_dir: Path = Field(default=Path("/data"))
    log_level: str = Field(default="INFO")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "elementclaude.sqlite3"


settings = Settings()  # type: ignore[call-arg]
