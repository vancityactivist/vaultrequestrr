"""Admin issue workflow: persistent Re-grab/Resolve buttons and shared logic.

Mirrors :mod:`approvals` but for reported issues. When a user files an issue,
admins are DM'd (and the approvals channel posted to) with a card carrying these
buttons. The custom_ids encode the issue id and are registered via
``bot.add_dynamic_items`` in setup_hook, so the buttons keep working across
restarts.

* **Re-grab** runs the monitor → interactive-search → grab flow
  (``ArrManager.research``). A grab does *not* resolve the issue — the
  notification poller watches the arr queue and resolves only once the
  replacement actually imports (see ``NotificationService._poll_regrabs``).
* **Resolve** just marks the issue resolved in Seerr.

Every admin DM (plus the channel post) is its own copy of the card, so
outcomes are synced to all copies via :func:`sync_issue_cards` — otherwise the
other copies keep live buttons and a second admin can fire a duplicate re-grab.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import discord

from .arr import ArrError
from .seerr import ISSUE_RESOLVED, ISSUE_TYPE_LABELS, SeerrError
from .store import TrackedIssue

logger = logging.getLogger(__name__)

# An in-flight re-grab blocks further re-grabs. After this long without an
# import we assume the download is stuck and let an admin fire a fresh one.
REGRAB_STALE_SECONDS = 6 * 60 * 60


def age_seconds(iso_timestamp: str | None) -> float:
    """Seconds since an ISO timestamp; infinity when missing/unparseable."""
    if not iso_timestamp:
        return float("inf")
    try:
        then = datetime.fromisoformat(iso_timestamp)
    except ValueError:
        return float("inf")
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds()


def _regrab_in_flight(tracked: TrackedIssue) -> bool:
    return (
        tracked.regrab_state == "grabbed"
        and age_seconds(tracked.regrab_at) < REGRAB_STALE_SECONDS
    )


async def sync_issue_cards(
    bot,  # type: ignore[no-untyped-def]
    issue_id: int,
    content: str,
    *,
    view: discord.ui.View | None = None,
    skip_message_id: int | None = None,
) -> None:
    """Propagate an outcome to every broadcast copy of an issue's card.

    Best-effort: copies that can't be edited (deleted message, closed DM) are
    skipped. ``skip_message_id`` avoids re-editing the card the admin clicked,
    which the interaction response already updated.
    """
    try:
        records = await bot.store.list_issue_messages(issue_id)
    except Exception:  # noqa: BLE001 - syncing must never fail the action
        logger.debug("Could not load card copies for issue %s", issue_id, exc_info=True)
        return
    for record in records:
        if skip_message_id is not None and record.message_id == skip_message_id:
            continue
        try:
            channel = bot.get_channel(record.channel_id) or await bot.fetch_channel(
                record.channel_id
            )
            await channel.get_partial_message(record.message_id).edit(
                content=content, view=view
            )
        except (discord.HTTPException, AttributeError) as exc:
            logger.debug(
                "Could not sync issue %s card %s: %s", issue_id, record.message_id, exc
            )


def build_issue_view(issue_id: int) -> discord.ui.View:
    """A persistent (timeout=None) view with this issue's Re-grab/Resolve buttons."""
    view = discord.ui.View(timeout=None)
    view.add_item(RegrabButton(issue_id))
    view.add_item(ResolveButton(issue_id))
    return view


