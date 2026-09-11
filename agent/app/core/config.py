"""Agent configuration, loaded from environment variables."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VIDHIVE_", env_file=".env", extra="ignore")

    # How the agent reaches the coordinator.
    coordinator_url: str = "http://coordinator:8000"

    # Identity and resources of this worker.
    worker_name: str = "agent-local"
    threads: int = 8
    storage_path: str = "/data/videos"

    agent_host: str = "0.0.0.0"
    agent_port: int = 8100

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
