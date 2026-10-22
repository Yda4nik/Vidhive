"""Coordinator configuration, loaded from environment variables."""

import secrets
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

    # A worker silent for this long (agents heartbeat every ~5 s) is marked offline.
    worker_offline_seconds: int = 30

    # Requests arriving through a tunnel / from a public address (see services/netinfo)
    # may not open administrator pages or the first-run setup, and cannot see /docs.
    # Turn off only if the app is never exposed beyond a network you fully trust.
    restrict_public: bool = True

    # Who may open administrator pages through a public link while restrict_public is on:
    #   none  - nobody (admin work only from the home network / Tailscale)   [default]
    #   owner - only the main administrator (the first account ever created)
    #   any   - every administrator
    public_admin: str = "none"

    # Address agents deployed over SSH use to reach this coordinator (e.g. the tunnel's
    # https://… link). Empty = the address the admin used to open the page.
    public_url: str = ""

    # Helper that runs the few Docker actions the settings page needs (public link, VPN).
    supervisor_url: str = ""

    log_level: str = "INFO"

    # Access control (all from env; empty admin/token = feature effectively off).
    secret_key: str = ""  # signs session cookies; see resolve_secret_key()
    agent_token: str = ""
    admin_user: str = ""
    admin_password: str = ""


# Values that are publicly known (the old built-in default, and the placeholder shipped
# in deploy/.env.example) — a deployment that kept one would have forgeable cookies.
_KNOWN_KEYS = {"", "dev-insecure-change-me", "change-me-to-a-long-random-string"}


def resolve_secret_key(value: str | None) -> tuple[str, bool]:
    """Return ``(key, ephemeral)``. A missing, known-placeholder or too-short key is
    replaced by a random one for this process (sessions then reset on restart) rather
    than silently running with a guessable signing key."""
    v = (value or "").strip()
    if v in _KNOWN_KEYS or len(v) < 16:
        return secrets.token_urlsafe(48), True
    return v, False


@lru_cache
def get_settings() -> Settings:
    return Settings()
