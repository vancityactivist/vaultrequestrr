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
    SearchResult,
    SeasonInfo,
    SeerrError,
    TvDetails,
    UserQuota,
    format_quota_line,
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

        view = ResultSelectView(self, media_type, results)
        suffix = f" — {view.page_label()}" if len(results) > MAX_SELECT_OPTIONS else ""
        await interaction.followup.send(
            f"Found {len(results)} result(s) for **{title}**, pick one{suffix}:",
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

        if self.bot.runtime.require_linking:
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

        # Deferred *update* (no new "thinking" message): we'll edit the picker
        # message in place with the result.
        await interaction.response.defer()
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
            created = await self.bot.seerr.create_request(
                media_type,
                result.tmdb_id,
                user_id=user_id,
                seasons=seasons,
            )
        except SeerrError as exc:
            # Keep the picker so the user can retry; report the error separately.
            await interaction.followup.send(
                f"⚠️ Couldn't submit the request: {exc}", ephemeral=True
            )
            return

        # Track it so the poller can DM this user when it lands or is declined.
        request_id = (created or {}).get("id")
        if request_id is not None:
            await self.bot.store.add_tracked_request(
                request_id=int(request_id),
                discord_id=str(interaction.user.id),
                media_type=media_type,
                tmdb_id=result.tmdb_id,
                title=result.title,
                seasons=_seasons_to_str(seasons),
            )

        embeds = _success_embeds(result, seasons)
        body = embeds[-1]  # the details embed sits below the poster banner
        if user_id is not None:
            try:
                quota = await self.bot.seerr.get_quota(user_id)
                line = format_quota_line(quota.movie if media_type == "movie" else quota.tv)
                body.add_field(name="Your remaining quota", value=line, inline=False)
            except SeerrError:
                pass  # never fail a successful request just because quota lookup did

        # Replace the picker message in place with the confirmation.
        await interaction.edit_original_response(content=None, embeds=embeds, view=None)


# ---------------------------------------------------------------------------
# Interactive components
# ---------------------------------------------------------------------------


class ResultSelectView(discord.ui.View):
    def __init__(self, cog: RequestCog, media_type: str, results: list[SearchResult]) -> None:
        super().__init__(timeout=INTERACTION_TIMEOUT)
        self._cog = cog
        self._media_type = media_type
        self._results = results
        self._page = 0
        self._render()

    @property
    def _max_page(self) -> int:
        return max((len(self._results) - 1) // MAX_SELECT_OPTIONS, 0)

    def _render(self) -> None:
        self.clear_items()
        start = self._page * MAX_SELECT_OPTIONS
        page_results = self._results[start : start + MAX_SELECT_OPTIONS]
        self.add_item(ResultSelect(self._cog, self._media_type, page_results))
        if len(self._results) > MAX_SELECT_OPTIONS:
            self.add_item(_PageButton(-1, "◀ Prev", disabled=self._page == 0))
            self.add_item(_PageButton(+1, "Next ▶", disabled=self._page >= self._max_page))

    def page_label(self) -> str:
        return f"Page {self._page + 1}/{self._max_page + 1}"


class _PageButton(discord.ui.Button):
    def __init__(self, delta: int, label: str, *, disabled: bool) -> None:
        super().__init__(label=label, style=discord.ButtonStyle.secondary, disabled=disabled, row=1)
        self._delta = delta

    async def callback(self, interaction: discord.Interaction) -> None:
        view: ResultSelectView = self.view  # type: ignore[assignment]
        view._page = max(0, min(view._page + self._delta, view._max_page))
        view._render()
        await interaction.response.edit_message(
            content=f"Pick a title — {view.page_label()}:", view=view
        )


class ResultSelect(discord.ui.Select):
    def __init__(self, cog: RequestCog, media_type: str, results: list[SearchResult]) -> None:
        self._cog = cog
        self._media_type = media_type
        # De-duplicate by tmdb id so the select never has duplicate option values.
        unique: list[SearchResult] = []
        seen: set[int] = set()
        for r in results:
            if r.tmdb_id in seen:
                continue
            seen.add(r.tmdb_id)
            unique.append(r)
        results = unique

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
        self._available = {s.season_number for s in details.seasons if s.available}
        self._all = {s.season_number for s in details.seasons}
        self.add_item(SeasonSelect(details))
        self.update_request_state()

    def _selected_numbers(self) -> set[int]:
        return set(self._all) if self.selected == "all" else set(self.selected)

    def update_request_state(self) -> None:
        """Disable Request when every selected season is already available."""
        if self._all and self._all <= self._available:
            self.request.disabled = True
            self.request.label = "All seasons available"
        else:
            self.request.disabled = not (self._selected_numbers() - self._available)
            self.request.label = "Request"

    @discord.ui.button(label="Request", style=discord.ButtonStyle.success, row=1)
    async def request(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._cog.handle_request(interaction, "tv", self._result, self.selected)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=1)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Cancelled.", embed=None, view=None)


class SeasonSelect(discord.ui.Select):
    def __init__(self, details: TvDetails) -> None:
        options = [discord.SelectOption(label="All seasons", value="all", default=True)]
        seen: set[int] = set()
        for season in details.seasons:
            if season.season_number in seen:
                continue
            seen.add(season.season_number)
            if len(options) >= MAX_SELECT_OPTIONS:
                break
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
        view.update_request_state()
        await interaction.response.edit_message(view=view)


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

        # Nothing to request if it's already fully available or pending — grey out the button.
        if result.available:
            self.request.disabled = True
            self.request.label = "Already available"
        elif result.requested:
            self.request.disabled = True
            self.request.label = "Already requested"

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
        # Deferred *update* so we can edit the original picker message in place.
        await interaction.response.defer()
        discord_id = str(interaction.user.id)
        result = await self._cog.bot.linker.link(discord_id, str(self.identity.value))

        if result.status is LinkStatus.LINKED:
            # _submit edits the picker message in place with the confirmation.
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


def _success_embeds(result: SearchResult, seasons: list[int] | str | None) -> list[discord.Embed]:
    title = result.title + (f" ({result.year})" if result.year else "")
    heading = "✅ Request submitted"
    description = f"**{title}** has been requested."
    color = discord.Color.green()

    # Discord renders a full-width embed image at the bottom of its embed, so to
    # get prominent artwork *above* the text we stack two embeds: a banner
    # (heading + full-width poster) on top, then the details below it.
    embeds: list[discord.Embed] = []
    if result.poster_url:
        banner = discord.Embed(title=heading, color=color)
        banner.set_image(url=result.poster_url)
        embeds.append(banner)
        body = discord.Embed(description=description, color=color)
    else:
        body = discord.Embed(title=heading, description=description, color=color)

    if isinstance(seasons, list) and seasons:
        body.add_field(name="Seasons", value=", ".join(str(s) for s in seasons))
    elif seasons == "all":
        body.add_field(name="Seasons", value="All")
    embeds.append(body)
    return embeds


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


def _quota_embed(quota: UserQuota) -> discord.Embed:
    embed = discord.Embed(title="Your Seerr request quota", color=discord.Color.blurple())
    embed.add_field(name="🎬 Movies", value=format_quota_line(quota.movie), inline=False)
    embed.add_field(name="📺 TV", value=format_quota_line(quota.tv), inline=False)
    return embed


def _seasons_to_str(seasons: list[int] | str | None) -> str | None:
    if seasons is None:
        return None
    if isinstance(seasons, str):
        return seasons
    return ",".join(str(s) for s in seasons)


def _truncate(text: str, length: int) -> str:
    text = (text or "").strip()
    if len(text) <= length:
        return text
    return text[: length - 1].rstrip() + "…"
