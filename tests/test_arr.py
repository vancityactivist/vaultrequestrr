import json

import httpx
import pytest

from vaultrequestrr.arr import ArrClient, ArrError, research_media


def make_client(handler) -> ArrClient:
    return ArrClient("http://radarr:7878", "key", transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_replace_movie_deletes_file_then_searches():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path == "/api/v3/movie/110":
            return httpx.Response(200, json={"id": 110, "movieFile": {"id": 555}})
        if request.url.path == "/api/v3/command":
            assert json.loads(request.content) == {"name": "MoviesSearch", "movieIds": [110]}
            return httpx.Response(201, json={})
        return httpx.Response(200, json={})

    client = make_client(handler)
    try:
        await client.replace_movie(110)
    finally:
        await client.aclose()

    assert ("GET", "/api/v3/movie/110") in calls
    assert ("DELETE", "/api/v3/moviefile/555") in calls
    assert ("POST", "/api/v3/command") in calls


@pytest.mark.asyncio
async def test_replace_movie_without_file_just_searches():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path == "/api/v3/movie/110":
            return httpx.Response(200, json={"id": 110, "hasFile": False})
        return httpx.Response(201, json={})

    client = make_client(handler)
    try:
        await client.replace_movie(110)
    finally:
        await client.aclose()

    assert not any(m == "DELETE" for m, _ in calls)  # nothing to delete
    assert ("POST", "/api/v3/command") in calls


@pytest.mark.asyncio
async def test_replace_episode_targets_single_episode():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path == "/api/v3/episode":
            assert request.url.params["seriesId"] == "302"
            return httpx.Response(200, json=[
                {"id": 899, "seasonNumber": 1, "episodeNumber": 1, "episodeFileId": 776},
                {"id": 900, "seasonNumber": 1, "episodeNumber": 2, "episodeFileId": 777},
                {"id": 901, "seasonNumber": 2, "episodeNumber": 2, "episodeFileId": 778},
            ])
        if request.url.path == "/api/v3/command":
            assert json.loads(request.content) == {"name": "EpisodeSearch", "episodeIds": [900]}
            return httpx.Response(201, json={})
        return httpx.Response(200, json={})

    client = make_client(handler)
    try:
        await client.replace_episode(302, 1, 2)
    finally:
        await client.aclose()

    assert ("DELETE", "/api/v3/episodefile/777") in calls  # only the reported episode
    assert ("POST", "/api/v3/command") in calls


@pytest.mark.asyncio
async def test_replace_episode_missing_episode_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[
            {"id": 899, "seasonNumber": 1, "episodeNumber": 1, "episodeFileId": 776}
        ])

    client = make_client(handler)
    try:
        with pytest.raises(ArrError):
            await client.replace_episode(302, 1, 99)
    finally:
        await client.aclose()


class FakeSeerr:
    def __init__(self, *, external=110):
        self._external = external

    async def get_media_service(self, media_type, tmdb_id):
        return 0, self._external

    async def get_arr_config(self, media_type, service_id):
        return "http://arr:7878", "key"


@pytest.mark.asyncio
async def test_research_media_movie():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/movie/110":
            return httpx.Response(200, json={"id": 110, "movieFile": {"id": 5}})
        return httpx.Response(201, json={})

    msg = await research_media(
        FakeSeerr(), "movie", 603, transport=httpx.MockTransport(handler)
    )
    assert "new search" in msg.lower()


@pytest.mark.asyncio
async def test_research_media_tv_requires_episode():
    with pytest.raises(ArrError):
        await research_media(FakeSeerr(external=302), "tv", 95396)  # no season/episode


@pytest.mark.asyncio
async def test_research_media_tv_with_episode():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/episode":
            return httpx.Response(200, json=[
                {"id": 900, "seasonNumber": 1, "episodeNumber": 2, "episodeFileId": 7}
            ])
        return httpx.Response(201, json={})

    msg = await research_media(
        FakeSeerr(external=302), "tv", 95396, season=1, episode=2,
        transport=httpx.MockTransport(handler),
    )
    assert "S01E02" in msg


@pytest.mark.asyncio
async def test_research_media_errors_when_not_in_arr():
    with pytest.raises(ArrError):
        await research_media(FakeSeerr(external=None), "movie", 603)
