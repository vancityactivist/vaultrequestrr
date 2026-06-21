"""Slash command and interactive flow for reporting media issues to Seerr.

Flow:
  /issue  ->  search Seerr (movie + tv)  ->  pick an in-library result
          ->  link gate (first time only)
          ->  pick an issue type (Video/Audio/Subtitle/Other)
          ->  describe the problem in a modal
          ->  file the issue, attributed in-message to the reporter

Issues can only be filed against media Seerr already tracks (it needs the
internal mediaInfo.id), so search results are filtered to in-library titles.
The Seerr API creates the issue under the API key's owner, so the real
reporter is recorded in the message text and tracked locally.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..linking import LinkStatus
from ..seerr import (
    ISSUE_OPEN,
    ISSUE_TYPE_LABELS,
    SearchResult,
    SeerrError,
)
from .requests import (
    INTERACTION_TIMEOUT,
    MAX_SELECT_OPTIONS,
    _PageButton,
    _media_embed,
    _status_emoji_text,
    _truncate,
)

if TYPE_CHECKING:
    from ..bot import VaultRequestrr

logger = logging.getLogger(__name__)


class IssueCog(commands.Cog):
    def __init__(self, bot: "VaultRequestrr") -> None:
        self.bot = bot

    # -- slash command -----------------------------------------------------

    @app_commands.command(name="issue", description="Report a problem with media on the server")
    @app_commands.describe(title="The movie or show you're having a problem with")
    async def issue(self, interaction: discord.Interaction, title: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            results = await self._search_library(title)
        except SeerrError as exc:
            await interaction.followup.send(f"⚠️ {exc}", ephemeral=True)
            return

        if not results:
            await interaction.followup.send(
                f"No media on the server matched **{title}**. Issues can only be "
                "reported for titles already added — request it first if it's missing.",
                ephemeral=True,
            )
            return

        view = IssueResultSelectView(self, results)
        suffix = f" — {view.page_label()}" if len(results) > MAX_SELECT_OPTIONS else ""
        await interaction.followup.send(
            f"Found {len(results)} match(es) for **{title}**, pick the one with the issue{suffix}:",
            view=view,
            ephemeral=True,
        )

    async def _search_library(self, title: str) -> list[SearchResult]:
        """Search both movies and TV, keeping only in-library results."""
        results: list[SearchResult] = []
        for media_type in ("movie", "tv"):
            results.extend(await self.bot.seerr.search(title, media_type))
        return [r for r in results if r.in_library]

    # -- submission --------------------------------------------------------

    async def submit_issue(
        self,
        interaction: discord.Interaction,
        result: SearchResult,
        issue_type: int,
        reporter: str,
        detail: str,
        *,
        season: int | None = None,
        episode: int | None = None,
    ) -> None:
        """File the issue with Seerr. Assumes the interaction is deferred."""
        discord_id = str(interaction.user.id)
        where = f" (S{season:02d}E{episode:02d})" if season is not None else ""
        message = (
            f"Reported by {reporter} (Discord {discord_id}) via VaultRequestrr{where}:\n\n{detail}"
        )
        try:
            created = await self.bot.seerr.create_issue(
                result.media_id,
                issue_type,
                message,
                problem_season=season,
                problem_episode=episode,
            )
        except SeerrError as exc:
            await interaction.followup.send(
                f"⚠️ Couldn't submit the issue: {exc}", ephemeral=True
            )
            return

        issue_id = (created or {}).get("id")
        if issue_id is not None:
            await self.bot.store.add_tracked_issue(
                issue_id=int(issue_id),
                discord_id=discord_id,
                media_type=result.media_type,
                tmdb_id=result.tmdb_id,
                title=result.title,
                issue_type=issue_type,
                message=detail,
                status=ISSUE_OPEN,
                problem_season=season,
                problem_episode=episode,
            )
            # Ping admins (DM + approvals channel) with Re-grab / Resolve buttons.
            await self.bot.notifications.notify_issue_filed(
                int(issue_id),
                media_type=result.media_type,
                tmdb_id=result.tmdb_id,
                title=result.title,
                issue_type=issue_type,
                reporter_label=reporter,
                season=season,
                episode=episode,
                message=detail,
            )

        await interaction.edit_original_response(
            content=None,
            embed=_issue_success_embed(result, issue_type, season, episode),
            view=None,
        )


# ---------------------------------------------------------------------------
# Interactive components
# ---------------------------------------------------------------------------


class IssueResultSelectView(discord.ui.View):
    def __init__(self, cog: IssueCog, results: list[SearchResult]) -> None:
        super().__init__(timeout=INTERACTION_TIMEOUT)
        self._cog = cog
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
        self.add_item(IssueResultSelect(self._cog, page_results))
        if len(self._results) > MAX_SELECT_OPTIONS:
            self.add_item(_PageButton(-1, "◀ Prev", disabled=self._page == 0))
            self.add_item(_PageButton(+1, "Next ▶", disabled=self._page >= self._max_page))

    def page_label(self) -> str:
        return f"Page {self._page + 1}/{self._max_page + 1}"


class IssueResultSelect(discord.ui.Select):
    def __init__(self, cog: IssueCog, results: list[SearchResult]) -> None:
        self._cog = cog
        # Movies and TV can share a tmdb id, so key options by media type + id.
        self._results = {f"{r.media_type}:{r.tmdb_id}": r for r in results}
        options = []
        for key, r in self._results.items():
            emoji, status_text = _status_emoji_text(r.status)
            kind = "📺" if r.media_type == "tv" else "🎬"
            description = " · ".join(
                part for part in (status_text, _truncate(r.overview or "", 80)) if part
            )
            options.append(
                discord.SelectOption(
                    label=_truncate(f"{kind} {r.title}" + (f" ({r.year})" if r.year else ""), 100),
                    value=key,
                    description=_truncate(description, 100) or None,
                    emoji=emoji,
                )
            )
        super().__init__(placeholder="Select a title…", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        result = self._results[self.values[0]]
        link = await self._cog.bot.linker.get_link(str(interaction.user.id))

        if link is None and self._cog.bot.runtime.require_linking:
            await interaction.response.send_modal(IssueLinkModal(self._cog, result))
            return

        reporter = _reporter_name(interaction, link)
        view = IssueTypeView(self._cog, result, reporter)
        await interaction.response.edit_message(
            content="What kind of problem is it?", embed=_media_embed(result), view=view
        )


class IssueLinkModal(discord.ui.Modal, title="Link your Plex account"):
    identity = discord.ui.TextInput(
        label="Plex username or email",
        placeholder="e.g. yourname or you@example.com",
        required=True,
        max_length=200,
    )

    def __init__(self, cog: IssueCog, result: SearchResult) -> None:
        super().__init__()
        self._cog = cog
        self._result = result

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Deferred *update* so we can edit the original picker message in place.
        await interaction.response.defer()
        discord_id = str(interaction.user.id)
        result = await self._cog.bot.linker.link(discord_id, str(self.identity.value))

        if result.status is LinkStatus.LINKED:
            who = result.user.plex_username or result.user.email or f"user #{result.user.id}"
            view = IssueTypeView(self._cog, self._result, who)
            await interaction.edit_original_response(
                content="What kind of problem is it?",
                embed=_media_embed(self._result),
                view=view,
            )
        elif result.status is LinkStatus.NOT_FOUND:
            seerr_url = self._cog.bot.config.seerr_url
            await interaction.followup.send(
                "❌ No matching Seerr account was found for that Plex username/email.\n"
                f"Please log into Seerr once at {seerr_url} (so you get imported from Plex), "
                "then run the command again.",
                ephemeral=True,
            )
        else:  # ERROR
            await interaction.followup.send(
                f"⚠️ Couldn't reach Seerr to link your account: {result.message}",
                ephemeral=True,
            )


class IssueTypeView(discord.ui.View):
    def __init__(self, cog: IssueCog, result: SearchResult, reporter: str) -> None:
        super().__init__(timeout=INTERACTION_TIMEOUT)
        self._cog = cog
        self._result = result
        self._reporter = reporter
        self.add_item(IssueTypeSelect())

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=1)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Cancelled.", embed=None, view=None)


class IssueTypeSelect(discord.ui.Select):
    def __init__(self) -> None:
        options = [
            discord.SelectOption(label=label, value=str(code))
            for code, label in ISSUE_TYPE_LABELS.items()
        ]
        super().__init__(
            placeholder="What kind of problem?", options=options, min_values=1, max_values=1
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: IssueTypeView = self.view  # type: ignore[assignment]
        issue_type = int(self.values[0])
        await interaction.response.send_modal(
            IssueMessageModal(view._cog, view._result, issue_type, view._reporter)
        )


class IssueMessageModal(discord.ui.Modal):
    def __init__(
        self, cog: IssueCog, result: SearchResult, issue_type: int, reporter: str
    ) -> None:
        super().__init__(title="Describe the problem")
        self._cog = cog
        self._result = result
        self._issue_type = issue_type
        self._reporter = reporter

        # For TV, pin the issue to a single episode (needed to re-search just it).
        self._season: discord.ui.TextInput | None = None
        self._episode: discord.ui.TextInput | None = None
        if result.media_type == "tv":
            self._season = discord.ui.TextInput(
                label="Season number", placeholder="e.g. 1", required=True, max_length=3
            )
            self._episode = discord.ui.TextInput(
                label="Episode number", placeholder="e.g. 4", required=True, max_length=3
            )
            self.add_item(self._season)
            self.add_item(self._episode)

        self._detail = discord.ui.TextInput(
            label="What's wrong?",
            placeholder="e.g. No subtitles, audio out of sync, won't play past 20 minutes…",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=1000,
        )
        self.add_item(self._detail)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        season = episode = None
        if self._season is not None:
            try:
                season = int(str(self._season.value).strip())
                episode = int(str(self._episode.value).strip())
            except ValueError:
                await interaction.response.send_message(
                    "⚠️ Season and episode must be numbers. Please run `/issue` again.",
                    ephemeral=True,
                )
                return

        await interaction.response.defer()
        await self._cog.submit_issue(
            interaction,
            self._result,
            self._issue_type,
            self._reporter,
            str(self._detail.value),
            season=season,
            episode=episode,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reporter_name(interaction: discord.Interaction, link) -> str:  # type: ignore[no-untyped-def]
    if link is not None:
        return link.plex_username or link.email or f"user #{link.seerr_user_id}"
    return interaction.user.display_name


def _issue_success_embed(
    result: SearchResult,
    issue_type: int,
    season: int | None = None,
    episode: int | None = None,
) -> discord.Embed:
    title = result.title + (f" ({result.year})" if result.year else "")
    if season is not None:
        title += f" — S{season:02d}E{episode:02d}"
    label = ISSUE_TYPE_LABELS.get(issue_type, "Issue")
    embed = discord.Embed(
        title="✅ Issue reported",
        description=f"Your **{label}** issue for **{title}** has been sent to the team.",
        color=discord.Color.green(),
    )
    if result.poster_url:
        embed.set_thumbnail(url=result.poster_url)
    return embed
