import json

import httpx
import pytest

from vaultrequestrr.seerr import (
    ISSUE_RESOLVED,
    ISSUE_VIDEO,
    SeerrClient,
    SeerrError,
    SeerrUser,
)


USERS_PAYLOAD = {
    "results": [
        {"id": 1, "displayName": "Alice", "username": "alice", "plexUsername": "alice_plex", "email": "alice@example.com"},
        {"id": 2, "displayName": "Bob", "username": "bob", "plexUsername": "bobby", "email": "bob@example.com"},
    ]
}


def make_client(handler) -> SeerrClient:
    return SeerrClient("http://seerr:5055", "key", transport=httpx.MockTransport(handler))


# -- SeerrUser.matches -----------------------------------------------------


def test_user_matches_various_fields():
    user = SeerrUser(1, "Alice", "alice", "alice_plex", "alice@example.com")
    assert user.matches("alice_plex")
    assert user.matches("ALICE@EXAMPLE.COM")
    assert user.matches("alice")
    assert not user.matches("nobody")


# -- find_user_by_plex_identity priority -----------------------------------


@pytest.mark.asyncio
async def test_find_user_prefers_email_then_plex():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/user"
        return httpx.Response(200, json=USERS_PAYLOAD)

    client = make_client(handler)
    try:
        by_email = await client.find_user_by_plex_identity("bob@example.com")
        assert by_email.id == 2
        by_plex = await client.find_user_by_plex_identity("alice_plex")
        assert by_plex.id == 1
        missing = await client.find_user_by_plex_identity("ghost")
        assert missing is None
    finally:
        await client.aclose()


# -- search query encoding -------------------------------------------------


@pytest.mark.asyncio
async def test_search_fully_encodes_reserved_characters():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"results": []})

    client = make_client(handler)
    try:
        await client.search("Mission: Impossible", "movie")
    finally:
        await client.aclose()

    # The colon and space must be percent-encoded, not left raw (Seerr 400s otherwise),
    # and must not be double-encoded (%253A).
    assert "query=Mission%3A%20Impossible" in captured["url"]


@pytest.mark.asyncio
async def test_search_fetches_multiple_pages():
    pages = {
        1: {"page": 1, "totalPages": 2, "results": [
            {"id": 1, "mediaType": "movie", "title": "A"},
            {"id": 2, "mediaType": "tv", "name": "skip me"},
        ]},
        2: {"page": 2, "totalPages": 2, "results": [
            {"id": 3, "mediaType": "movie", "title": "B"},
        ]},
    }
    seen_pages = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        seen_pages.append(page)
        return httpx.Response(200, json=pages[page])

    client = make_client(handler)
    try:
        results = await client.search("x", "movie")
    finally:
        await client.aclose()

    assert seen_pages == [1, 2]  # walked both pages
    assert [r.tmdb_id for r in results] == [1, 3]  # tv filtered out, movies combined


@pytest.mark.asyncio
async def test_search_dedupes_across_pages():
    # Same title (id 7) appears on both pages — must be returned only once.
    pages = {
        1: {"page": 1, "totalPages": 2, "results": [{"id": 7, "mediaType": "movie", "title": "Dup"}]},
        2: {"page": 2, "totalPages": 2, "results": [
            {"id": 7, "mediaType": "movie", "title": "Dup"},
            {"id": 8, "mediaType": "movie", "title": "Other"},
        ]},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pages[int(request.url.params["page"])])

    client = make_client(handler)
    try:
        results = await client.search("dup", "movie")
    finally:
        await client.aclose()

    assert [r.tmdb_id for r in results] == [7, 8]  # 7 not repeated


# -- create_request body ---------------------------------------------------


@pytest.mark.asyncio
async def test_create_movie_request_body():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        assert request.headers["X-Api-Key"] == "key"
        return httpx.Response(201, json={"id": 99})

    client = make_client(handler)
    try:
        await client.create_request("movie", 603, user_id=7)
    finally:
        await client.aclose()

    assert captured["body"] == {"mediaType": "movie", "mediaId": 603, "is4k": False, "userId": 7}


