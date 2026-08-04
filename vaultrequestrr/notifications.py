"""Background poller that DMs requesters when their media lands or is declined.

Requests submitted through the bot are recorded at submit time (which is also
where we capture the title — the Seerr request payload doesn't include one).
Requests made outside the bot (e.g. the Seerr web UI) are *adopted* into the
same tracking table when their requester has linked their account: instantly
via the Seerr webhook, or discovered by the poller's external sweep. The poller
checks each not-yet-finalised request and notifies on the first transition to
available / declined, then stops tracking it.
"""
from __future__ import annotations

import logging

import discord
from discord.ext import tasks

from .arr import ArrError
from .seerr import (
    ISSUE_RESOLVED,
    ISSUE_TYPE_LABELS,
    REQUEST_DECLINED,
    STATUS_AVAILABLE,
    STATUS_PARTIALLY_AVAILABLE,
    PendingRequest,
    RequestInfo,
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

# Re-grab import watching: give the download client a moment to surface the
# queue item before judging, and only call a re-grab dead once the queue has
# been empty with no file for a while (covers post-download import lag).
REGRAB_QUEUE_GRACE_SECONDS = 120
REGRAB_FAIL_SECONDS = 15 * 60

# External-request discovery: high-water mark (newest createdAt adopted so far)
# persisted in app_settings, and page limits for each sweep of GET /request.
EXTERNAL_CURSOR_KEY = "external_requests_cursor"
EXTERNAL_PAGE_SIZE = 100
EXTERNAL_MAX_PAGES = 5


def _parse_tracked_seasons(seasons: str | None) -> list[int]:
    """Season numbers from a TrackedRequest.seasons string, or [] for "all"/none."""
    if not seasons or seasons == "all":
        return []
    numbers: list[int] = []
    for part in seasons.split(","):
        part = part.strip()
        if part.isdigit():
            numbers.append(int(part))
    return numbers


def _request_available(tracked: TrackedRequest, info: RequestInfo) -> bool:
    """Whether a tracked request's content has landed and should trigger a DM.

    The show-level ``media_status`` is only a rollup: for TV, PARTIALLY_AVAILABLE
    means *some* season is present, not necessarily the requested one. So when a
    request targets specific seasons we consult the per-season status instead —
    otherwise a season that was already on the server (e.g. an existing S4) would
    make a brand-new S1 request report "available" on the very next poll.
    """
    if info.media_status == STATUS_AVAILABLE:
        return True
    if tracked.media_type != "tv":
        return False

    wanted = _parse_tracked_seasons(tracked.seasons)
    if wanted and info.season_status:
        landed = (STATUS_AVAILABLE, STATUS_PARTIALLY_AVAILABLE)
        return all(info.season_status.get(n) in landed for n in wanted)

    # "All seasons" (or no per-season detail): fall back to the show-level rollup,
    # where partial availability means content has started landing.
    return info.media_status == STATUS_PARTIALLY_AVAILABLE


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
        await self._broadcast(
            embeds,
            lambda: build_approval_view(request_id),
            recipient_ids=await self.bot.admin_ids(),
            channel_id=await self.bot.approvals_channel_id(),
        )

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
        sent = await self._broadcast(
            embeds,
            lambda: build_issue_view(issue_id),
            recipient_ids=await self.bot.issue_notify_ids(),
            channel_id=await self.bot.issues_channel_id(),
        )
        await self._record_issue_messages(issue_id, sent)

    async def _broadcast(self, embeds, make_view, *, recipient_ids, channel_id) -> list:  # type: ignore[no-untyped-def]
        """DM each recipient and post to the channel; a fresh view per send.

        Returns the messages that were actually sent, so issue cards can be
        tracked and later edited in sync (see ``issue_actions.sync_issue_cards``).
        """
        sent = []
        for user_id in recipient_ids:
            try:
                user = await self.bot.fetch_user(user_id)
            except (discord.NotFound, discord.HTTPException, ValueError) as exc:
                logger.warning("Could not resolve recipient %s: %s", user_id, exc)
                continue
            try:
                sent.append(await user.send(embeds=embeds, view=make_view()))
            except discord.Forbidden:
                logger.info("Recipient %s has DMs disabled; skipping", user_id)
            except discord.HTTPException as exc:
                logger.warning("Failed to DM recipient %s: %s", user_id, exc)

        if channel_id is not None:
            try:
                channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(
                    channel_id
                )
                sent.append(await channel.send(embeds=embeds, view=make_view()))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                logger.warning("Could not post to channel %s: %s", channel_id, exc)
        return sent

    async def _record_issue_messages(self, issue_id: int, messages: list) -> None:
        """Remember each card copy so later outcomes can be synced onto it."""
        for msg in messages:
            channel_id = getattr(getattr(msg, "channel", None), "id", None)
            message_id = getattr(msg, "id", None)
            if not isinstance(channel_id, int) or not isinstance(message_id, int):
                continue
            try:
                await self.bot.store.add_issue_message(issue_id, channel_id, message_id)
            except Exception:  # noqa: BLE001 - tracking is best-effort
                logger.debug(
                    "Could not record card copy for issue %s", issue_id, exc_info=True
                )

    # -- targeted checks (used by the Seerr webhook for instant delivery) ----

    async def check_request(self, request_id: int) -> None:
        """Re-check a single tracked request now (webhook-triggered).

        Requests we aren't tracking yet — made in the Seerr web UI rather than
        through the bot — get adopted first, when their requester has linked
        their account. Reuses the same finalisation path as the poller, so it's
        idempotent against the notified_* flags.
        """
        tracked = await self.bot.store.get_tracked(request_id)
        if tracked is None:
            tracked = await self._adopt_request(request_id)
        if tracked is None:
            return
        await self._check_one(tracked)

    # -- external (web-UI) requests ----------------------------------------

    async def _adopt_request(self, request_id: int) -> TrackedRequest | None:
        """Start tracking a request that was made outside the bot.

        Returns the new tracked row, or None when external tracking is off,
        the request can't be fetched, or the requester never linked a Discord
        account (there's nobody to DM).
        """
        if not self.bot.runtime.track_external_requests:
            return None
        try:
            info = await self.bot.seerr.get_request(request_id)
        except SeerrError as exc:
            logger.debug("Could not fetch request %s for adoption: %s", request_id, exc)
            return None
        if info is None or info.requested_by_id is None or info.media_type not in ("movie", "tv"):
            return None
        link = await self.bot.store.get_by_seerr_user(info.requested_by_id)
        if link is None:
            return None
        seasons = None
        if info.media_type == "tv":
            seasons = ",".join(str(n) for n in sorted(info.requested_seasons)) or "all"
        title = None
        if info.tmdb_id is not None:
            title = await self.bot.seerr.get_title(info.media_type, info.tmdb_id)
        await self.bot.store.add_tracked_request(
            request_id,
            link.discord_id,
            info.media_type,
            info.tmdb_id,
            title,
            seasons,
            source="seerr",
        )
        logger.info(
            "Adopted external request %s (%s) for Discord user %s",
            request_id, title or info.tmdb_id, link.discord_id,
        )
        return await self.bot.store.get_tracked(request_id)

    async def _sync_external_requests(self) -> None:
        """Discover requests made outside the bot and adopt them for tracking.

        Sweeps GET /request (newest first) down to the persisted cursor. The
        very first sweep is a backfill: requests already available or declined
        are adopted pre-notified, so enabling the feature records history
        without blasting DMs for media that landed long ago.
        """
        if not self.bot.runtime.track_external_requests:
            return
        store = self.bot.store
        cursor = await store.get_setting(EXTERNAL_CURSOR_KEY)
        backfill = cursor is None
        newest = cursor
        caught_up = False
        for page in range(EXTERNAL_MAX_PAGES):
            try:
                batch = await self.bot.seerr.list_requests(
                    take=EXTERNAL_PAGE_SIZE, skip=page * EXTERNAL_PAGE_SIZE
                )
            except SeerrError as exc:
                logger.debug("Could not list requests for external sync: %s", exc)
                return
            for req in batch:
                created = req.created_at or ""
                if newest is None or created > newest:
                    newest = created
                if cursor is not None and created <= cursor:
                    caught_up = True
                    break
                try:
                    await self._adopt_listed(req, backfill=backfill)
                except Exception:  # noqa: BLE001 - one bad request can't stall the sweep
                    logger.exception("Error adopting external request %s", req.id)
            if caught_up or len(batch) < EXTERNAL_PAGE_SIZE:
                break
        if newest and newest != cursor:
            await store.set_setting(EXTERNAL_CURSOR_KEY, newest)

    async def _adopt_listed(self, req: PendingRequest, *, backfill: bool) -> None:
        """Adopt one listed request, when its requester is linked and it's new."""
        if req.id is None or req.requested_by_id is None:
            return
        if req.media_type not in ("movie", "tv"):
            return
        store = self.bot.store
        if await store.get_tracked(req.id) is not None:
            return  # bot-submitted, or already adopted (e.g. via the webhook)
        link = await store.get_by_seerr_user(req.requested_by_id)
        if link is None:
            return
        seasons = None
        if req.media_type == "tv":
            seasons = ",".join(str(n) for n in sorted(req.seasons)) or "all"
        title = None
        if req.tmdb_id is not None:
            title = await self.bot.seerr.get_title(req.media_type, req.tmdb_id)
        # Backfilled terminal states are adopted pre-notified. That includes
        # PARTIALLY_AVAILABLE shows: the partial content may predate the
        # request, and a wrong "now available" DM is worse than a missing one.
        already_available = backfill and req.media_status in (
            STATUS_AVAILABLE,
            STATUS_PARTIALLY_AVAILABLE,
        )
        already_declined = backfill and req.request_status == REQUEST_DECLINED
        await store.add_tracked_request(
            req.id,
            link.discord_id,
            req.media_type,
            req.tmdb_id,
            title,
            seasons,
            source="seerr",
            notified_available=already_available,
            notified_declined=already_declined,
        )
        logger.info(
            "Adopted external request %s (%s) for Discord user %s%s",
            req.id, title or req.tmdb_id, link.discord_id,
            " [backfill]" if backfill else "",
        )

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
        # Discover web-UI requests first so a newly adopted one gets checked
        # (and possibly DMed) in this same cycle.
        try:
            await self._sync_external_requests()
        except Exception:  # noqa: BLE001 - never let the loop die
            logger.exception("Failed to sync external requests")

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
            await self._poll_regrabs()
        except Exception:  # noqa: BLE001
            logger.exception("Failed to poll in-flight re-grabs")

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

        if _request_available(tracked, info):
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

    # -- re-grab import watching -------------------------------------------

    async def _poll_regrabs(self) -> None:
        """Finalise in-flight re-grabs: resolve on import, flag dead downloads.

        A Re-grab click only pushes a release to the download client. The issue
        must stay open until the replacement actually lands, so each poll checks
        the arr queue/file state for every issue whose re-grab hasn't imported.
        """
        for tracked in await self.bot.store.issues_awaiting_import():
            try:
                await self._check_regrab(tracked)
            except Exception:  # noqa: BLE001 - one bad issue can't stall the rest
                logger.exception("Error checking re-grab for issue %s", tracked.issue_id)

    async def _check_regrab(self, tracked: TrackedIssue) -> None:
        from .issue_actions import age_seconds

        elapsed = age_seconds(tracked.regrab_at)
        if elapsed < REGRAB_QUEUE_GRACE_SECONDS:
            return  # the download client may not have picked the grab up yet

        try:
            detail = await self.bot.arr.media_detail(
                tracked.media_type,
                tracked.tmdb_id,
                season=tracked.problem_season,
                episode=tracked.problem_episode,
            )
        except (ArrError, SeerrError) as exc:
            logger.debug(
                "Could not check re-grab for issue %s: %s", tracked.issue_id, exc
            )
            return

        if detail["queue"]:
            return  # still downloading/importing — keep waiting
        if detail["has_file"]:
            await self._finish_regrab(tracked)
        elif elapsed > REGRAB_FAIL_SECONDS:
            await self._fail_regrab(tracked)
        # else: brief window between queue removal and import — check next poll.

    async def _finish_regrab(self, tracked: TrackedIssue) -> None:
        """The replacement imported: resolve the issue and finalise every card."""
        from .issue_actions import sync_issue_cards

        try:
            await self.bot.seerr.update_issue_status(tracked.issue_id, resolved=True)
        except SeerrError as exc:
            logger.debug(
                "Imported but couldn't resolve issue %s in Seerr: %s",
                tracked.issue_id, exc,
            )
        # Reuses the normal resolution path: DMs the reporter once and marks
        # the tracked issue resolved + notified.
        await self._apply_issue_status(tracked, ISSUE_RESOLVED)
        await self.bot.store.mark_issue(tracked.issue_id, regrab_state="imported")

        release = f" (“{tracked.regrab_release}”)" if tracked.regrab_release else ""
        by = ""
        if tracked.regrab_by:  # a Discord id, or "dashboard" for web-started re-grabs
            by = (
                f" — started by <@{tracked.regrab_by}>"
                if tracked.regrab_by.isdigit()
                else f" — started via the {tracked.regrab_by}"
            )
        note = (
            f"🎯 Replacement{release} for **{tracked.title or 'the reported title'}** "
            f"downloaded & imported{by}. Issue resolved."
        )
        await sync_issue_cards(self.bot, tracked.issue_id, note)
        await self.bot.store.delete_issue_messages(tracked.issue_id)

    async def _fail_regrab(self, tracked: TrackedIssue) -> None:
        """The download vanished without importing: reopen the cards for a retry."""
        from .issue_actions import build_issue_view, sync_issue_cards

        await self.bot.store.mark_issue(tracked.issue_id, regrab_state="failed")

        title = tracked.title or "the reported title"
        note = (
            f"⚠️ The re-grabbed release for **{title}** left the download queue "
            "without importing — the download likely failed. The issue is still open."
        )
        await sync_issue_cards(self.bot, tracked.issue_id, note)

        embed = discord.Embed(
            title="⚠️ Re-grab failed",
            description=(
                f"{note}\nYou can retry the re-grab or resolve the issue manually."
            ),
            color=discord.Color.orange(),
        )
        embed.set_footer(text="VaultRequestrr")
        sent = await self._broadcast(
            [embed],
            lambda: build_issue_view(tracked.issue_id),
            recipient_ids=await self.bot.issue_notify_ids(),
            channel_id=await self.bot.issues_channel_id(),
        )
        await self._record_issue_messages(tracked.issue_id, sent)

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
