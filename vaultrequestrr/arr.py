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
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from .store import ArrInstance

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

    @classmethod
    def from_instance(
        cls,
        instance: "ArrInstance",
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> "ArrClient":
        """Build a client from a stored connection (our own credentials)."""
        return cls(instance.base_url, instance.api_key, transport=transport)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def system_status(self) -> dict[str, Any]:
        """Probe the instance; raises ArrError if it can't be reached/authorised."""
        return await self._request("GET", "system/status") or {}

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ArrError(f"Could not reach the download manager: {exc}") from exc
        if response.is_success:
            return response.json() if response.content else None
        raise ArrError(f"Download manager returned {response.status_code}")

    # -- movies (Radarr) ---------------------------------------------------

    async def movie_by_tmdb(self, tmdb_id: int) -> dict[str, Any] | None:
        """Find a movie in this Radarr by its TMDB id (Radarr indexes by tmdbId)."""
        data = await self._request("GET", "movie", params={"tmdbId": tmdb_id})
        return data[0] if data else None

    async def movie(self, movie_id: int) -> dict[str, Any]:
        return await self._request("GET", f"movie/{movie_id}") or {}

    async def replace_movie(self, movie_id: int) -> None:
        """Delete the movie's current file (if any) and search for a new one."""
        movie = await self._request("GET", f"movie/{movie_id}")
        file_id = (movie.get("movieFile") or {}).get("id")
        if file_id:
            await self._request("DELETE", f"moviefile/{file_id}")
        await self._command("MoviesSearch", movieIds=[movie_id])

    # -- episodes (Sonarr) -------------------------------------------------

    async def series(self, series_id: int) -> dict[str, Any]:
        return await self._request("GET", f"series/{series_id}") or {}

    async def episode_file(self, file_id: int) -> dict[str, Any]:
        return await self._request("GET", f"episodefile/{file_id}") or {}

    async def queue_details(
        self, *, movie_id: int | None = None, series_id: int | None = None
    ) -> list[dict[str, Any]]:
        """Active download-queue items for a movie or series (empty if idle)."""
        params: dict[str, Any] = {}
        if movie_id is not None:
            params["movieId"] = movie_id
        if series_id is not None:
            params["seriesId"] = series_id
        return await self._request("GET", "queue/details", params=params) or []

    async def find_episode(
        self, series_id: int, season: int, episode: int
    ) -> dict[str, Any]:
        """Return Sonarr's episode record for a SxxExx, or raise ArrError."""
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
        return match

    async def replace_episode(self, series_id: int, season: int, episode: int) -> None:
        """Delete the reported episode's file (if any) and search for a new one."""
        match = await self.find_episode(series_id, season, episode)
        file_id = match.get("episodeFileId")
        if file_id:
            await self._request("DELETE", f"episodefile/{file_id}")
        await self._command("EpisodeSearch", episodeIds=[match["id"]])

    # -- manual search -----------------------------------------------------

    async def releases(
        self, *, movie_id: int | None = None, episode_id: int | None = None
    ) -> list[dict[str, Any]]:
        """Run an interactive indexer search and return the candidate releases."""
        params: dict[str, Any] = {}
        if movie_id is not None:
            params["movieId"] = movie_id
        if episode_id is not None:
            params["episodeId"] = episode_id
        return await self._request("GET", "release", params=params) or []

    async def grab(self, guid: str, indexer_id: int) -> None:
        """Push a specific release to the download client."""
        await self._request("POST", "release", json={"guid": guid, "indexerId": indexer_id})

    # -- helpers -----------------------------------------------------------

    async def _command(self, name: str, **fields: Any) -> None:
        await self._request("POST", "command", json={"name": name, **fields})


async def research_media(
    instance: "ArrInstance",
    media_type: str,
    external_id: int | None,
    *,
    season: int | None = None,
    episode: int | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """Delete the bad file and kick off a fresh search on a resolved instance.

    ``external_id`` is the movieId/seriesId inside the arr (resolved upstream by
    :class:`ArrManager`). Raises ArrError on failure.
    """
    if external_id is None:
        raise ArrError("This title isn't managed by Radarr/Sonarr, so it can't be re-searched.")

    client = ArrClient.from_instance(instance, transport=transport)
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


def _host_of(base_url: str) -> str:
    from urllib.parse import urlsplit

    return (urlsplit(base_url).hostname or "").lower()


def _file_fields(f: dict[str, Any]) -> tuple[str | None, int | None, list[str]]:
    """Pull (quality name, size bytes, languages) from a movie/episode file dict."""
    quality = ((f.get("quality") or {}).get("quality") or {}).get("name")
    languages = [lang.get("name") for lang in (f.get("languages") or []) if lang.get("name")]
    return quality, f.get("size"), languages


def _release_fields(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "guid": r.get("guid"),
        "indexer_id": r.get("indexerId"),
        "title": r.get("title"),
        "quality": ((r.get("quality") or {}).get("quality") or {}).get("name"),
        "size": r.get("size"),
        "seeders": r.get("seeders"),
        "indexer": r.get("indexer"),
        "protocol": r.get("protocol"),
    }


def _queue_items(
    items: list[dict[str, Any]], *, episode_id: int | None = None
) -> list[dict[str, Any]]:
    """Normalise arr queue/details items (and filter to one episode for TV)."""
    out: list[dict[str, Any]] = []
    for it in items:
        if episode_id is not None and it.get("episodeId") not in (None, episode_id):
            continue
        size = it.get("size") or 0
        left = it.get("sizeleft")
        progress = round((size - left) / size * 100) if size and left is not None else None
        out.append(
            {
                "title": it.get("title"),
                "status": it.get("trackedDownloadState") or it.get("status"),
                "progress": progress,
                "timeleft": it.get("timeleft"),
            }
        )
    return out


class ArrManager:
    """Resolves media to a VaultRequestrr-owned arr instance and acts on it.

    Seerr remains the *id resolver* (it knows which serviceId holds a title and
    the item's internal movieId/seriesId), but every operation runs against our
    own stored credentials — see ``store.list_arr_instances``.
    """

    def __init__(  # type: ignore[no-untyped-def] - bot is VaultRequestrr
        self, bot, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.bot = bot
        self._transport = transport

    def _client(self, instance: "ArrInstance") -> ArrClient:
        return ArrClient.from_instance(instance, transport=self._transport)

    async def resolve(self, media_type: str, tmdb_id: int) -> tuple["ArrInstance", int]:
        """Return (our instance, external_id) for a title, or raise ArrError."""
        kind = "sonarr" if media_type == "tv" else "radarr"
        instances = await self.bot.store.list_arr_instances(kind)
        if not instances:
            raise ArrError(
                f"No {kind.title()} connection is configured in VaultRequestrr — "
                "add one on the Settings page."
            )

        service_id, external_id = await self.bot.seerr.get_media_service(media_type, tmdb_id)

        # Map Seerr's serviceId to one of our instances by hostname.
        seerr_match = None
        try:
            for inst in await self.bot.seerr.list_service_instances(kind):
                if inst.id == service_id:
                    seerr_match = inst
                    break
        except Exception:  # noqa: BLE001 - Seerr mapping is best-effort
            logger.debug("Could not list Seerr %s instances for mapping", kind, exc_info=True)

        instance = self._pick(instances, seerr_match)

        # Movies: if Seerr didn't know the id, ask our Radarr directly by tmdbId.
        if external_id is None and media_type != "tv":
            client = self._client(instance)
            try:
                movie = await client.movie_by_tmdb(tmdb_id)
            finally:
                await client.aclose()
            if movie:
                external_id = movie.get("id")

        if external_id is None:
            raise ArrError(
                "This title isn't managed by Radarr/Sonarr, so it can't be actioned."
            )
        return instance, external_id

    def _pick(self, instances: list["ArrInstance"], seerr_match) -> "ArrInstance":  # type: ignore[no-untyped-def]
        """Choose the best of our instances for a Seerr instance (host > 4k > default)."""
        if seerr_match is not None and seerr_match.hostname:
            host = seerr_match.hostname.lower()
            by_host = next((i for i in instances if _host_of(i.base_url) == host), None)
            if by_host is not None:
                return by_host
        if seerr_match is not None:
            by_4k = next((i for i in instances if i.is_4k == seerr_match.is_4k), None)
            if by_4k is not None:
                return by_4k
        return next((i for i in instances if i.is_default), instances[0])

    async def research(
        self,
        media_type: str,
        tmdb_id: int,
        *,
        season: int | None = None,
        episode: int | None = None,
    ) -> str:
        """Resolve then delete-and-research the title on our own credentials."""
        instance, external_id = await self.resolve(media_type, tmdb_id)
        return await research_media(
            instance, media_type, external_id,
            season=season, episode=episode, transport=self._transport,
        )

    async def media_detail(
        self,
        media_type: str,
        tmdb_id: int,
        *,
        season: int | None = None,
        episode: int | None = None,
    ) -> dict[str, Any]:
        """Direct-from-arr view of a title: file quality/size, monitored, queue."""
        instance, external_id = await self.resolve(media_type, tmdb_id)
        client = self._client(instance)
        episode_id: int | None = None
        quality: str | None = None
        size: int | None = None
        languages: list[str] = []
        try:
            if media_type == "tv":
                series = await client.series(external_id)
                title = series.get("title")
                monitored = bool(series.get("monitored"))
                has_file = False
                if season is not None and episode is not None:
                    ep = await client.find_episode(external_id, season, episode)
                    episode_id = ep.get("id")
                    monitored = bool(ep.get("monitored"))
                    file_id = ep.get("episodeFileId")
                    if file_id:
                        quality, size, languages = _file_fields(
                            await client.episode_file(file_id)
                        )
                        has_file = True
                queue = _queue_items(
                    await client.queue_details(series_id=external_id), episode_id=episode_id
                )
            else:
                movie = await client.movie(external_id)
                title = movie.get("title")
                monitored = bool(movie.get("monitored"))
                has_file = bool(movie.get("hasFile"))
                movie_file = movie.get("movieFile") or {}
                if movie_file:
                    quality, size, languages = _file_fields(movie_file)
                else:
                    size = movie.get("sizeOnDisk")
                queue = _queue_items(await client.queue_details(movie_id=external_id))
        finally:
            await client.aclose()

        return {
            "instance": instance,
            "media_type": media_type,
            "tmdb_id": tmdb_id,
            "external_id": external_id,
            "episode_id": episode_id,
            "season": season,
            "episode": episode,
            "title": title,
            "monitored": monitored,
            "has_file": has_file,
            "quality": quality,
            "size": size,
            "languages": languages,
            "queue": queue,
        }

    async def releases(
        self,
        media_type: str,
        tmdb_id: int,
        *,
        season: int | None = None,
        episode: int | None = None,
    ) -> list[dict[str, Any]]:
        """Interactive indexer search for a title (or a specific TV episode)."""
        instance, external_id = await self.resolve(media_type, tmdb_id)
        client = self._client(instance)
        try:
            if media_type == "tv":
                if season is None or episode is None:
                    raise ArrError("Manual search needs a specific episode.")
                ep = await client.find_episode(external_id, season, episode)
                raw = await client.releases(episode_id=ep["id"])
            else:
                raw = await client.releases(movie_id=external_id)
        finally:
            await client.aclose()
        return [_release_fields(r) for r in raw]

    async def grab(
        self, media_type: str, tmdb_id: int, guid: str, indexer_id: int
    ) -> None:
        """Grab a chosen release on the instance that holds the title."""
        instance, _ = await self.resolve(media_type, tmdb_id)
        client = self._client(instance)
        try:
            await client.grab(guid, indexer_id)
        finally:
            await client.aclose()