async def build_issue_embeds(
    bot,  # type: ignore[no-untyped-def]
    *,
    media_type: str | None,
    tmdb_id: int | None,
    title: str | None,
    issue_type: int | None,
    reporter_label: str | None,
    season: int | None,
    episode: int | None,
    message: str | None,
) -> list[discord.Embed]:
    """Banner (poster) + details embed describing a freshly reported issue."""
    title = title or "Unknown title"
    kind = "📺 TV show" if media_type == "tv" else "🎬 Movie"
    type_label = ISSUE_TYPE_LABELS.get(issue_type or 0, "Issue")
    heading = "🛠️ New issue reported"
    color = discord.Color.orange()

    poster_url = None
    if tmdb_id is not None and media_type:
        try:
            poster_url = await bot.seerr.get_poster_url(media_type, tmdb_id)
        except SeerrError:
            poster_url = None

    embeds: list[discord.Embed] = []
    if poster_url:
        banner = discord.Embed(title=heading, color=color)
        banner.set_image(url=poster_url)
        embeds.append(banner)
        body = discord.Embed(title=title, color=color)
    else:
        body = discord.Embed(title=f"{heading} — {title}", color=color)
    body.add_field(name="Type", value=kind, inline=True)
    body.add_field(name="Problem", value=type_label, inline=True)
    if season is not None and episode is not None:
        body.add_field(name="Episode", value=f"S{season:02d}E{episode:02d}", inline=True)
    if reporter_label:
        body.add_field(name="Reported by", value=reporter_label, inline=True)
    if message:
        body.add_field(
            name="Details",
            value=message if len(message) <= 1000 else message[:999] + "…",
            inline=False,
        )
    body.set_footer(text="VaultRequestrr")
    embeds.append(body)
    return embeds


async def _restore_card(
    interaction: discord.Interaction, issue_id: int, content: str
) -> None:
    """Put a status line on the card and bring the Re-grab/Resolve buttons back."""
    try:
        await interaction.edit_original_response(
            content=content, view=build_issue_view(issue_id)
        )
    except discord.HTTPException:
        await interaction.followup.send(content, ephemeral=True)


async def act_regrab(
    bot,  # type: ignore[no-untyped-def]
    interaction: discord.Interaction,
    issue_id: int,
) -> None:
    """Delete & re-grab a replacement for the issue's media, gated to admins.

    A successful grab does *not* resolve the issue: the poller resolves it (and
    DMs the reporter) only once the replacement finishes downloading and
    imports. On failure the card is left in place so an admin can retry or
    resolve manually.
    """
    if not await bot.is_issue_handler(interaction.user.id):
        await interaction.response.send_message(
            "⛔ You're not set up to handle issues.", ephemeral=True
        )
        return

    tracked = await bot.store.get_tracked_issue(issue_id)
    if tracked is None or tracked.tmdb_id is None or not tracked.media_type:
        await interaction.response.send_message(
            "⚠️ Can't re-grab this issue — no media is recorded for it.", ephemeral=True
        )
        return

    # Guard the *other* copies of this card: once the issue is resolved or a
    # re-grab is already in flight, clicking a stale copy must not fire the
    # whole delete-and-grab again (that's how duplicate downloads happen).
    if tracked.status == ISSUE_RESOLVED:
        await interaction.response.edit_message(
            content="✅ This issue has already been resolved.", view=None
        )
        return
    if _regrab_in_flight(tracked):
        await interaction.response.edit_message(
            content=f"⏳ A re-grab for **{tracked.title}** is already in flight — "
            "waiting for the replacement to download and import.",
            view=None,
        )
        return

    # The interactive search hits indexers and can take several seconds. Show a
    # visible "in progress" state on the card (and drop the buttons so it can't
    # be double-fired) instead of a silent defer, so every admin can see the
    # click was registered while the search runs.
    await interaction.response.edit_message(
        content=f"⏳ Re-grabbing… searching indexers for **{tracked.title}**, "
        "this can take a moment.",
        view=None,
    )
    try:
        result = await bot.arr.research(
            tracked.media_type,
            tracked.tmdb_id,
            season=tracked.problem_season,
            episode=tracked.problem_episode,
        )
    except (ArrError, SeerrError) as exc:
        # Restore the buttons so the admin can retry, and surface the error on
        # the card itself rather than in a fleeting ephemeral message.
        await _restore_card(interaction, issue_id, f"⚠️ {exc}")
        return

    if not result.grabbed:
        # Nothing grabbed: restore the card + buttons so the admin can retry.
        await _restore_card(interaction, issue_id, f"ℹ️ {result.message}")
        return

    # Record the in-flight re-grab; the poller takes it from here and resolves
    # the issue once the replacement imports (or flags it if the download dies).
    try:
        await bot.store.mark_issue(
            issue_id,
            regrab_state="grabbed",
            regrab_release=result.release or "",
            regrab_by=str(interaction.user.id),
            regrab_at=datetime.now(timezone.utc).isoformat(),
        )
    except Exception:  # noqa: BLE001 - bookkeeping must not undo the grab
        logger.debug("Could not record re-grab for issue %s", issue_id, exc_info=True)

    note = (
        f"📥 {result.message} Started by {interaction.user.mention} — the issue stays "
        "open until the replacement finishes downloading and imports."
    )
    try:
        await interaction.edit_original_response(content=note, view=None)
    except discord.HTTPException:
        await interaction.followup.send(note, ephemeral=True)
    await sync_issue_cards(
        bot, issue_id, note,
        skip_message_id=getattr(getattr(interaction, "message", None), "id", None),
    )


