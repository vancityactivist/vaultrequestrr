"""Minimal Radarr/Sonarr (v3) client for re-grabbing a bad release.

VaultRequestrr never stores arr credentials itself — it reads the Radarr/Sonarr
connection details out of Seerr's own settings at call time (see
``SeerrClient.get_arr_config``). The one operation we need is "mark the last
grab as failed", which blocklists that release and makes the arr automatically
search for a replacement.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class ArrError(RuntimeError):
    """Raised when a Radarr/Sonarr request fails or there's nothing to re-grab."""


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

    async def last_grab_id(
        self, *, movie_id: int | None = None, series_id: int | None = None
    ) -> int | None:
        """Return the history id of the most recent *grabbed* release, or None."""
        if movie_id is not None:
            records = await self._request("GET", "history/movie", params={"movieId": movie_id})
        elif series_id is not None:
            records = await self._request("GET", "history/series", params={"seriesId": series_id})
        else:  # pragma: no cover - guarded by callers
            raise ArrError("last_grab_id requires a movie_id or series_id")

        grabs = [h for h in (records or []) if h.get("eventType") == "grabbed"]
        grabs.sort(key=lambda h: h.get("date") or "", reverse=True)
        return grabs[0]["id"] if grabs else None

    async def mark_failed(self, history_id: int) -> None:
        """Blocklist the release behind ``history_id`` and trigger a new search."""
        await self._request("POST", f"history/failed/{history_id}")


async def research_media(
    seerr,  # type: ignore[no-untyped-def] - SeerrClient, avoids a circular import
    media_type: str,
    tmdb_id: int,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """Blocklist the last grab for a title and kick off a fresh search.

    Returns a short human-readable result. Raises ArrError on any failure.
    """
    service_id, external_id = await seerr.get_media_service(media_type, tmdb_id)
    if external_id is None:
        raise ArrError("This title isn't managed by Radarr/Sonarr, so it can't be re-searched.")

    base_url, api_key = await seerr.get_arr_config(media_type, service_id)
    client = ArrClient(base_url, api_key, transport=transport)
    try:
        if media_type == "tv":
            history_id = await client.last_grab_id(series_id=external_id)
        else:
            history_id = await client.last_grab_id(movie_id=external_id)

        if history_id is None:
            raise ArrError("No grabbed release was found to blocklist — nothing to re-search.")

        await client.mark_failed(history_id)
    finally:
        await client.aclose()

    return "Blocklisted the last download and started a new search."