@pytest.mark.asyncio
async def test_create_tv_request_includes_seasons():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": 1})

    client = make_client(handler)
    try:
        await client.create_request("tv", 1399, user_id=3, seasons=[1, 2])
    finally:
        await client.aclose()

    assert captured["body"]["seasons"] == [1, 2]
    assert captured["body"]["userId"] == 3


@pytest.mark.asyncio
async def test_create_tv_request_defaults_to_all_seasons():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": 1})

    client = make_client(handler)
    try:
        await client.create_request("tv", 1399, user_id=3, seasons="all")
    finally:
        await client.aclose()

    assert captured["body"]["seasons"] == "all"


@pytest.mark.asyncio
async def test_create_request_includes_routing_overrides():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": 5})

    client = make_client(handler)
    try:
        await client.create_request(
            "tv", 1399, user_id=3, seasons="all",
            server_id=2, profile_id=7, root_folder="/tv/anime",
        )
    finally:
        await client.aclose()

    assert captured["body"]["serverId"] == 2
    assert captured["body"]["profileId"] == 7
    assert captured["body"]["rootFolder"] == "/tv/anime"


@pytest.mark.asyncio
async def test_create_request_omits_unset_routing_overrides():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": 6})

    client = make_client(handler)
    try:
        await client.create_request("movie", 603, user_id=7)
    finally:
        await client.aclose()

    for field in ("serverId", "profileId", "rootFolder", "languageProfileId", "tags"):
        assert field not in captured["body"]


# -- add_discord_id merge --------------------------------------------------


@pytest.mark.asyncio
async def test_add_discord_id_merges_existing_settings():
    state = {
        "settings": {"pgpKey": "abc", "discordIds": ["111"], "telegramChatId": "t"},
        "posted": None,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=state["settings"])
        # POST
        state["posted"] = json.loads(request.content)
        return httpx.Response(200, json=state["posted"])

    client = make_client(handler)
    try:
        await client.add_discord_id(5, "222")
    finally:
        await client.aclose()

    posted = state["posted"]
    assert posted["pgpKey"] == "abc"  # untouched
    assert posted["telegramChatId"] == "t"  # untouched
    assert set(posted["discordIds"]) == {"111", "222"}  # merged
    assert posted["discordId"] == "222"  # legacy field set


# -- get_tv_details season status ------------------------------------------


