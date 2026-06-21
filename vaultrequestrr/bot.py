"""Discord bot bootstrap for VaultRequestrr."""
from __future__ import annotations

import logging
import uuid

import discord
from discord.ext import commands

from .arr import ArrManager
from .config import Config
from .linking import AccountLinker
from .notifications import NotificationService
from .plex import PlexClient
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
        self._seerr_url = config.seerr_url
        self.seerr = SeerrClient(config.seerr_url, config.seerr_api_key)
        self.store = LinkStore(config.database_path)
        self.linker = AccountLinker(self.seerr, self.store)
        self.arr = ArrManager(self)
        self.notifications = NotificationService(self)
        self.web = WebDashboard(self)
        self.plex: PlexClient | None = None

    @property
    def seerr_url(self) -> str:
        """The Seerr base URL the live client is currently using."""
        return self._seerr_url

    async def effective_webhook_secret(self) -> str:
        """The active webhook secret: dashboard-set wins, env is the first-run default.

        An empty string stored via the dashboard means "explicitly disabled" and
        overrides the env var.
        """
        stored = await self.store.get_setting("webhook_secret")
        if stored is not None:
            return stored
        return self.config.webhook_secret

    async def admin_ids(self) -> set[int]:
        """Discord ids that can approve requests and get notified of pending ones.

        Dashboard-set list wins; the ADMIN_DISCORD_IDS env var is the default.
        """
        stored = await self.store.get_setting("admin_discord_ids")
        if stored is not None:
            return {int(p) for p in stored.split(",") if p.strip().isdigit()}
        return set(self.config.admin_discord_ids)

    async def is_admin(self, discord_id: int | str) -> bool:
        return int(discord_id) in await self.admin_ids()

    async def approvals_channel_id(self) -> int | None:
        """Channel to post pending-approval cards to (dashboard-set wins over env)."""
        stored = await self.store.get_setting("approvals_channel_id")
        if stored is not None:
            return int(stored) if stored.strip().isdigit() else None
        return self.config.approvals_channel_id

    async def _anime_setting(self, key: str, env_default: int | str | None) -> int | str | None:
        """Resolve one anime-routing value: dashboard-set wins, env is the default.

        A stored empty string means "explicitly cleared" and overrides the env var.
        """
        stored = await self.store.get_setting(key)
        if stored is not None:
            stored = stored.strip()
            if not stored:
                return None
            if key.endswith(("_server_id", "_profile_id")):
                return int(stored) if stored.isdigit() else None
            return stored
        return env_default

    async def anime_routing(self, media_type: str) -> dict[str, object] | None:
        """`create_request` override kwargs for an anime request, or None if unconfigured.

        Returns only the keys that are set, with `server_id` being mandatory — without
        a target instance there's nothing to route, so the /anime flow treats None as
        "anime not set up for this media type" and falls back to normal routing.
        """
        kind = "sonarr" if media_type == "tv" else "radarr"
        server_id = await self._anime_setting(
            f"anime_{kind}_server_id", getattr(self.config, f"anime_{kind}_server_id")
        )
        if server_id is None:
            return None
        override: dict[str, object] = {"server_id": server_id}
        profile_id = await self._anime_setting(
            f"anime_{kind}_profile_id", getattr(self.config, f"anime_{kind}_profile_id")
        )
        if profile_id is not None:
            override["profile_id"] = profile_id
        root_folder = await self._anime_setting(
            f"anime_{kind}_root_folder", getattr(self.config, f"anime_{kind}_root_folder")
        )
        if root_folder is not None:
            override["root_folder"] = root_folder
        return override

    async def plex_client_id(self) -> str:
        """Stable Plex client identifier, generated once and persisted."""
        client_id = await self.store.get_setting("plex_client_id")
        if not client_id:
            client_id = str(uuid.uuid4())
            await self.store.set_setting("plex_client_id", client_id)
        return client_id

    async def apply_plex_connection(self, token: str, machine_id: str) -> None:
        """Build/swap the live Plex client for the chosen server + owner token."""
        client_id = await self.plex_client_id()
        old = self.plex
        self.plex = PlexClient(token, client_id, machine_id)
        if old is not None:
            try:
                await old.aclose()
            except Exception:  # noqa: BLE001 - best effort closing the old client
                logger.debug("Failed to close previous Plex client", exc_info=True)

    async def apply_seerr_connection(self, url: str, api_key: str) -> None:
        """Swap the live Seerr client (and the linker that holds it) to new creds."""
        old = self.seerr
        self._seerr_url = url
        self.seerr = SeerrClient(url, api_key)
        self.linker = AccountLinker(self.seerr, self.store)
        try:
            await old.aclose()
        except Exception:  # noqa: BLE001 - best effort closing the old client
            logger.debug("Failed to close previous Seerr client", exc_info=True)

    async def setup_hook(self) -> None:
        await self.store.connect()

        # Register persistent approve/decline buttons so they keep working across
        # restarts (an admin may act on a pending request hours later).
        from .approvals import ApproveButton, DeclineButton

        self.add_dynamic_items(ApproveButton, DeclineButton)

        # Persisted web-edited connection overrides env (env is the first-run default).
        url = await self.store.get_setting("seerr_url")
        key = await self.store.get_setting("seerr_api_key")
        if url or key:
            await self.apply_seerr_connection(
                url or self.config.seerr_url, key or self.config.seerr_api_key
            )

        try:
            await self.seerr.test_connection()
            logger.info("Connected to Seerr at %s", self.seerr_url)
        except SeerrError as exc:
            logger.warning("Could not verify Seerr connection: %s", exc)

        # Restore the Plex connection (web-UI configured, no env fallback).
        plex_token = await self.store.get_setting("plex_token")
        plex_machine_id = await self.store.get_setting("plex_machine_id")
        if plex_token and plex_machine_id:
            await self.apply_plex_connection(plex_token, plex_machine_id)
            logger.info("Plex invites enabled (server %s)", plex_machine_id)

        # Import here to avoid a circular import at module load.
        from .cogs.invites import InviteCog
        from .cogs.issues import IssueCog
        from .cogs.requests import RequestCog

        await self.add_cog(RequestCog(self))
        await self.add_cog(IssueCog(self))
        await self.add_cog(InviteCog(self))

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
                "Notification poller started (adaptive: tight until a Seerr webhook is "
                "configured, then relaxes to a %ds backstop)",
                self.config.poll_interval_seconds,
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
        if self.plex is not None:
            await self.plex.aclose()
        await self.store.close()
        await super().close()
