"""Application settings.

All configuration comes from environment variables (optionally via a local
`.env` file, which is git-ignored) — nothing here is hardcoded per the
"secrets only through environment variables" rule.

`database_url` defaults to a local SQLite file so the API can run without
Docker for quick iteration. The documented, intended setup is Postgres via
Docker Compose (see docker-compose.yml / .env.example), which sets
AGENTFORGE_DATABASE_URL explicitly.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENTFORGE_", env_file=".env", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./agentforge_dev.db"
    sql_echo: bool = False
    cors_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]


@lru_cache
def get_settings() -> Settings:
    return Settings()
