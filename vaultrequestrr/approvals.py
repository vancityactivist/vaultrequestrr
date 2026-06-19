"""Admin approval workflow: persistent Approve/Decline buttons and shared logic.

The buttons are persistent (custom_id encodes the request id, registered via
`bot.add_dynamic_items` in setup_hook) so an admin can act on a pending request
any time — minutes or days later, across bot restarts.
"""
from __future__ import annotations

import logging
import re

import discord

from .seerr import REQUEST_APPROVED, REQUEST_DECLINED, SeerrError
from .store import TrackedRequest

logger = logging.getLogger(__name__)


def build_approval_view(request_id: int) -> discord.ui.View:
    """A persistent (timeout=None) view with this request's Approve/Decline buttons."""
    view = discord.ui.View(timeout=None)
    view.add_item(ApproveButton(request_id))
    view.add_item(DeclineButton(request_id))
    return view


async def build_approval_embeds(
    bot,  # type: ignore[no-untyped-def]
    *,
    media_type: str | None,
    tmdb_id: int | None,
    title: str | None,
    requester_label: str | None,
    seasons: str | None,
) -> list[discord.Embed]:
    """Banner (poster) + details embed describing a request that needs approval."""
    title = title or "Untitled request"
    kind = "📺 TV show" if media_type == "tv" else "🎬 Movie"
    heading = "🆕 Approval needed"
    color = discord.Color.gold()

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
    if requester_label:
        body.add_field(name="Requested by", value=requester_label, inline=True)
    if seasons:
        pretty = "All" if seasons == "all" else seasons
        body.add_field(name="Seasons", value=pretty, inline=True)
    body.set_footer(text="VaultRequestrr")
    embeds.append(body)
    return embeds


async def act_on(
    bot,  # type: ignore[no-untyped-def]
    interaction: discord.Interaction,
    request_id: int,
    *,
    approve: bool,
) -> None:
    """Approve or decline a request from a button press, gated to admins."""
    if not await bot.is_admin(interaction.user.id):
        await interaction.response.send_message(
            "⛔ You're not an approver.", ephemeral=True
        )
        return

    verb = "approve" if approve else "decline"
    try:
        if approve:
            await bot.seerr.approve_request(request_id)
        else:
            await bot.seerr.decline_request(request_id)
    except SeerrError as exc:
        await interaction.response.send_message(
            f"⚠️ Couldn't {verb} that request (it may already be handled): {exc}",
            ephemeral=True,
        )
        return

    # Reflect the decision locally; suppress a duplicate "declined" DM from the poller.
    try:
        await bot.store.mark_tracked(
            request_id,
            request_status=REQUEST_APPROVED if approve else REQUEST_DECLINED,
            notified_declined=None if approve else True,
        )
    except Exception:  # noqa: BLE001 - never fail the action on a bookkeeping error
        logger.debug("Could not update tracked request %s", request_id, exc_info=True)

    emoji = "✅" if approve else "❌"
    note = f"{emoji} {'Approved' if approve else 'Declined'} by {interaction.user.mention}"
    try:
        await interaction.response.edit_message(content=note, view=None)
    except discord.HTTPException:
        # e.g. another admin already edited it; acknowledge so the click doesn't error.
        try:
            await interaction.response.send_message(note, ephemeral=True)
        except discord.HTTPException:
            pass

    # Tell the requester the outcome (best-effort; only for bot-made requests).
    tracked = await bot.store.get_tracked(request_id)
    if tracked is not None:
        await _dm_requester_decision(bot, tracked, approved=approve)


async def _dm_requester_decision(
    bot,  # type: ignore[no-untyped-def]
    tracked: TrackedRequest,
    *,
    approved: bool,
) -> None:
    try:
        user = await bot.fetch_user(int(tracked.discord_id))
    except (discord.NotFound, discord.HTTPException, ValueError) as exc:
        logger.warning("Could not resolve requester %s: %s", tracked.discord_id, exc)
        return

    title = tracked.title or "Your request"
    if approved:
        heading = "✅ Request approved"
        description = f"Your request for **{title}** was approved — it's on the way! 🍿"
        color = discord.Color.green()
    else:
        heading = "❌ Request declined"
        description = f"Your request for **{title}** was declined."
        color = discord.Color.red()
    embed = discord.Embed(title=heading, description=description, color=color)
    embed.set_footer(text="VaultRequestrr")
    try:
        await user.send(embed=embed)
    except discord.Forbidden:
        logger.info("Requester %s has DMs disabled; skipping", tracked.discord_id)
    except discord.HTTPException as exc:
        logger.warning("Failed to DM requester %s: %s", tracked.discord_id, exc)


class ApproveButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"vr:approve:(?P<rid>\d+)",
):
    def __init__(self, request_id: int) -> None:
        self.request_id = request_id
        super().__init__(
            discord.ui.Button(
                label="Approve",
                style=discord.ButtonStyle.success,
                custom_id=f"vr:approve:{request_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):  # type: ignore[no-untyped-def]
        return cls(int(match["rid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await act_on(interaction.client, interaction, self.request_id, approve=True)


class DeclineButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"vr:decline:(?P<rid>\d+)",
):
    def __init__(self, request_id: int) -> None:
        self.request_id = request_id
        super().__init__(
            discord.ui.Button(
                label="Decline",
                style=discord.ButtonStyle.danger,
                custom_id=f"vr:decline:{request_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):  # type: ignore[no-untyped-def]
        return cls(int(match["rid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await act_on(interaction.client, interaction, self.request_id, approve=False)