async def act_resolve(
    bot,  # type: ignore[no-untyped-def]
    interaction: discord.Interaction,
    issue_id: int,
) -> None:
    """Mark the issue resolved in Seerr, gated to admins."""
    if not await bot.is_issue_handler(interaction.user.id):
        await interaction.response.send_message(
            "⛔ You're not set up to handle issues.", ephemeral=True
        )
        return

    tracked = await bot.store.get_tracked_issue(issue_id)
    if tracked is not None and tracked.status == ISSUE_RESOLVED:
        # A stale copy of the card (another admin already resolved it).
        await interaction.response.edit_message(
            content="✅ This issue has already been resolved.", view=None
        )
        return

    try:
        await bot.seerr.update_issue_status(issue_id, resolved=True)
    except SeerrError as exc:
        await interaction.response.send_message(
            f"⚠️ Couldn't resolve that issue (it may already be handled): {exc}",
            ephemeral=True,
        )
        return

    try:
        await bot.store.mark_issue(issue_id, status=ISSUE_RESOLVED)
    except Exception:  # noqa: BLE001 - never fail the action on a bookkeeping error
        logger.debug("Could not update tracked issue %s", issue_id, exc_info=True)

    note = f"✅ Resolved by {interaction.user.mention}"
    try:
        await interaction.response.edit_message(content=note, view=None)
    except discord.HTTPException:
        try:
            await interaction.response.send_message(note, ephemeral=True)
        except discord.HTTPException:
            pass
    # Kill the live buttons on every other copy of the card, then forget them —
    # the issue is finalised, so there's nothing left to sync.
    await sync_issue_cards(
        bot, issue_id, note,
        skip_message_id=getattr(getattr(interaction, "message", None), "id", None),
    )
    try:
        await bot.store.delete_issue_messages(issue_id)
    except Exception:  # noqa: BLE001
        logger.debug("Could not clear card copies for issue %s", issue_id, exc_info=True)


class RegrabButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"vr:issue:regrab:(?P<iid>\d+)",
):
    def __init__(self, issue_id: int) -> None:
        self.issue_id = issue_id
        super().__init__(
            discord.ui.Button(
                label="Re-grab",
                style=discord.ButtonStyle.primary,
                custom_id=f"vr:issue:regrab:{issue_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):  # type: ignore[no-untyped-def]
        return cls(int(match["iid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await act_regrab(interaction.client, interaction, self.issue_id)


class ResolveButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"vr:issue:resolve:(?P<iid>\d+)",
):
    def __init__(self, issue_id: int) -> None:
        self.issue_id = issue_id
        super().__init__(
            discord.ui.Button(
                label="Resolve",
                style=discord.ButtonStyle.success,
                custom_id=f"vr:issue:resolve:{issue_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):  # type: ignore[no-untyped-def]
        return cls(int(match["iid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await act_resolve(interaction.client, interaction, self.issue_id)
