"""Mutable runtime settings the admin dashboard can toggle without a restart.

Seeded from the immutable env Config at startup. The bot, cogs and notifier
read from this so changes take effect immediately.
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import Config


@dataclass
class RuntimeSettings:
    require_linking: bool
    notify_on_available: bool
    notify_on_declined: bool
    notify_on_issue_resolved: bool
    log_level: str

    @classmethod
    def from_config(cls, config: Config) -> "RuntimeSettings":
        return cls(
            require_linking=config.require_linking,
            notify_on_available=config.notify_on_available,
            notify_on_declined=config.notify_on_declined,
            notify_on_issue_resolved=config.notify_on_issue_resolved,
            log_level=config.log_level,
        )
