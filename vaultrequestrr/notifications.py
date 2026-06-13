"""Background poller that DMs requesters when their media lands or is declined.

We only track requests submitted through the bot (recorded at submit time, which
is also where we capture the title — the Seerr request payload doesn't include
one). The poller checks each not-yet-finalised request and notifies on the first
transition to available / declined, then stops tracking it.
"""
from __future__ import annotations

import logging

import discord
from discord.ext import tasks

from .seerr import (
    REQUEST_DECLINED,
    STATUS_AVAILABLE,
    STATUS_PARTIALLY_AVAILABLE,
    SeerrError,
)
from .store import TrackedRequest

logger = logging.getLogger(__name__)


class NotificationService:
    def __init__(self, bot) -> None:  # type: ignore[no-untyped-def]
        self.bot = bot
        interval = max(bot.config.poll_interval_seconds, 30)
        self._loop = tasks.loop(seconds=interval)(self._poll)
        self._loop.before_loop(self._before_loop)

    def start(self) -> None:
        if not self._loop.is_running():
            self._loop.start()

    def stop(self) -> None:
        self._loop.cancel()

    async def _before_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _poll(self) -> None:
        try:
            pending = await self.bot.store.pending_tracked()
        except Exception:  # noqa: BLE001 - never let the loop die
            logger.exception("Failed to load pending tracked requests")
            return

        for tracked in pending:
            try:
                await self._check_one(tracked)
            except Exception:  # noqa: BLE001
                logger.exception("Error checking tracked request %s", tracked.request_id)

    async def _check_one(self, tracked: TrackedRequest) -> None:
        runtime = self.bot.runtime
        try:
            info = await self.bot.seerr.get_request(tracked.request_id)
        except SeerrError as exc:
            if "404" in str(exc):  # request deleted in Seerr — stop tracking
                await self.bot.store.remove_tracked(tracked.request_id)
            else:
                logger.debug("Could not refresh request %s: %s", tracked.request_id, exc)
            return

        # Declined takes priority over availability.
        if info.request_status == REQUEST_DECLINED:
            if runtime.notify_on_declined and not tracked.notified_declined:
                await self._dm(tracked, available=False)
            await self.bot.store.mark_tracked(
                tracked.request_id,
                request_status=info.request_status,
                notified_declined=True,  # finalise either way so we stop polling it
            )
            return

        available = info.media_status == STATUS_AVAILABLE or (
            tracked.media_type == "tv" and info.media_status == STATUS_PARTIALLY_AVAILABLE
        )
        if available:
            if runtime.notify_on_available and not tracked.notified_available:
                await self._dm(tracked, available=True)
            await self.bot.store.mark_tracked(
                tracked.request_id,
                media_status=info.media_status,
                notified_available=True,
            )
            return

        # Still in flight — just record the latest status.
        await self.bot.store.mark_tracked(
            tracked.request_id,
            request_status=info.request_status,
            media_status=info.media_status,
        )

    async def _dm(self, tracked: TrackedRequest, *, available: bool) -> None:
        try:
            user = await self.bot.fetch_user(int(tracked.discord_id))
        except (discord.NotFound, discord.HTTPException, ValueError) as exc:
            logger.warning("Could not resolve Discord user %s: %s", tracked.discord_id, exc)
            return

        title = tracked.title or "Your request"
        if tracked.seasons and tracked.seasons != "all":
            title = f"{title} (seasons {tracked.seasons})"
        elif tracked.seasons == "all" and tracked.media_type == "tv":
            title = f"{title} (all seasons)"

        if available:
            embed = discord.Embed(
                title="✅ Now available",
                description=f"**{title}** is ready to watch.",
                color=discord.Color.green(),
            )
        else:
            embed = discord.Embed(
                title="❌ Request declined",
                description=f"Your request for **{title}** was declined.",
                color=discord.Color.red(),
            )

        try:
            await user.send(embed=embed)
        except discord.Forbidden:
            logger.info("User %s has DMs disabled; skipping notification", tracked.discord_id)
        except discord.HTTPException as exc:
            logger.warning("Failed to DM user %s: %s", tracked.discord_id, exc)
