"""Slash command and flow for inviting a friend to Plex.

Flow:
  /invite  ->  (link gate: only linked users may invite)
           ->  enter the friend's Plex email in a modal
           ->  share the server with them via the Plex API (Plex emails them)

Invites are gated three ways: Plex must be connected and invites enabled by an
admin, the inviter must have linked their own Plex/Seerr account, and each user
has a configurable cap on how many friends they can invite (default 3). Plex
itself delivers the invite by email — Discord only triggers it.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..linking import LinkStatus
from ..plex import PlexError
from .requests import INTERACTION_TIMEOUT

if TYPE_CHECKING:
    from ..bot import VaultRequestrr

logger = logging.getLogger(__name__)

DEFAULT_INVITE_LIMIT = 3


class InviteCog(commands.Cog):
    def __init__(self, bot: "VaultRequestrr") -> None:
        self.bot = bot

    # -- settings helpers --------------------------------------------------

    async def _invites_enabled(self) -> bool:
        if self.bot.plex is None:
            return False
        return (await self.bot.store.get_setting("plex_invites_enabled")) == "1"

    async def _global_limit(self) -> int:
        raw = await self.bot.store.get_setting("plex_invite_limit")
        try:
            return int(raw) if raw is not None else DEFAULT_INVITE_LIMIT
        except ValueError:
            return DEFAULT_INVITE_LIMIT

    async def _effective_limit(self, discord_id: str, link=None) -> int:  # type: ignore[no-untyped-def]
        """Per-user override (set by an admin in the dashboard) or the global default."""
        if link is None:
            link = await self.bot.linker.get_link(discord_id)
        if link is not None and link.invite_limit is not None:
            return link.invite_limit
        return await self._global_limit()

    async def _shared_libraries(self) -> list[int]:
        raw = await self.bot.store.get_setting("plex_shared_libraries") or ""
        ids: list[int] = []
        for part in raw.split(","):
            part = part.strip()
            if part:
                try:
                    ids.append(int(part))
                except ValueError:
                    continue
        return ids

    # -- slash command -----------------------------------------------------

    @app_commands.command(name="invite", description="Invite a friend to Plex")
    async def invite(self, interaction: discord.Interaction) -> None:
        if not await self._invites_enabled():
            await interaction.response.send_message(
                "Plex invites aren't enabled right now — ask an admin to set it up.",
                ephemeral=True,
            )
            return

        discord_id = str(interaction.user.id)
        link = await self.bot.linker.get_link(discord_id)
        if link is None:
            # Only linked users may invite. Link first, then offer to continue.
            await interaction.response.send_modal(InviteLinkModal(self))
            return

        limit = await self._effective_limit(discord_id, link)
        used = await self.bot.store.count_invites(discord_id)
        if used >= limit:
            await interaction.response.send_message(
                f"You've used all **{limit}** of your Plex invites.", ephemeral=True
            )
            return

        await interaction.response.send_modal(InviteEmailModal(self, remaining=limit - used))

    # -- submission --------------------------------------------------------

    async def send_invite(self, interaction: discord.Interaction, email: str) -> None:
        """Issue the Plex share. Assumes the interaction is already deferred."""
        discord_id = str(interaction.user.id)
        email = email.strip()

        if self.bot.plex is None:
            await interaction.followup.send(
                "Plex isn't connected anymore — ask an admin.", ephemeral=True
            )
            return

        # Re-check the cap at submit time (the modal may have sat open a while).
        limit = await self._effective_limit(discord_id)
        used = await self.bot.store.count_invites(discord_id)
        if used >= limit:
            await interaction.followup.send(
                f"You've used all **{limit}** of your Plex invites.", ephemeral=True
            )
            return

        try:
            await self.bot.plex.share(email, await self._shared_libraries())
        except PlexError as exc:
            await self.bot.store.add_invite(discord_id, email, status="failed")
            await interaction.followup.send(f"⚠️ {exc}", ephemeral=True)
            return

        await self.bot.store.add_invite(discord_id, email, status="sent")
        remaining = max(limit - (used + 1), 0)
        await interaction.followup.send(
            embed=_invite_success_embed(email, remaining), ephemeral=True
        )


# ---------------------------------------------------------------------------
# Interactive components
# ---------------------------------------------------------------------------


class InviteEmailModal(discord.ui.Modal, title="Invite a friend to Plex"):
    email = discord.ui.TextInput(
        label="Your friend's Plex email",
        placeholder="friend@example.com",
        required=True,
        max_length=200,
    )

    def __init__(self, cog: InviteCog, *, remaining: int) -> None:
        super().__init__()
        self._cog = cog
        if remaining > 0:
            self.email.placeholder = f"friend@example.com  ·  {remaining} invite(s) left"

    async def on_submit(self, interaction: discord.Interaction) -> None:
        value = str(self.email.value).strip()
        if "@" not in value:
            await interaction.response.send_message(
                "⚠️ That doesn't look like an email address. Run `/invite` again.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._cog.send_invite(interaction, value)


class InviteLinkModal(discord.ui.Modal, title="Link your Plex account"):
    identity = discord.ui.TextInput(
        label="Plex username or email",
        placeholder="e.g. yourname or you@example.com",
        required=True,
        max_length=200,
    )

    def __init__(self, cog: InviteCog) -> None:
        super().__init__()
        self._cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        discord_id = str(interaction.user.id)
        result = await self._cog.bot.linker.link(discord_id, str(self.identity.value))

        if result.status is LinkStatus.LINKED:
            # Can't open a modal from a modal submit, so offer a button to continue.
            await interaction.response.send_message(
                "✅ Linked! Now invite your friend:",
                view=InvitePromptView(self._cog),
                ephemeral=True,
            )
        elif result.status is LinkStatus.NOT_FOUND:
            seerr_url = self._cog.bot.seerr_url
            await interaction.response.send_message(
                "❌ No matching Seerr account was found for that Plex username/email.\n"
                f"Please log into Seerr once at {seerr_url} (so you get imported from Plex), "
                "then run `/invite` again.",
                ephemeral=True,
            )
        else:  # ERROR
            await interaction.response.send_message(
                f"⚠️ Couldn't reach Seerr to link your account: {result.message}",
                ephemeral=True,
            )


class InvitePromptView(discord.ui.View):
    """Shown after a first-time link so the user can open the email modal."""

    def __init__(self, cog: InviteCog) -> None:
        super().__init__(timeout=INTERACTION_TIMEOUT)
        self._cog = cog

    @discord.ui.button(label="Invite a friend", style=discord.ButtonStyle.success)
    async def invite(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        discord_id = str(interaction.user.id)
        limit = await self._cog._effective_limit(discord_id)
        used = await self._cog.bot.store.count_invites(discord_id)
        if used >= limit:
            await interaction.response.edit_message(
                content=f"You've used all **{limit}** of your Plex invites.", view=None
            )
            return
        await interaction.response.send_modal(
            InviteEmailModal(self._cog, remaining=limit - used)
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _invite_success_embed(email: str, remaining: int) -> discord.Embed:
    embed = discord.Embed(
        title="✅ Invite sent",
        description=(
            f"Plex will email an invite to **{email}**. They'll need to accept it and "
            "sign in with that Plex account to get access."
        ),
        color=discord.Color.green(),
    )
    embed.set_footer(text=f"{remaining} invite(s) remaining")
    return embed
