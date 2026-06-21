"""Admin issue workflow: persistent Re-grab/Resolve buttons and shared logic.

Mirrors :mod:`approvals` but for reported issues. When a user files an issue,
admins are DM'd (and the approvals channel posted to) with a card carrying these
buttons. The custom_ids encode the issue id and are registered via
``bot.add_dynamic_items`` in setup_hook, so the buttons keep working across
restarts.

* **Re-grab** runs the same monitor → interactive-search → grab flow as the
  dashboard (``ArrManager.research``) and resolves the issue only if a release
  is actually grabbed.
* **Resolve** just marks the issue resolved in Seerr.
"""
from __future__ import annotations

import logging
import re

import discord

from .arr import ArrError
from .seerr import ISSUE_RESOLVED, ISSUE_TYPE_LABELS, SeerrError

logger = logging.getLogger(__name__)


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


async def act_regrab(
    bot,  # type: ignore[no-untyped-def]
    interaction: discord.Interaction,
    issue_id: int,
) -> None:
    """Delete & re-grab a replacement for the issue's media, gated to admins.

    Resolves the issue only when a release is actually grabbed; otherwise the
    card is left in place so an admin can retry or resolve manually.
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

    # The interactive search hits indexers and can take several seconds, so
    # acknowledge the click first (defer the message update) to beat the timeout.
    await interaction.response.defer()
    try:
        result = await bot.arr.research(
            tracked.media_type,
            tracked.tmdb_id,
            season=tracked.problem_season,
            episode=tracked.problem_episode,
        )
    except (ArrError, SeerrError) as exc:
        await interaction.followup.send(f"⚠️ {exc}", ephemeral=True)
        return

    if not result.grabbed:
        # Nothing grabbed: leave the card + buttons so the admin can retry.
        await interaction.followup.send(f"ℹ️ {result.message}", ephemeral=True)
        return

    try:
        await bot.seerr.update_issue_status(issue_id, resolved=True)
        await bot.store.mark_issue(issue_id, status=ISSUE_RESOLVED)
    except SeerrError as exc:
        logger.debug("Re-grabbed but couldn't resolve issue %s: %s", issue_id, exc)

    note = f"🎯 {result.message} Re-grabbed & resolved by {interaction.user.mention}."
    try:
        await interaction.edit_original_response(content=note, view=None)
    except discord.HTTPException:
        await interaction.followup.send(note, ephemeral=True)


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
