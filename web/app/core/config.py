"""Web front-end configuration."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VIDHIVE_", env_file=".env", extra="ignore")

    coordinator_url: str = "http://coordinator:8000"
    web_host: str = "0.0.0.0"
    web_port: int = 8080
    request_timeout: float = 5.0
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
