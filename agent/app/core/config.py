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

    # How the coordinator/web reach this agent (files, control). Defaults to the
    # docker-network name; override when running outside compose.
    agent_url: str | None = None
    agent_host: str = "0.0.0.0"
    agent_port: int = 8100

    # Target and processing.
    target_template: str = "http://kinescope.io/{id}"
    # Lease window with ~4 heartbeats inside it: a dead agent's block returns to
    # the queue quickly, while a live one never loses its lease.
    lease_seconds: int = 60
    poll_interval: float = 1.0          # wait between lease attempts when idle
    # Metrics must reach the UI at least once per 5 seconds (spec 18).
    heartbeat_interval: float = 5.0
    check_timeout: float = 20.0
    progress_batch: int = 50            # identifiers per progress report / checkpoint
    enable_download: bool = True

    # Rate limiting and retries (spec section 9).
    rate_limit_rps: float | None = None   # global checks/sec for this agent; None = unlimited
    max_retries: int = 4
    retry_base_delay: float = 1.0
    retry_max_delay: float = 60.0

    log_level: str = "INFO"

    def advertised_url(self) -> str:
        return self.agent_url or f"http://{self.worker_name}:{self.agent_port}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
