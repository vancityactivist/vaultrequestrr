"""Discord bot bootstrap for VaultRequestrr."""
from __future__ import annotations

import logging

import discord
from discord.ext import commands

from .config import Config
from .linking import AccountLinker
from .notifications import NotificationService
from .runtime import RuntimeSettings
from .seerr import SeerrClient, SeerrError
from .store import LinkStore
from .web import WebDashboard

logger = logging.getLogger(__name__)


class VaultRequestrr(commands.Bot):
    def __init__(self, config: Config) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.config = config
        self.runtime = RuntimeSettings.from_config(config)
        self.seerr = SeerrClient(config.seerr_url, config.seerr_api_key)
        self.store = LinkStore(config.database_path)
        self.linker = AccountLinker(self.seerr, self.store)
        self.notifications = NotificationService(self)
        self.web = WebDashboard(self)

    async def setup_hook(self) -> None:
        await self.store.connect()

        try:
            await self.seerr.test_connection()
            logger.info("Connected to Seerr at %s", self.config.seerr_url)
        except SeerrError as exc:
            logger.warning("Could not verify Seerr connection: %s", exc)

        # Import here to avoid a circular import at module load.
        from .cogs.requests import RequestCog

        await self.add_cog(RequestCog(self))

        if self.config.discord_guild_id:
            guild = discord.Object(id=self.config.discord_guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            logger.info("Synced %d commands to guild %s", len(synced), self.config.discord_guild_id)
        else:
            synced = await self.tree.sync()
            logger.info("Synced %d global commands (may take up to ~1h to appear)", len(synced))

        if self.config.poll_interval_seconds > 0:
            self.notifications.start()
            logger.info(
                "Notification poller started (every %ds)", self.config.poll_interval_seconds
            )

        if self.config.web_password:
            await self.web.start()
            logger.info("Admin dashboard listening on port %d", self.config.web_port)
        else:
            logger.info("Admin dashboard disabled (set WEB_PASSWORD to enable)")

    async def on_ready(self) -> None:
        logger.info("Logged in as %s (id=%s)", self.user, getattr(self.user, "id", "?"))

    async def close(self) -> None:
        self.notifications.stop()
        await self.web.stop()
        await self.seerr.aclose()
        await self.store.close()
        await super().close()
