"""Async client for the Seerr / Overseerr / Jellyseerr API.

Only the handful of endpoints VaultRequestrr needs are implemented:
search, media details, user lookup, per-user notification settings
(for the Discord-ID write-back) and request creation.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

import httpx

logger = logging.getLogger(__name__)

# Seerr media status codes (mediaInfo.status / status4k).
STATUS_UNKNOWN = 1
STATUS_PENDING = 2
STATUS_PROCESSING = 3
STATUS_PARTIALLY_AVAILABLE = 4
STATUS_AVAILABLE = 5

MediaType = str  # "movie" or "tv"


class SeerrError(RuntimeError):
    """Raised when the Seerr API returns an error response."""


@dataclass(frozen=True)
class SearchResult:
    media_type: MediaType
    tmdb_id: int
    title: str
    year: str | None
    overview: str | None
    poster_url: str | None
    status: int | None

    @property
    def available(self) -> bool:
        return self.status == STATUS_AVAILABLE

    @property
    def requested(self) -> bool:
        return self.status in {
            STATUS_PENDING,
            STATUS_PROCESSING,
            STATUS_PARTIALLY_AVAILABLE,
            STATUS_AVAILABLE,
        }


@dataclass(frozen=True)
class SeerrUser:
    id: int
    display_name: str | None
    username: str | None
    plex_username: str | None
    email: str | None

    def matches(self, identity: str) -> bool:
        needle = identity.strip().lower()
        return needle in {
            value.strip().lower()
            for value in (self.plex_username, self.email, self.username, self.display_name)
            if value
        }


@dataclass(frozen=True)
class SeasonInfo:
    season_number: int
    name: str | None = None


@dataclass(frozen=True)
class TvDetails:
    tmdb_id: int
    title: str
    seasons: list[SeasonInfo] = field(default_factory=list)


class SeerrClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/") + "/api/v1/"
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={
                "X-Api-Key": api_key,
                "Accept": "application/json",
            },
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "SeerrClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    # -- low-level helpers -------------------------------------------------

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise SeerrError(f"Could not reach Seerr: {exc}") from exc

        if response.is_success:
            if response.content:
                return response.json()
            return None

        raise SeerrError(_error_message(response))

    async def _get(self, path: str, **kwargs: Any) -> Any:
        return await self._request("GET", path, **kwargs)

    async def _post(self, path: str, json: Any) -> Any:
        return await self._request("POST", path, json=json)

    # -- connectivity ------------------------------------------------------

    async def test_connection(self) -> None:
        """Raise SeerrError if the URL/key are wrong."""
        await self._get("settings/main")

    # -- search & details --------------------------------------------------

    async def search(self, query: str, media_type: MediaType) -> list[SearchResult]:
        data = await self._get(
            "search",
            params={"query": query, "page": 1, "language": "en"},
        )
        results = []
        for raw in data.get("results", []):
            if raw.get("mediaType") != media_type:
                continue
            results.append(_to_search_result(raw))
        return results

    async def get_tv_details(self, tmdb_id: int) -> TvDetails:
        data = await self._get(f"tv/{tmdb_id}")
        seasons = [
            SeasonInfo(
                season_number=s.get("seasonNumber"),
                name=s.get("name"),
            )
            for s in data.get("seasons", [])
            if s.get("seasonNumber", 0) > 0
        ]
        return TvDetails(
            tmdb_id=tmdb_id,
            title=data.get("name") or data.get("title") or str(tmdb_id),
            seasons=seasons,
        )

    # -- users -------------------------------------------------------------

    async def list_users(self) -> list[SeerrUser]:
        # Seerr caps `take`; 1000 covers any realistic Plex friend list.
        data = await self._get("user", params={"take": 1000, "skip": 0, "sort": "created"})
        return [_to_user(raw) for raw in data.get("results", [])]

    async def get_user(self, user_id: int) -> SeerrUser:
        return _to_user(await self._get(f"user/{user_id}"))

    async def find_user_by_plex_identity(self, identity: str) -> SeerrUser | None:
        """Resolve a self-reported Plex username/email to a Seerr user.

        Prefers an exact email match, then plex username, then any field.
        """
        needle = identity.strip().lower()
        users = await self.list_users()

        by_email = [u for u in users if u.email and u.email.lower() == needle]
        if by_email:
            return by_email[0]

        by_plex = [u for u in users if u.plex_username and u.plex_username.lower() == needle]
        if by_plex:
            return by_plex[0]

        for user in users:
            if user.matches(identity):
                return user
        return None

    # -- notification settings (Discord-ID write-back) ---------------------

    async def get_notification_settings(self, user_id: int) -> dict[str, Any]:
        return await self._get(f"user/{user_id}/settings/notifications") or {}

    async def add_discord_id(self, user_id: int, discord_id: str) -> None:
        """Best-effort: store the Discord ID in the user's Seerr settings.

        Reads current settings and merges, so we don't clobber other
        notification preferences. Handles both the legacy `discordId`
        string and the newer `discordIds` list.
        """
        settings = await self.get_notification_settings(user_id)

        discord_ids = list(settings.get("discordIds") or [])
        if discord_id not in discord_ids:
            discord_ids.append(discord_id)

        settings["discordIds"] = discord_ids
        settings["discordId"] = discord_id  # keep legacy field populated too

        await self._post(f"user/{user_id}/settings/notifications", settings)

    # -- requests ----------------------------------------------------------

    async def create_request(
        self,
        media_type: MediaType,
        tmdb_id: int,
        *,
        user_id: int | None = None,
        seasons: Sequence[int] | str | None = None,
        is_4k: bool = False,
    ) -> dict[str, Any]:
        """Create a request, attributed to `user_id` so their quota applies."""
        body: dict[str, Any] = {
            "mediaType": media_type,
            "mediaId": tmdb_id,
            "is4k": is_4k,
        }
        if user_id is not None:
            body["userId"] = user_id
        if media_type == "tv":
            body["seasons"] = list(seasons) if isinstance(seasons, Iterable) and not isinstance(seasons, str) else (seasons or "all")

        return await self._post("request", body)


# -- module-level parsing helpers -----------------------------------------


def _to_search_result(raw: dict[str, Any]) -> SearchResult:
    media_type = raw.get("mediaType")
    media_info = raw.get("mediaInfo") or {}
    date = raw.get("releaseDate") if media_type == "movie" else raw.get("firstAirDate")
    poster = raw.get("posterPath")
    return SearchResult(
        media_type=media_type,
        tmdb_id=raw.get("id"),
        title=raw.get("title") or raw.get("name") or "Unknown",
        year=(date or "")[:4] or None,
        overview=raw.get("overview") or None,
        poster_url=f"https://image.tmdb.org/t/p/w500{poster}" if poster else None,
        status=media_info.get("status"),
    )


def _to_user(raw: dict[str, Any]) -> SeerrUser:
    return SeerrUser(
        id=raw.get("id"),
        display_name=raw.get("displayName"),
        username=raw.get("username"),
        plex_username=raw.get("plexUsername"),
        email=raw.get("email"),
    )


def _error_message(response: httpx.Response) -> str:
    detail: str | None = None
    try:
        payload = response.json()
        if isinstance(payload, dict):
            detail = payload.get("message") or payload.get("error")
    except ValueError:
        detail = None
    return f"Seerr returned {response.status_code}" + (f": {detail}" if detail else "")
