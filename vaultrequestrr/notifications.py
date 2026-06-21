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
    ISSUE_RESOLVED,
    ISSUE_TYPE_LABELS,
    REQUEST_DECLINED,
    STATUS_AVAILABLE,
    STATUS_PARTIALLY_AVAILABLE,
    SeerrError,
    format_quota_line,
)
from .store import TrackedIssue, TrackedRequest

logger = logging.getLogger(__name__)

# When no Seerr webhook is configured, polling is the only delivery path, so we
# poll at this tighter cadence for near-real-time DMs. With a webhook set, the
# poller relaxes to the (longer) configured POLL_INTERVAL_SECONDS as a backstop.
ACTIVE_POLL_SECONDS = 120
MIN_POLL_SECONDS = 30


class NotificationService:
    def __init__(self, bot) -> None:  # type: ignore[no-untyped-def]
        self.bot = bot
        # Start tight; the first poll re-evaluates and relaxes if a webhook exists.
        self._loop = tasks.loop(seconds=self._floor(ACTIVE_POLL_SECONDS))(self._poll)
        self._loop.before_loop(self._before_loop)

    def start(self) -> None:
        if not self._loop.is_running():
            self._loop.start()

    def stop(self) -> None:
        self._loop.cancel()

    async def _before_loop(self) -> None:
        await self.bot.wait_until_ready()

    # -- adaptive cadence --------------------------------------------------

    def _floor(self, seconds: int) -> int:
        return max(seconds, MIN_POLL_SECONDS)

    async def _target_interval(self) -> int:
        """Tight when polling is the only delivery path; relaxed once a webhook exists."""
        backstop = self._floor(self.bot.config.poll_interval_seconds)
        try:
            has_webhook = bool(await self.bot.effective_webhook_secret())
        except Exception:  # noqa: BLE001 - never let cadence logic break the poll
            has_webhook = False
        return backstop if has_webhook else min(backstop, self._floor(ACTIVE_POLL_SECONDS))

    async def _adapt_interval(self) -> None:
        target = await self._target_interval()
        if round(self._loop.seconds or 0) != target:
            self._loop.change_interval(seconds=target)
            logger.debug("Poll cadence set to %ds (webhook backstop adapts)", target)

    # -- approvals ---------------------------------------------------------

    async def notify_pending_approval(
        self,
        request_id: int,
        *,
        media_type: str | None,
        tmdb_id: int | None,
        title: str | None,
        requester_label: str | None,
        seasons: str | None,
    ) -> None:
        """Announce a request awaiting approval: DM each admin and post to the channel."""
        from .approvals import build_approval_embeds, build_approval_view

        embeds = await build_approval_embeds(
            self.bot,
            media_type=media_type,
            tmdb_id=tmdb_id,
            title=title,
            requester_label=requester_label,
            seasons=seasons,
        )
        await self._broadcast_to_admins(embeds, lambda: build_approval_view(request_id))

    async def notify_issue_filed(
        self,
        issue_id: int,
        *,
        media_type: str | None,
        tmdb_id: int | None,
        title: str | None,
        issue_type: int | None,
        reporter_label: str | None,
        season: int | None,
        episode: int | None,
        message: str | None,
    ) -> None:
        """Announce a freshly reported issue: DM each admin and post to the channel."""
        from .issue_actions import build_issue_embeds, build_issue_view

        embeds = await build_issue_embeds(
            self.bot,
            media_type=media_type,
            tmdb_id=tmdb_id,
            title=title,
            issue_type=issue_type,
            reporter_label=reporter_label,
            season=season,
            episode=episode,
            message=message,
        )
        await self._broadcast_to_admins(embeds, lambda: build_issue_view(issue_id))

    async def _broadcast_to_admins(self, embeds, make_view) -> None:  # type: ignore[no-untyped-def]
        """DM each admin and post to the approvals channel; a fresh view per send."""
        for admin_id in await self.bot.admin_ids():
            try:
                user = await self.bot.fetch_user(admin_id)
            except (discord.NotFound, discord.HTTPException, ValueError) as exc:
                logger.warning("Could not resolve admin %s: %s", admin_id, exc)
                continue
            try:
                await user.send(embeds=embeds, view=make_view())
            except discord.Forbidden:
                logger.info("Admin %s has DMs disabled; skipping", admin_id)
            except discord.HTTPException as exc:
                logger.warning("Failed to DM admin %s: %s", admin_id, exc)

        channel_id = await self.bot.approvals_channel_id()
        if channel_id is not None:
            try:
                channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(
                    channel_id
                )
                await channel.send(embeds=embeds, view=make_view())
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                logger.warning("Could not post to approvals channel %s: %s", channel_id, exc)

    # -- targeted checks (used by the Seerr webhook for instant delivery) ----

    async def check_request(self, request_id: int) -> None:
        """Re-check a single tracked request now (webhook-triggered).

        A no-op if we aren't tracking this request. Reuses the same finalisation
        path as the poller, so it's idempotent against the notified_* flags.
        """
        tracked = await self.bot.store.get_tracked(request_id)
        if tracked is None:
            return
        await self._check_one(tracked)

    async def check_issue(self, issue_id: int) -> None:
        """Re-check a single tracked issue now (webhook-triggered)."""
        tracked = await self.bot.store.get_tracked_issue(issue_id)
        if tracked is None or tracked.notified_resolved:
            return
        try:
            live = await self.bot.seerr.list_issues()
        except SeerrError as exc:
            logger.debug("Could not refresh issue %s: %s", issue_id, exc)
            return
        status = {issue.id: issue.status for issue in live}.get(issue_id)
        if status is not None:
            await self._apply_issue_status(tracked, status)

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

        try:
            await self._poll_issues()
        except Exception:  # noqa: BLE001
            logger.exception("Failed to poll tracked issues")

        try:
            await self._adapt_interval()
        except Exception:  # noqa: BLE001
            logger.exception("Failed to adapt poll cadence")

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

    # -- issues ------------------------------------------------------------

    async def _poll_issues(self) -> None:
        pending = await self.bot.store.pending_issues()
        if not pending:
            return

        try:
            live = await self.bot.seerr.list_issues()
        except SeerrError as exc:
            logger.debug("Could not refresh issues: %s", exc)
            return
        status_by_id = {issue.id: issue.status for issue in live}

        for tracked in pending:
            status = status_by_id.get(tracked.issue_id)
            if status is not None:
                await self._apply_issue_status(tracked, status)

    async def _apply_issue_status(self, tracked: TrackedIssue, status: int) -> None:
        """Finalise a tracked issue: DM on first resolution, else keep status fresh."""
        if status != ISSUE_RESOLVED:
            if status != tracked.status:
                await self.bot.store.mark_issue(tracked.issue_id, status=status)
            return

        if self.bot.runtime.notify_on_issue_resolved and not tracked.notified_resolved:
            await self._dm_issue_resolved(tracked)
        await self.bot.store.mark_issue(
            tracked.issue_id, status=ISSUE_RESOLVED, notified_resolved=True
        )

    async def _dm_issue_resolved(self, tracked: TrackedIssue) -> None:
        try:
            user = await self.bot.fetch_user(int(tracked.discord_id))
        except (discord.NotFound, discord.HTTPException, ValueError) as exc:
            logger.warning("Could not resolve Discord user %s: %s", tracked.discord_id, exc)
            return

        title = tracked.title or "your reported title"
        label = ISSUE_TYPE_LABELS.get(tracked.issue_type or 0, "issue")
        heading = "🛠️ Issue resolved"
        description = (
            f"The **{label}** issue you reported for **{title}** has been marked resolved. "
            "If it's still happening, run `/issue` to report it again."
        )
        color = discord.Color.green()

        poster_url = None
        if tracked.tmdb_id is not None and tracked.media_type:
            try:
                poster_url = await self.bot.seerr.get_poster_url(
                    tracked.media_type, tracked.tmdb_id
                )
            except SeerrError:
                poster_url = None

        embeds: list[discord.Embed] = []
        if poster_url:
            banner = discord.Embed(title=heading, color=color)
            banner.set_image(url=poster_url)
            embeds.append(banner)
            body = discord.Embed(description=description, color=color)
        else:
            body = discord.Embed(title=heading, description=description, color=color)
        body.set_footer(text="VaultRequestrr")
        embeds.append(body)

        try:
            await user.send(embeds=embeds)
        except discord.Forbidden:
            logger.info("User %s has DMs disabled; skipping notification", tracked.discord_id)
        except discord.HTTPException as exc:
            logger.warning("Failed to DM user %s: %s", tracked.discord_id, exc)

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

        kind = "📺 TV show" if tracked.media_type == "tv" else "🎬 Movie"

        if available:
            heading = "✅ Now available"
            description = f"**{title}** is ready to watch — enjoy! 🍿"
            color = discord.Color.green()
        else:
            heading = "❌ Request declined"
            description = f"Your request for **{title}** was declined."
            color = discord.Color.red()

        # Cover art — fetch the poster for a richer DM (best-effort).
        poster_url = None
        if tracked.tmdb_id is not None:
            try:
                poster_url = await self.bot.seerr.get_poster_url(
                    tracked.media_type, tracked.tmdb_id
                )
            except SeerrError:
                poster_url = None

        # Discord renders a full-width embed image at the bottom of its embed, so
        # to get prominent artwork *above* the text we stack two embeds: a banner
        # (heading + full-width poster) on top, then the details below it.
        embeds: list[discord.Embed] = []
        if poster_url:
            banner = discord.Embed(title=heading, color=color)
            banner.set_image(url=poster_url)
            embeds.append(banner)
            body = discord.Embed(description=description, color=color)
        else:
            body = discord.Embed(title=heading, description=description, color=color)
        body.add_field(name="Type", value=kind, inline=True)

        # Remind them what's left in their quota (best-effort).
        await self._add_quota_field(body, tracked)

        body.set_footer(text="VaultRequestrr")
        embeds.append(body)

        try:
            await user.send(embeds=embeds)
        except discord.Forbidden:
            logger.info("User %s has DMs disabled; skipping notification", tracked.discord_id)
        except discord.HTTPException as exc:
            logger.warning("Failed to DM user %s: %s", tracked.discord_id, exc)

    async def _add_quota_field(
        self, embed: discord.Embed, tracked: TrackedRequest
    ) -> None:
        """Append a remaining-quota reminder for the tracked media type, if we can."""
        try:
            link = await self.bot.store.get(tracked.discord_id)
            if link is None:
                return
            quota = await self.bot.seerr.get_quota(link.seerr_user_id)
        except SeerrError as exc:
            logger.debug("Could not load quota for DM to %s: %s", tracked.discord_id, exc)
            return

        status = quota.tv if tracked.media_type == "tv" else quota.movie
        label = "📺 TV quota" if tracked.media_type == "tv" else "🎬 Movie quota"
        embed.add_field(name=label, value=format_quota_line(status), inline=False)
