"""Coordinator configuration, loaded from environment variables."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VIDHIVE_", env_file=".env", extra="ignore")

    # Async DSN used by the application (SQLAlchemy + asyncpg)
    database_url: str = "postgresql+asyncpg://vidhive:vidhive@postgres:5432/vidhive"

    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # Defaults for new jobs
    default_chunk_size: int = 5000
    default_lease_seconds: int = 120

    # Stop issuing blocks to a worker that is running out of disk (spec 6.3).
    min_free_space_gb: float = 5.0

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
