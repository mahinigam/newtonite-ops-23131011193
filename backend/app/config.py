"""Application configuration.

Settings come from environment variables, falling back to the repository-root
`.env` file (see `.env.example`). Secrets are never read from code.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/config.py -> backend/app -> backend -> repository root
REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Runtime configuration for the API, Alembic and operational scripts."""

    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env", REPO_ROOT / "backend" / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: str = Field(default="development")
    database_url: str = Field(
        default="postgresql+asyncpg://newtonite:newtonite_dev_pw@127.0.0.1:5432/newtonite"
    )
    test_database_url: str = Field(
        default="postgresql+asyncpg://newtonite:newtonite_dev_pw@127.0.0.1:5432/newtonite_test"
    )

    session_absolute_ttl_hours: int = Field(default=12, ge=1, le=24 * 30)
    session_idle_ttl_hours: int = Field(default=2, ge=1, le=24 * 30)

    # Comma-separated list of browser origins allowed to call the API.
    cors_allowed_origins: str = Field(default="http://localhost:5173")

    # Outbox worker. The queue is a PostgreSQL table, not a broker, so these are the only
    # knobs: how much to take at once, how often to look when idle, and when to give up.
    outbox_batch_size: int = Field(default=20, ge=1, le=1000)
    outbox_poll_interval_seconds: float = Field(default=1.0, ge=0.05, le=60)
    outbox_max_attempts: int = Field(default=5, ge=1, le=100)

    log_level: str = Field(default="INFO")

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_allowed_origins.split(",") if origin.strip()]

    @property
    def is_test(self) -> bool:
        return self.environment == "test"


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
