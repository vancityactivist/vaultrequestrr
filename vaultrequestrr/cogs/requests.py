"""Slash commands and interactive flow for requesting media via Seerr.

Flow:
  /movie or /tv  ->  search Seerr  ->  pick a result
                 ->  (TV) pick season(s)
                 ->  Request button  ->  link gate (first time only)
                 ->  submit request attributed to the user's Seerr id
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..linking import LinkStatus
from ..seerr import (
    STATUS_AVAILABLE,
    STATUS_PARTIALLY_AVAILABLE,
    STATUS_PENDING,
    STATUS_PROCESSING,
    QuotaStatus,
    SearchResult,
    SeasonInfo,
    SeerrError,
    TvDetails,
    UserQuota,
)

if TYPE_CHECKING:
    from ..bot import VaultRequestrr

logger = logging.getLogger(__name__)

MAX_SELECT_OPTIONS = 25
INTERACTION_TIMEOUT = 180.0  # seconds before the ephemeral controls expire


class RequestCog(commands.Cog):
    def __init__(self, bot: "VaultRequestrr") -> None:
        self.bot = bot

    # -- slash commands ----------------------------------------------------

    @app_commands.command(name="movie", description="Request a movie via Seerr")
    @app_commands.describe(title="The movie title to search for")
    async def movie(self, interaction: discord.Interaction, title: str) -> None:
        await self._start_search(interaction, "movie", title)

    @app_commands.command(name="tv", description="Request a TV show via Seerr")
    @app_commands.describe(title="The TV show title to search for")
    async def tv(self, interaction: discord.Interaction, title: str) -> None:
        await self._start_search(interaction, "tv", title)

    @app_commands.command(name="linkstatus", description="Show your linked Plex/Seerr account")
    async def linkstatus(self, interaction: discord.Interaction) -> None:
        link = await self.bot.linker.get_link(str(interaction.user.id))
        if link is None:
            await interaction.response.send_message(
                "You're not linked yet. Make a request and you'll be prompted to link.",
                ephemeral=True,
            )
            return
        who = link.plex_username or link.email or f"user #{link.seerr_user_id}"
        await interaction.response.send_message(
            f"You're linked to Seerr account **{who}** (id `{link.seerr_user_id}`).",
            ephemeral=True,
        )

    @app_commands.command(name="unlink", description="Remove your Plex/Seerr account link")
    async def unlink(self, interaction: discord.Interaction) -> None:
        await self.bot.linker.unlink(str(interaction.user.id))
        await interaction.response.send_message(
            "Your account link has been removed. You'll be asked to link again on your next request.",
            ephemeral=True,
        )

    @app_commands.command(name="quota", description="Show your remaining Seerr request quota")
    async def quota(self, interaction: discord.Interaction) -> None:
        link = await self.bot.linker.get_link(str(interaction.user.id))
        if link is None:
            await interaction.response.send_message(
                "You're not linked yet. Make a request and you'll be prompted to link first.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            quota = await self.bot.seerr.get_quota(link.seerr_user_id)
        except SeerrError as exc:
            await interaction.followup.send(f"⚠️ {exc}", ephemeral=True)
            return
        await interaction.followup.send(embed=_quota_embed(quota), ephemeral=True)

    # -- search entry point ------------------------------------------------

    async def _start_search(
        self, interaction: discord.Interaction, media_type: str, title: str
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            results = await self.bot.seerr.search(title, media_type)
        except SeerrError as exc:
            await interaction.followup.send(f"⚠️ {exc}", ephemeral=True)
            return

        if not results:
            await interaction.followup.send(
                f"No {('movies' if media_type == 'movie' else 'TV shows')} found for **{title}**.",
                ephemeral=True,
            )
            return

        view = ResultSelectView(self, media_type, results[:MAX_SELECT_OPTIONS])
        await interaction.followup.send(
            f"Found {len(results[:MAX_SELECT_OPTIONS])} result(s) for **{title}** — pick one:",
            view=view,
            ephemeral=True,
        )

    # -- request submission ------------------------------------------------

    async def handle_request(
        self,
        interaction: discord.Interaction,
        media_type: str,
        result: SearchResult,
        seasons: list[int] | str | None,
    ) -> None:
        """Entry from a Request button: apply the link gate, then submit."""
        discord_id = str(interaction.user.id)

        if self.bot.config.require_linking:
            link = await self.bot.linker.get_link(discord_id)
            if link is None:
                # First request: prompt for Plex identity. The modal continues the flow.
                await interaction.response.send_modal(
                    PlexLinkModal(self, media_type, result, seasons)
                )
                return
            user_id = link.seerr_user_id
        else:
            user_id = self.bot.config.default_seerr_user_id

        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._submit(interaction, media_type, result, seasons, user_id)

    async def _submit(
        self,
        interaction: discord.Interaction,
        media_type: str,
        result: SearchResult,
        seasons: list[int] | str | None,
        user_id: int | None,
    ) -> None:
        """Submit the request to Seerr. Assumes the interaction is deferred."""
        try:
            await self.bot.seerr.create_request(
                media_type,
                result.tmdb_id,
                user_id=user_id,
                seasons=seasons,
            )
        except SeerrError as exc:
            await interaction.followup.send(
                f"⚠️ Couldn't submit the request: {exc}", ephemeral=True
            )
            return

        embed = _success_embed(result, seasons)
        if user_id is not None:
            try:
                quota = await self.bot.seerr.get_quota(user_id)
                line = _quota_line(quota.movie if media_type == "movie" else quota.tv)
                embed.add_field(name="Your remaining quota", value=line, inline=False)
            except SeerrError:
                pass  # never fail a successful request just because quota lookup did

        await interaction.followup.send(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# Interactive components
# ---------------------------------------------------------------------------


class ResultSelectView(discord.ui.View):
    def __init__(self, cog: RequestCog, media_type: str, results: list[SearchResult]) -> None:
        super().__init__(timeout=INTERACTION_TIMEOUT)
        self.add_item(ResultSelect(cog, media_type, results))


class ResultSelect(discord.ui.Select):
    def __init__(self, cog: RequestCog, media_type: str, results: list[SearchResult]) -> None:
        self._cog = cog
        self._media_type = media_type
        self._results = {str(r.tmdb_id): r for r in results}
        options = []
        for r in results:
            emoji, status_text = _status_emoji_text(r.status)
            description = " · ".join(
                part for part in (status_text, _truncate(r.overview or "", 80)) if part
            )
            options.append(
                discord.SelectOption(
                    label=_truncate(f"{r.title}" + (f" ({r.year})" if r.year else ""), 100),
                    value=str(r.tmdb_id),
                    description=_truncate(description, 100) or None,
                    emoji=emoji,
                )
            )
        super().__init__(placeholder="Select a title…", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        result = self._results[self.values[0]]

        if self._media_type == "movie":
            view = ConfirmView(self._cog, "movie", result, seasons=None)
            await interaction.response.edit_message(
                content=None, embed=_media_embed(result), view=view
            )
            return

        # TV: load seasons before showing the season picker.
        await interaction.response.defer()
        try:
            details = await self._cog.bot.seerr.get_tv_details(result.tmdb_id)
        except SeerrError as exc:
            await interaction.edit_original_response(
                content=f"⚠️ {exc}", embed=None, view=None
            )
            return

        if not details.seasons:
            # No discrete seasons — request the whole show.
            view = ConfirmView(self._cog, "tv", result, seasons="all")
            await interaction.edit_original_response(
                content=None, embed=_media_embed(result), view=view
            )
            return

        view = SeasonSelectView(self._cog, result, details)
        await interaction.edit_original_response(
            content=None, embed=_media_embed(result), view=view
        )


class SeasonSelectView(discord.ui.View):
    def __init__(self, cog: RequestCog, result: SearchResult, details: TvDetails) -> None:
        super().__init__(timeout=INTERACTION_TIMEOUT)
        self._cog = cog
        self._result = result
        self.selected: list[int] | str = "all"
        self.add_item(SeasonSelect(details))

    @discord.ui.button(label="Request", style=discord.ButtonStyle.success, row=1)
    async def request(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._cog.handle_request(interaction, "tv", self._result, self.selected)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=1)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Cancelled.", embed=None, view=None)


class SeasonSelect(discord.ui.Select):
    def __init__(self, details: TvDetails) -> None:
        options = [discord.SelectOption(label="All seasons", value="all", default=True)]
        for season in details.seasons[: MAX_SELECT_OPTIONS - 1]:
            emoji, status_text = _season_emoji_text(season)
            options.append(
                discord.SelectOption(
                    label=_truncate(season.name or f"Season {season.season_number}", 100),
                    value=str(season.season_number),
                    description=status_text,
                    emoji=emoji,
                )
            )
        super().__init__(
            placeholder="Choose season(s) (default: all)…",
            options=options,
            min_values=1,
            max_values=len(options),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: SeasonSelectView = self.view  # type: ignore[assignment]
        if "all" in self.values or not self.values:
            view.selected = "all"
        else:
            view.selected = [int(v) for v in self.values]
        await interaction.response.defer()  # acknowledge without changing the message


class ConfirmView(discord.ui.View):
    def __init__(
        self,
        cog: RequestCog,
        media_type: str,
        result: SearchResult,
        seasons: list[int] | str | None,
    ) -> None:
        super().__init__(timeout=INTERACTION_TIMEOUT)
        self._cog = cog
        self._media_type = media_type
        self._result = result
        self._seasons = seasons

    @discord.ui.button(label="Request", style=discord.ButtonStyle.success)
    async def request(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._cog.handle_request(interaction, self._media_type, self._result, self._seasons)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Cancelled.", embed=None, view=None)


class PlexLinkModal(discord.ui.Modal, title="Link your Plex account"):
    identity = discord.ui.TextInput(
        label="Plex username or email",
        placeholder="e.g. yourname or you@example.com",
        required=True,
        max_length=200,
    )

    def __init__(
        self,
        cog: RequestCog,
        media_type: str,
        result: SearchResult,
        seasons: list[int] | str | None,
    ) -> None:
        super().__init__()
        self._cog = cog
        self._media_type = media_type
        self._result = result
        self._seasons = seasons

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        discord_id = str(interaction.user.id)
        result = await self._cog.bot.linker.link(discord_id, str(self.identity.value))

        if result.status is LinkStatus.LINKED:
            who = result.user.plex_username or result.user.email or f"user #{result.user.id}"
            await interaction.followup.send(f"✅ Linked to **{who}**. Submitting your request…", ephemeral=True)
            await self._cog._submit(
                interaction, self._media_type, self._result, self._seasons, result.user.id
            )
        elif result.status is LinkStatus.NOT_FOUND:
            seerr_url = self._cog.bot.config.seerr_url
            await interaction.followup.send(
                "❌ No matching Seerr account was found for that Plex username/email.\n"
                f"Please log into Seerr once at {seerr_url} (so you get imported from Plex), "
                "then run the request again.",
                ephemeral=True,
            )
        else:  # ERROR
            await interaction.followup.send(
                f"⚠️ Couldn't reach Seerr to link your account: {result.message}",
                ephemeral=True,
            )


# ---------------------------------------------------------------------------
# Embed / formatting helpers
# ---------------------------------------------------------------------------


def _media_embed(result: SearchResult) -> discord.Embed:
    title = result.title + (f" ({result.year})" if result.year else "")
    embed = discord.Embed(title=title, description=_truncate(result.overview or "", 400) or None)
    if result.poster_url:
        embed.set_thumbnail(url=result.poster_url)
    if result.available:
        embed.set_footer(text="Already available on your server")
    elif result.requested:
        embed.set_footer(text="Already requested")
    return embed


def _success_embed(result: SearchResult, seasons: list[int] | str | None) -> discord.Embed:
    title = result.title + (f" ({result.year})" if result.year else "")
    embed = discord.Embed(
        title="✅ Request submitted",
        description=f"**{title}** has been requested.",
        color=discord.Color.green(),
    )
    if result.poster_url:
        embed.set_thumbnail(url=result.poster_url)
    if isinstance(seasons, list) and seasons:
        embed.add_field(name="Seasons", value=", ".join(str(s) for s in seasons))
    elif seasons == "all":
        embed.add_field(name="Seasons", value="All")
    return embed


def _status_emoji_text(status: int | None) -> tuple[str | None, str | None]:
    if status == STATUS_AVAILABLE:
        return "✅", "Available"
    if status == STATUS_PARTIALLY_AVAILABLE:
        return "🟡", "Partially available"
    if status == STATUS_PROCESSING:
        return "⏳", "Processing"
    if status == STATUS_PENDING:
        return "🕒", "Requested"
    return None, None


def _season_emoji_text(season: SeasonInfo) -> tuple[str | None, str | None]:
    if season.available:
        return "✅", "Available"
    if season.requested:
        return "🕒", "Requested"
    return None, None


def _quota_line(quota: QuotaStatus) -> str:
    if quota.unlimited:
        return "Unlimited"
    return f"{quota.remaining} of {quota.limit} left ({quota.used} used in the last {quota.days} days)"


def _quota_embed(quota: UserQuota) -> discord.Embed:
    embed = discord.Embed(title="Your Seerr request quota", color=discord.Color.blurple())
    embed.add_field(name="🎬 Movies", value=_quota_line(quota.movie), inline=False)
    embed.add_field(name="📺 TV", value=_quota_line(quota.tv), inline=False)
    return embed


def _truncate(text: str, length: int) -> str:
    text = (text or "").strip()
    if len(text) <= length:
        return text
    return text[: length - 1].rstrip() + "…"
