"""Runtime configuration loaded from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    discord_token: str
    discord_guild_id: int | None
    seerr_url: str
    seerr_api_key: str
    require_linking: bool
    default_seerr_user_id: int | None
    database_path: str
    log_level: str
    # Notifications
    poll_interval_seconds: int
    notify_on_available: bool
    notify_on_declined: bool
    notify_on_issue_resolved: bool
    # Web dashboard
    web_port: int
    web_password: str
    # Shared secret for the inbound Seerr webhook; empty disables the endpoint.
    webhook_secret: str

    @classmethod
    def from_env(cls) -> "Config":
        discord_token = os.getenv("DISCORD_TOKEN", "").strip()
        seerr_url = os.getenv("SEERR_URL", "").strip().rstrip("/")
        seerr_api_key = os.getenv("SEERR_API_KEY", "").strip()

        missing = [
            name
            for name, value in (
                ("DISCORD_TOKEN", discord_token),
                ("SEERR_URL", seerr_url),
                ("SEERR_API_KEY", seerr_api_key),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                "Missing required environment variables: " + ", ".join(missing)
            )

        return cls(
            discord_token=discord_token,
            discord_guild_id=_optional_int(os.getenv("DISCORD_GUILD_ID")),
            seerr_url=seerr_url,
            seerr_api_key=seerr_api_key,
            require_linking=_bool(os.getenv("REQUIRE_LINKING"), default=True),
            default_seerr_user_id=_optional_int(os.getenv("DEFAULT_SEERR_USER_ID")),
            database_path=os.getenv("DATABASE_PATH", "data/vaultrequestrr.sqlite3").strip(),
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
            poll_interval_seconds=_optional_int(os.getenv("POLL_INTERVAL_SECONDS")) or 600,
            notify_on_available=_bool(os.getenv("NOTIFY_ON_AVAILABLE"), default=True),
            notify_on_declined=_bool(os.getenv("NOTIFY_ON_DECLINED"), default=True),
            notify_on_issue_resolved=_bool(
                os.getenv("NOTIFY_ON_ISSUE_RESOLVED"), default=True
            ),
            web_port=_optional_int(os.getenv("WEB_PORT")) or 5056,
            web_password=os.getenv("WEB_PASSWORD", "").strip(),
            webhook_secret=os.getenv("WEBHOOK_SECRET", "").strip(),
        )


def _optional_int(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        return int(value.strip())
    except ValueError as exc:
        raise ConfigError(f"Expected an integer but got {value!r}") from exc
