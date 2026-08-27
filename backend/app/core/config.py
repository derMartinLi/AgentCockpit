from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENT_COCKPIT_",
        env_file=BACKEND_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    app_name: str = "AI Coding Cockpit"
    data_dir: Path = Path(".agent-cockpit")
    database_path: Path | None = None
    api_key: str | None = None
    command_timeout_seconds: int = 120
    max_concurrent_runs: int = Field(default=3, ge=1)
    frontend_origin: str = "http://localhost:4173"
    langfuse_enabled: bool = True
    langfuse_public_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "LANGFUSE_PUBLIC_KEY", "AGENT_COCKPIT_LANGFUSE_PUBLIC_KEY"
        ),
    )
    langfuse_secret_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "LANGFUSE_SECRET_KEY", "AGENT_COCKPIT_LANGFUSE_SECRET_KEY"
        ),
    )
    langfuse_base_url: str = Field(
        default="https://cloud.langfuse.com",
        validation_alias=AliasChoices("LANGFUSE_BASE_URL", "AGENT_COCKPIT_LANGFUSE_BASE_URL"),
    )
    langfuse_environment: str = "development"

    @property
    def resolved_database_path(self) -> Path:
        return self.database_path or self.data_dir / "agent-cockpit.db"

    @property
    def worktrees_dir(self) -> Path:
        return self.data_dir / "worktrees"


@lru_cache
def get_settings() -> Settings:
    return Settings()
