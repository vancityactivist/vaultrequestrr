"""Self-service account linking: resolve a Discord user to a Seerr user."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum, auto

from .seerr import SeerrClient, SeerrError, SeerrUser
from .store import AccountLink, LinkStore

logger = logging.getLogger(__name__)


class LinkStatus(Enum):
    LINKED = auto()
    NOT_FOUND = auto()  # no Seerr user matched the provided Plex identity
    ERROR = auto()  # Seerr could not be reached


@dataclass(frozen=True)
class LinkResult:
    status: LinkStatus
    user: SeerrUser | None = None
    message: str | None = None


class AccountLinker:
    def __init__(self, seerr: SeerrClient, store: LinkStore) -> None:
        self._seerr = seerr
        self._store = store

    async def get_link(self, discord_id: str) -> AccountLink | None:
        return await self._store.get(discord_id)

    async def is_linked(self, discord_id: str) -> bool:
        return await self._store.get(discord_id) is not None

    async def link(self, discord_id: str, plex_identity: str) -> LinkResult:
        """Resolve `plex_identity` to a Seerr user and persist the link.

        On success the link is saved locally and the Discord ID is also
        written back into the Seerr user's notification settings (best
        effort — a write-back failure does not fail the link).
        """
        try:
            user = await self._seerr.find_user_by_plex_identity(plex_identity)
        except SeerrError as exc:
            logger.warning("Seerr lookup failed during linking: %s", exc)
            return LinkResult(LinkStatus.ERROR, message=str(exc))

        if user is None:
            return LinkResult(LinkStatus.NOT_FOUND)

        await self._store.save(
            discord_id=discord_id,
            seerr_user_id=user.id,
            plex_username=user.plex_username,
            email=user.email,
        )

        try:
            await self._seerr.add_discord_id(user.id, discord_id)
        except SeerrError as exc:
            logger.warning(
                "Linked Discord %s -> Seerr user %s locally, but write-back failed: %s",
                discord_id,
                user.id,
                exc,
            )

        return LinkResult(LinkStatus.LINKED, user=user)

    async def unlink(self, discord_id: str) -> None:
        await self._store.remove(discord_id)
