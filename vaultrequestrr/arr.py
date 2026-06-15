"""Minimal Radarr/Sonarr (v3) client for replacing a bad release.

VaultRequestrr never stores arr credentials itself — it reads the Radarr/Sonarr
connection details out of Seerr's own settings at call time (see
``SeerrClient.get_arr_config``).

Radarr/Sonarr can't "blocklist but keep the file" for an already-imported
release (the file still satisfies the quality cutoff, so a search grabs
nothing). The reliable way to force a replacement is therefore to delete the
current file and trigger a fresh search:

* Movie  → delete the movie file, then ``MoviesSearch``.
* TV     → delete the reported episode's file, then ``EpisodeSearch`` for just
           that episode (issues are filed per-episode).
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class ArrError(RuntimeError):
    """Raised when a Radarr/Sonarr request fails or the target can't be found."""


class ArrClient:
    """Thin async client for a single Radarr/Sonarr v3 instance."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/api/v3/",
            headers={"X-Api-Key": api_key, "Accept": "application/json"},
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ArrError(f"Could not reach the download manager: {exc}") from exc
        if response.is_success:
            return response.json() if response.content else None
        raise ArrError(f"Download manager returned {response.status_code}")

    # -- movies (Radarr) ---------------------------------------------------

    async def replace_movie(self, movie_id: int) -> None:
        """Delete the movie's current file (if any) and search for a new one."""
        movie = await self._request("GET", f"movie/{movie_id}")
        file_id = (movie.get("movieFile") or {}).get("id")
        if file_id:
            await self._request("DELETE", f"moviefile/{file_id}")
        await self._command("MoviesSearch", movieIds=[movie_id])

    # -- episodes (Sonarr) -------------------------------------------------

    async def replace_episode(self, series_id: int, season: int, episode: int) -> None:
        """Delete the reported episode's file (if any) and search for a new one."""
        episodes = await self._request("GET", "episode", params={"seriesId": series_id})
        match = next(
            (
                e
                for e in (episodes or [])
                if e.get("seasonNumber") == season and e.get("episodeNumber") == episode
            ),
            None,
        )
        if match is None:
            raise ArrError(f"Sonarr has no S{season:02d}E{episode:02d} for that series.")

        file_id = match.get("episodeFileId")
        if file_id:
            await self._request("DELETE", f"episodefile/{file_id}")
        await self._command("EpisodeSearch", episodeIds=[match["id"]])

    # -- helpers -----------------------------------------------------------

    async def _command(self, name: str, **fields: Any) -> None:
        await self._request("POST", "command", json={"name": name, **fields})


async def research_media(
    seerr,  # type: ignore[no-untyped-def] - SeerrClient, avoids a circular import
    media_type: str,
    tmdb_id: int,
    *,
    season: int | None = None,
    episode: int | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """Delete the bad file and kick off a fresh search. Raises ArrError on failure."""
    service_id, external_id = await seerr.get_media_service(media_type, tmdb_id)
    if external_id is None:
        raise ArrError("This title isn't managed by Radarr/Sonarr, so it can't be re-searched.")

    base_url, api_key = await seerr.get_arr_config(media_type, service_id)
    client = ArrClient(base_url, api_key, transport=transport)
    try:
        if media_type == "tv":
            if season is None or episode is None:
                raise ArrError(
                    "This issue has no episode recorded — re-file it against a specific episode."
                )
            await client.replace_episode(external_id, season, episode)
            return f"Deleted S{season:02d}E{episode:02d} and started a new search for it."

        await client.replace_movie(external_id)
        return "Deleted the current file and started a new search."
    finally:
        await client.aclose()
