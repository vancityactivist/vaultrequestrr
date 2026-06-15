"""Async client for the Seerr / Overseerr / Jellyseerr API.

Only the handful of endpoints VaultRequestrr needs are implemented:
search, media details, user lookup, per-user notification settings
(for the Discord-ID write-back) and request creation.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

# Seerr media status codes (mediaInfo.status / status4k).
STATUS_UNKNOWN = 1
STATUS_PENDING = 2
STATUS_PROCESSING = 3
STATUS_PARTIALLY_AVAILABLE = 4
STATUS_AVAILABLE = 5

# Seerr request status codes (request.status).
REQUEST_PENDING = 1
REQUEST_APPROVED = 2
REQUEST_DECLINED = 3

# Seerr issue type codes (issue.issueType) and status codes (issue.status).
ISSUE_VIDEO = 1
ISSUE_AUDIO = 2
ISSUE_SUBTITLE = 3
ISSUE_OTHER = 4
ISSUE_OPEN = 1
ISSUE_RESOLVED = 2

ISSUE_TYPE_LABELS = {
    ISSUE_VIDEO: "Video",
    ISSUE_AUDIO: "Audio",
    ISSUE_SUBTITLE: "Subtitle",
    ISSUE_OTHER: "Other",
}

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
    media_id: int | None = None  # internal Seerr media DB id (mediaInfo.id), if in library

    @property
    def in_library(self) -> bool:
        """True when Seerr already tracks this media (required to file an issue)."""
        return self.media_id is not None

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
    available: bool = False
    requested: bool = False


@dataclass(frozen=True)
class TvDetails:
    tmdb_id: int
    title: str
    seasons: list[SeasonInfo] = field(default_factory=list)


@dataclass(frozen=True)
class QuotaStatus:
    limit: int  # 0 means unlimited
    used: int
    remaining: int | None  # None when unlimited
    restricted: bool  # True when the user has hit their limit
    days: int
    reset_at: datetime | None = None  # when the next slot frees (rolling window)

    @property
    def unlimited(self) -> bool:
        return self.limit == 0


@dataclass(frozen=True)
class UserQuota:
    movie: QuotaStatus
    tv: QuotaStatus


def format_quota_line(quota: QuotaStatus) -> str:
    """Human-readable remaining-quota summary, with Discord markdown."""
    if quota.unlimited:
        return "Unlimited"
    line = (
        f"**{quota.remaining}** of **{quota.limit}** left "
        f"({quota.used} used in the last {quota.days} days)"
    )
    if quota.reset_at is not None:
        line += f"\nNext request opens <t:{int(quota.reset_at.timestamp())}:R>"
    return line


@dataclass(frozen=True)
class RequestInfo:
    id: int
    request_status: int | None  # REQUEST_* code
    media_status: int | None  # STATUS_* code
    media_type: str | None
    tmdb_id: int | None


@dataclass(frozen=True)
class ServiceInstance:
    """Safe (no API key) view of a Radarr/Sonarr instance configured in Seerr."""
    kind: str  # "radarr" or "sonarr"
    name: str | None
    hostname: str | None
    port: int | None
    use_ssl: bool
    is_default: bool
    is_4k: bool
    profile: str | None

    @property
    def url(self) -> str:
        scheme = "https" if self.use_ssl else "http"
        return f"{scheme}://{self.hostname}:{self.port}"


@dataclass(frozen=True)
class IssueInfo:
    id: int
    issue_type: int | None  # ISSUE_* type code
    status: int | None  # ISSUE_OPEN / ISSUE_RESOLVED
    media_type: str | None
    tmdb_id: int | None
    created_by_name: str | None
    created_at: str | None


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

    async def search(
        self,
        query: str,
        media_type: MediaType,
        *,
        max_pages: int = 3,
        limit: int = 50,
    ) -> list[SearchResult]:
        # Seerr requires the query value to be fully percent-encoded, including
        # reserved characters like ":" (e.g. "Mission: Impossible"). httpx's
        # default param encoding leaves those untouched, so we encode it
        # ourselves with safe="" and build the query string directly.
        encoded = quote(query, safe="")
        results: list[SearchResult] = []
        seen: set[int] = set()
        page = 1
        total_pages = 1
        while page <= total_pages and page <= max_pages and len(results) < limit:
            data = await self._get(f"search?query={encoded}&page={page}&language=en")
            total_pages = data.get("totalPages") or 1
            for raw in data.get("results", []):
                if raw.get("mediaType") != media_type:
                    continue
                result = _to_search_result(raw)
                # The same title can appear on more than one page; keep it unique
                # so the Discord select doesn't get duplicate option values.
                if result.tmdb_id in seen:
                    continue
                seen.add(result.tmdb_id)
                results.append(result)
            page += 1
        return results[:limit]

    async def get_tv_details(self, tmdb_id: int) -> TvDetails:
        data = await self._get(f"tv/{tmdb_id}")
        media_info = data.get("mediaInfo") or {}

        # Per-season availability comes from mediaInfo.seasons[].status; requested
        # seasons come from pending/approved entries in mediaInfo.requests[].
        season_status = {
            s.get("seasonNumber"): s.get("status")
            for s in (media_info.get("seasons") or [])
        }
        requested_numbers: set[int] = set()
        for req in media_info.get("requests") or []:
            if req.get("status") in (1, 2):  # PENDING or APPROVED
                for s in req.get("seasons") or []:
                    requested_numbers.add(s.get("seasonNumber"))

        seasons = []
        for s in data.get("seasons", []):
            number = s.get("seasonNumber", 0)
            if number <= 0:
                continue
            status = season_status.get(number)
            available = status == STATUS_AVAILABLE
            requested = number in requested_numbers or status in (
                STATUS_PENDING,
                STATUS_PROCESSING,
                STATUS_PARTIALLY_AVAILABLE,
            )
            seasons.append(
                SeasonInfo(
                    season_number=number,
                    name=s.get("name"),
                    available=available,
                    requested=requested and not available,
                )
            )

        return TvDetails(
            tmdb_id=tmdb_id,
            title=data.get("name") or data.get("title") or str(tmdb_id),
            seasons=seasons,
        )

    async def get_poster_url(self, media_type: MediaType, tmdb_id: int) -> str | None:
        """Resolve the TMDB poster URL for a movie or TV title, or None."""
        endpoint = "tv" if media_type == "tv" else "movie"
        try:
            data = await self._get(f"{endpoint}/{tmdb_id}")
        except SeerrError as exc:
            logger.debug("Could not load poster for %s/%s: %s", endpoint, tmdb_id, exc)
            return None
        poster = data.get("posterPath")
        return f"https://image.tmdb.org/t/p/w500{poster}" if poster else None

    async def get_quota(self, user_id: int) -> UserQuota:
        data = await self._get(f"user/{user_id}/quota")
        movie = _to_quota(data.get("movie") or {})
        tv = _to_quota(data.get("tv") or {})

        # For limited quotas with usage, compute when the next slot frees: the
        # oldest request still inside the rolling window, plus the window length.
        if (movie.limit and movie.used) or (tv.limit and tv.used):
            resets = await self._compute_quota_resets(user_id, movie, tv)
            movie = replace(movie, reset_at=resets.get("movie"))
            tv = replace(tv, reset_at=resets.get("tv"))

        return UserQuota(movie=movie, tv=tv)

    async def _compute_quota_resets(
        self, user_id: int, movie: QuotaStatus, tv: QuotaStatus
    ) -> dict[str, datetime | None]:
        try:
            data = await self._get(
                f"user/{user_id}/requests", params={"take": 100, "skip": 0}
            )
        except SeerrError as exc:
            logger.debug("Could not load requests for quota reset: %s", exc)
            return {}

        now = datetime.now(timezone.utc)
        results = data.get("results", [])
        out: dict[str, datetime | None] = {}
        for media_type, quota in (("movie", movie), ("tv", tv)):
            if not (quota.limit and quota.used and quota.days):
                continue
            cutoff = now - timedelta(days=quota.days)
            in_window = [
                ts
                for r in results
                if r.get("type") == media_type
                and (ts := _parse_dt(r.get("createdAt"))) is not None
                and ts >= cutoff
            ]
            if in_window:
                out[media_type] = min(in_window) + timedelta(days=quota.days)
        return out

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

    async def get_request(self, request_id: int) -> RequestInfo:
        data = await self._get(f"request/{request_id}")
        media = data.get("media") or {}
        return RequestInfo(
            id=data.get("id", request_id),
            request_status=data.get("status"),
            media_status=media.get("status"),
            media_type=media.get("mediaType"),
            tmdb_id=media.get("tmdbId"),
        )

    # -- issues ------------------------------------------------------------

    async def create_issue(
        self, media_id: int, issue_type: int, message: str
    ) -> dict[str, Any]:
        """Report an issue against an in-library media item.

        `media_id` is the internal Seerr media DB id (mediaInfo.id), not a tmdbId.
        The issue is attributed to the API key's owner; the reporter is recorded
        in the message text by the caller.
        """
        return await self._post(
            "issue",
            {"issueType": issue_type, "message": message, "mediaId": media_id},
        )

    async def list_issues(self, *, take: int = 100) -> list[IssueInfo]:
        # `filter=all` is required — Seerr defaults to open issues only, which
        # would hide the resolved transitions the notifier and dashboard need.
        data = await self._get(
            "issue", params={"take": take, "skip": 0, "sort": "modified", "filter": "all"}
        )
        return [_to_issue_info(raw) for raw in data.get("results", [])]

    async def update_issue_status(self, issue_id: int, *, resolved: bool) -> None:
        """Resolve or reopen an issue."""
        await self._post(f"issue/{issue_id}/{'resolved' if resolved else 'open'}", None)

    # -- arr (Radarr/Sonarr) integration -----------------------------------

    async def get_media_service(
        self, media_type: MediaType, tmdb_id: int
    ) -> tuple[int | None, int | None]:
        """Return (serviceId, externalServiceId) for a title's Radarr/Sonarr item.

        serviceId selects which configured arr instance holds it; the external id
        is that item's movieId/seriesId inside the arr. Either may be None when the
        media isn't managed by an arr.
        """
        endpoint = "tv" if media_type == "tv" else "movie"
        data = await self._get(f"{endpoint}/{tmdb_id}")
        media_info = data.get("mediaInfo") or {}
        return media_info.get("serviceId"), media_info.get("externalServiceId")

    async def list_service_instances(self, kind: str) -> list["ServiceInstance"]:
        """List configured Radarr/Sonarr instances (no API keys), for display."""
        data = await self._get(f"settings/{kind}")
        return [_to_service_instance(kind, raw) for raw in (data or [])]

    async def get_arr_config(
        self, media_type: MediaType, service_id: int | None
    ) -> tuple[str, str]:
        """Resolve the (base_url, api_key) for the arr instance holding the media.

        Reads Seerr's own Radarr/Sonarr connection settings — the same creds Seerr
        uses to talk to them — so no separate configuration is needed.
        """
        kind = "sonarr" if media_type == "tv" else "radarr"
        instances = await self._get(f"settings/{kind}")
        match = next((s for s in instances if s.get("id") == service_id), None)
        if match is None and instances:
            match = next((s for s in instances if s.get("isDefault")), instances[0])
        if not match:
            raise SeerrError(f"No {kind.title()} instance is configured in Seerr")

        scheme = "https" if match.get("useSsl") else "http"
        base_url = f"{scheme}://{match['hostname']}:{match['port']}{match.get('baseUrl') or ''}"
        return base_url, match["apiKey"]


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
        media_id=media_info.get("id"),
    )


def _to_service_instance(kind: str, raw: dict[str, Any]) -> ServiceInstance:
    return ServiceInstance(
        kind=kind,
        name=raw.get("name"),
        hostname=raw.get("hostname"),
        port=raw.get("port"),
        use_ssl=bool(raw.get("useSsl")),
        is_default=bool(raw.get("isDefault")),
        is_4k=bool(raw.get("is4k")),
        profile=raw.get("activeProfileName"),
    )


def _to_issue_info(raw: dict[str, Any]) -> IssueInfo:
    media = raw.get("media") or {}
    created_by = raw.get("createdBy") or {}
    return IssueInfo(
        id=raw.get("id"),
        issue_type=raw.get("issueType"),
        status=raw.get("status"),
        media_type=media.get("mediaType"),
        tmdb_id=media.get("tmdbId"),
        created_by_name=created_by.get("displayName") or created_by.get("username"),
        created_at=raw.get("createdAt"),
    )


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _to_quota(raw: dict[str, Any]) -> QuotaStatus:
    limit = raw.get("limit") or 0
    used = raw.get("used") or 0
    remaining = raw.get("remaining")
    if remaining is None and limit:
        remaining = max(limit - used, 0)
    return QuotaStatus(
        limit=limit,
        used=used,
        remaining=remaining,
        restricted=bool(raw.get("restricted")),
        days=raw.get("days") or 0,
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
