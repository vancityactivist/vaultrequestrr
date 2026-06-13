"""Entrypoint: `python -m vaultrequestrr`."""
from __future__ import annotations

import logging
import sys

from .bot import VaultRequestrr
from .config import Config, ConfigError
from .logbuffer import install as install_log_buffer


def main() -> int:
    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    install_log_buffer()  # capture recent logs for the web dashboard

    bot = VaultRequestrr(config)
    bot.run(config.discord_token, log_handler=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