@pytest.mark.asyncio
async def test_tv_details_marks_available_and_requested_seasons():
    payload = {
        "name": "Test Show",
        "seasons": [
            {"seasonNumber": 0, "name": "Specials"},  # ignored
            {"seasonNumber": 1, "name": "Season 1"},
            {"seasonNumber": 2, "name": "Season 2"},
            {"seasonNumber": 3, "name": "Season 3"},
        ],
        "mediaInfo": {
            "seasons": [{"seasonNumber": 1, "status": 5}],  # S1 available
            "requests": [{"status": 1, "seasons": [{"seasonNumber": 2}]}],  # S2 requested
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = make_client(handler)
    try:
        details = await client.get_tv_details(123)
    finally:
        await client.aclose()

    by_num = {s.season_number: s for s in details.seasons}
    assert set(by_num) == {1, 2, 3}  # special (0) excluded
    assert by_num[1].available and not by_num[1].requested
    assert by_num[2].requested and not by_num[2].available
    assert not by_num[3].available and not by_num[3].requested


# -- get_quota -------------------------------------------------------------


@pytest.mark.asyncio
async def test_quota_unlimited_and_limited():
    payload = {
        "movie": {"days": 7, "limit": 0, "used": 3, "restricted": False},
        "tv": {"days": 30, "limit": 5, "used": 2, "restricted": False},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/user/9/quota":
            return httpx.Response(200, json=payload)
        # reset lookup for the limited tv quota
        assert request.url.path == "/api/v1/user/9/requests"
        return httpx.Response(200, json={"results": []})

    client = make_client(handler)
    try:
        quota = await client.get_quota(9)
    finally:
        await client.aclose()

    assert quota.movie.unlimited
    assert quota.movie.remaining is None
    assert not quota.tv.unlimited
    assert quota.tv.remaining == 3  # 5 - 2


@pytest.mark.asyncio
async def test_quota_reset_uses_oldest_in_window_request():
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    in_window_old = (now - timedelta(days=3)).isoformat().replace("+00:00", "Z")
    in_window_new = (now - timedelta(days=1)).isoformat().replace("+00:00", "Z")
    out_of_window = (now - timedelta(days=20)).isoformat().replace("+00:00", "Z")

    quota_payload = {
        "movie": {"days": 7, "limit": 5, "used": 2, "restricted": False},
        "tv": {"days": 7, "limit": 0, "used": 0, "restricted": False},
    }
    requests_payload = {
        "results": [
            {"type": "movie", "createdAt": in_window_new},
            {"type": "movie", "createdAt": in_window_old},
            {"type": "movie", "createdAt": out_of_window},  # ignored, outside window
            {"type": "tv", "createdAt": in_window_new},  # wrong type, ignored
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/quota"):
            return httpx.Response(200, json=quota_payload)
        return httpx.Response(200, json=requests_payload)

    client = make_client(handler)
    try:
        quota = await client.get_quota(9)
    finally:
        await client.aclose()

    assert quota.movie.reset_at is not None
    # reset = oldest in-window (3 days ago) + 7-day window => ~4 days from now
    delta_days = (quota.movie.reset_at - now).total_seconds() / 86400
    assert 3.9 < delta_days < 4.1


# -- search captures the internal media id ---------------------------------


@pytest.mark.asyncio
async def test_search_captures_media_id_for_in_library_items():
    payload = {
        "page": 1,
        "totalPages": 1,
        "results": [
            {"id": 1, "mediaType": "movie", "title": "In library", "mediaInfo": {"id": 42, "status": 5}},
            {"id": 2, "mediaType": "movie", "title": "Not added"},
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = make_client(handler)
    try:
        results = await client.search("x", "movie")
    finally:
        await client.aclose()

    by_id = {r.tmdb_id: r for r in results}
    assert by_id[1].media_id == 42 and by_id[1].in_library
    assert by_id[2].media_id is None and not by_id[2].in_library


# -- issues ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_issue_body():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/issue"
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": 5})

    client = make_client(handler)
    try:
        created = await client.create_issue(42, ISSUE_VIDEO, "no subs")
    finally:
        await client.aclose()

    assert created == {"id": 5}
    assert captured["body"] == {"issueType": ISSUE_VIDEO, "message": "no subs", "mediaId": 42}


@pytest.mark.asyncio
async def test_create_issue_includes_season_and_episode():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": 6})

    client = make_client(handler)
    try:
        await client.create_issue(42, ISSUE_VIDEO, "bad ep", problem_season=1, problem_episode=4)
    finally:
        await client.aclose()

    assert captured["body"]["problemSeason"] == 1
    assert captured["body"]["problemEpisode"] == 4


@pytest.mark.asyncio
async def test_list_issues_parses_results():
    payload = {
        "results": [
            {
                "id": 5,
                "issueType": 1,
                "status": 2,
                "createdAt": "2026-06-15T00:00:00Z",
                "media": {"mediaType": "movie", "tmdbId": 603},
                "createdBy": {"displayName": "Admin"},
            }
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/issue"
        # Must request all issues — Seerr defaults to open-only, hiding resolutions.
        assert request.url.params["filter"] == "all"
        return httpx.Response(200, json=payload)

    client = make_client(handler)
    try:
        issues = await client.list_issues()
    finally:
        await client.aclose()

    assert len(issues) == 1
    issue = issues[0]
    assert issue.id == 5 and issue.status == ISSUE_RESOLVED
    assert issue.tmdb_id == 603 and issue.media_type == "movie"
    assert issue.created_by_name == "Admin"


@pytest.mark.asyncio
async def test_update_issue_status_paths():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={})

    client = make_client(handler)
    try:
        await client.update_issue_status(5, resolved=True)
        await client.update_issue_status(5, resolved=False)
    finally:
        await client.aclose()

    assert seen == ["/api/v1/issue/5/resolved", "/api/v1/issue/5/open"]


# -- arr integration helpers -----------------------------------------------


@pytest.mark.asyncio
async def test_get_media_service_reads_ids():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/movie/603"
        return httpx.Response(200, json={"mediaInfo": {"serviceId": 0, "externalServiceId": 110}})

    client = make_client(handler)
    try:
        assert await client.get_media_service("movie", 603) == (0, 110)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_list_service_instances_omits_api_key():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/settings/radarr"
        return httpx.Response(200, json=[
            {"id": 3, "name": "Radarr", "hostname": "h", "port": 7878, "useSsl": False,
             "isDefault": True, "is4k": False, "activeProfileName": "HD", "apiKey": "secret"},
        ])

    client = make_client(handler)
    try:
        instances = await client.list_service_instances("radarr")
    finally:
        await client.aclose()

    assert len(instances) == 1
    inst = instances[0]
    assert inst.id == 3  # serviceId is captured for instance mapping
    assert inst.url == "http://h:7878" and inst.profile == "HD" and inst.is_default
    # the dataclass has no field that could leak the API key
    assert "secret" not in str(inst)


@pytest.mark.asyncio
async def test_error_response_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "Forbidden"})

    client = make_client(handler)
    try:
        with pytest.raises(SeerrError) as exc:
            await client.create_request("movie", 1, user_id=1)
        assert "Forbidden" in str(exc.value)
    finally:
        await client.aclose()


# -- approval workflow -----------------------------------------------------


@pytest.mark.asyncio
async def test_approve_and_decline_request_hit_right_paths():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={"id": 7, "status": 2})

    client = make_client(handler)
    try:
        await client.approve_request(7)
        await client.decline_request(7)
    finally:
        await client.aclose()
    assert ("POST", "/api/v1/request/7/approve") in seen
    assert ("POST", "/api/v1/request/7/decline") in seen


@pytest.mark.asyncio
async def test_list_pending_requests_parses_and_filters():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/request"
        assert request.url.params.get("filter") == "pending"
        payload = {
            "results": [
                {
                    "id": 11,
                    "type": "tv",
                    "createdAt": "2026-06-18T10:00:00Z",
                    "media": {"mediaType": "tv", "tmdbId": 1396},
                    "requestedBy": {"id": 5, "displayName": "Neo"},
                    "seasons": [{"seasonNumber": 1}, {"seasonNumber": 2}],
                },
            ]
        }
        return httpx.Response(200, json=payload)

    client = make_client(handler)
    try:
        pending = await client.list_pending_requests()
    finally:
        await client.aclose()
    assert len(pending) == 1
    req = pending[0]
    assert req.id == 11 and req.tmdb_id == 1396 and req.media_type == "tv"
    assert req.requested_by_id == 5 and req.requested_by_name == "Neo"
    assert req.seasons == [1, 2]


@pytest.mark.asyncio
async def test_get_title_returns_name_or_title():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/movie/603":
            return httpx.Response(200, json={"title": "The Matrix"})
        return httpx.Response(200, json={"name": "Breaking Bad"})

    client = make_client(handler)
    try:
        assert await client.get_title("movie", 603) == "The Matrix"
        assert await client.get_title("tv", 1396) == "Breaking Bad"
    finally:
        await client.aclose()
