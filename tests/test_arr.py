import httpx
import pytest

from vaultrequestrr.arr import ArrClient, ArrError, research_media


def make_client(handler) -> ArrClient:
    return ArrClient("http://radarr:7878", "key", transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_last_grab_id_picks_most_recent_grab():
    records = [
        {"id": 1, "eventType": "grabbed", "date": "2026-01-01T00:00:00Z"},
        {"id": 2, "eventType": "downloadFolderImported", "date": "2026-02-01T00:00:00Z"},
        {"id": 3, "eventType": "grabbed", "date": "2026-03-01T00:00:00Z"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/history/movie"
        assert request.url.params["movieId"] == "110"
        assert request.headers["X-Api-Key"] == "key"
        return httpx.Response(200, json=records)

    client = make_client(handler)
    try:
        assert await client.last_grab_id(movie_id=110) == 3  # newest grab, import ignored
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_last_grab_id_none_when_no_grabs():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"id": 9, "eventType": "movieFileDeleted"}])

    client = make_client(handler)
    try:
        assert await client.last_grab_id(movie_id=110) is None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_mark_failed_posts_to_history_failed():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(200, json={})

    client = make_client(handler)
    try:
        await client.mark_failed(3)
    finally:
        await client.aclose()

    assert seen == {"method": "POST", "path": "/api/v3/history/failed/3"}


class FakeSeerr:
    def __init__(self, *, service=(0, 110), media_type="movie"):
        self._service = service
        self._media_type = media_type

    async def get_media_service(self, media_type, tmdb_id):
        return self._service

    async def get_arr_config(self, media_type, service_id):
        return "http://radarr:7878", "key"


@pytest.mark.asyncio
async def test_research_media_marks_last_grab_failed():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path == "/api/v3/history/movie":
            return httpx.Response(200, json=[{"id": 7, "eventType": "grabbed", "date": "2026-03-01"}])
        return httpx.Response(200, json={})

    result = await research_media(
        FakeSeerr(), "movie", 603, transport=httpx.MockTransport(handler)
    )

    assert "new search" in result.lower()
    assert ("GET", "/api/v3/history/movie") in calls
    assert ("POST", "/api/v3/history/failed/7") in calls


@pytest.mark.asyncio
async def test_research_media_uses_series_endpoint_for_tv():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/v3/history/series":
            assert request.url.params["seriesId"] == "42"
            return httpx.Response(200, json=[{"id": 5, "eventType": "grabbed", "date": "2026-03-01"}])
        return httpx.Response(200, json={})

    await research_media(
        FakeSeerr(service=(0, 42)), "tv", 1399, transport=httpx.MockTransport(handler)
    )

    assert "/api/v3/history/series" in calls
    assert "/api/v3/history/failed/5" in calls


@pytest.mark.asyncio
async def test_research_media_errors_when_not_in_arr():
    with pytest.raises(ArrError):
        await research_media(FakeSeerr(service=(None, None)), "movie", 603)


@pytest.mark.asyncio
async def test_research_media_errors_when_no_grab_history():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])  # no history at all

    with pytest.raises(ArrError):
        await research_media(
            FakeSeerr(), "movie", 603, transport=httpx.MockTransport(handler)
        )
