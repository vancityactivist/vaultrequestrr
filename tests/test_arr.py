import json

import httpx
import pytest

from vaultrequestrr.arr import ArrClient, ArrError, research_media
from vaultrequestrr.store import ArrInstance


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


def make_instance(kind="radarr") -> ArrInstance:
    return ArrInstance(
        id="i1", kind=kind, label="Radarr", base_url="http://arr:7878",
        api_key="key", is_4k=False, is_default=True, created_at="now",
    )


@pytest.mark.asyncio
async def test_research_media_movie():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/movie/110":
            return httpx.Response(200, json={"id": 110, "movieFile": {"id": 5}})
        return httpx.Response(201, json={})

    msg = await research_media(
        make_instance(), "movie", 110, transport=httpx.MockTransport(handler)
    )
    assert "new search" in msg.lower()


@pytest.mark.asyncio
async def test_research_media_tv_requires_episode():
    with pytest.raises(ArrError):
        await research_media(make_instance("sonarr"), "tv", 302)  # no season/episode


@pytest.mark.asyncio
async def test_research_media_tv_with_episode():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/episode":
            return httpx.Response(200, json=[
                {"id": 900, "seasonNumber": 1, "episodeNumber": 2, "episodeFileId": 7}
            ])
        return httpx.Response(201, json={})

    msg = await research_media(
        make_instance("sonarr"), "tv", 302, season=1, episode=2,
        transport=httpx.MockTransport(handler),
    )
    assert "S01E02" in msg


@pytest.mark.asyncio
async def test_research_media_errors_when_not_in_arr():
    with pytest.raises(ArrError):
        await research_media(make_instance(), "movie", None)


@pytest.mark.asyncio
async def test_system_status_ok_and_error():
    ok = make_client(lambda r: httpx.Response(200, json={"version": "5.0"}))
    try:
        assert (await ok.system_status())["version"] == "5.0"
    finally:
        await ok.aclose()

    bad = make_client(lambda r: httpx.Response(401, json={}))
    try:
        with pytest.raises(ArrError):
            await bad.system_status()
    finally:
        await bad.aclose()


# -- ArrManager.resolve -----------------------------------------------------

class FakeStore:
    def __init__(self, instances):
        self._instances = instances

    async def list_arr_instances(self, kind=None):
        return [i for i in self._instances if kind is None or i.kind == kind]


class FakeSeerrSvc:
    def __init__(self, *, service_id, external, instances):
        self._service_id = service_id
        self._external = external
        self._instances = instances

    async def get_media_service(self, media_type, tmdb_id):
        return self._service_id, self._external

    async def list_service_instances(self, kind):
        return [i for i in self._instances if i.kind == kind]


def seerr_inst(kind, sid, hostname, is_4k=False):
    from vaultrequestrr.seerr import ServiceInstance

    return ServiceInstance(
        kind=kind, id=sid, name=kind, hostname=hostname, port=7878,
        use_ssl=False, is_default=False, is_4k=is_4k, profile="HD",
    )


def make_bot(our, service_id, external, seerr_instances):
    from types import SimpleNamespace
    from vaultrequestrr.arr import ArrManager

    bot = SimpleNamespace(
        store=FakeStore(our),
        seerr=FakeSeerrSvc(service_id=service_id, external=external, instances=seerr_instances),
    )
    bot.arr = ArrManager(bot)
    return bot


@pytest.mark.asyncio
async def test_resolve_matches_by_hostname():
    hd = ArrInstance("a", "radarr", "HD", "http://10.0.0.10:7878", "k", False, True, "now")
    uhd = ArrInstance("b", "radarr", "4K", "http://10.0.0.11:7878", "k", True, False, "now")
    bot = make_bot(
        [hd, uhd], service_id=2, external=110,
        seerr_instances=[seerr_inst("radarr", 2, "10.0.0.11", is_4k=True)],
    )
    instance, external = await bot.arr.resolve("movie", 603)
    assert instance.id == "b" and external == 110  # matched the 4K host


@pytest.mark.asyncio
async def test_resolve_falls_back_to_default_when_unmapped():
    hd = ArrInstance("a", "sonarr", "HD", "http://h1:8989", "k", False, True, "now")
    uhd = ArrInstance("b", "sonarr", "4K", "http://h2:8989", "k", True, False, "now")
    bot = make_bot([hd, uhd], service_id=9, external=302, seerr_instances=[])
    instance, external = await bot.arr.resolve("tv", 95396)
    assert instance.id == "a" and external == 302  # default Sonarr


@pytest.mark.asyncio
async def test_resolve_errors_without_instances():
    bot = make_bot([], service_id=0, external=110, seerr_instances=[])
    with pytest.raises(ArrError):
        await bot.arr.resolve("movie", 603)


@pytest.mark.asyncio
async def test_media_detail_movie():
    from types import SimpleNamespace
    from vaultrequestrr.arr import ArrManager

    hd = ArrInstance("a", "radarr", "HD", "http://h:7878", "k", False, True, "now")

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/api/v3/movie/110":
            return httpx.Response(200, json={
                "title": "The Matrix", "monitored": True, "hasFile": True,
                "movieFile": {"quality": {"quality": {"name": "Bluray-1080p"}},
                              "size": 8_000_000_000, "languages": [{"name": "English"}]},
            })
        if p == "/api/v3/queue/details":
            return httpx.Response(200, json=[
                {"title": "Matrix.1080p", "size": 100, "sizeleft": 25,
                 "status": "downloading", "timeleft": "00:05:00"}
            ])
        return httpx.Response(200, json={})

    bot = SimpleNamespace(
        store=FakeStore([hd]),
        seerr=FakeSeerrSvc(service_id=0, external=110, instances=[]),
    )
    bot.arr = ArrManager(bot, transport=httpx.MockTransport(handler))
    d = await bot.arr.media_detail("movie", 603)
    assert d["title"] == "The Matrix" and d["has_file"]
    assert d["quality"] == "Bluray-1080p" and d["size"] == 8_000_000_000
    assert d["languages"] == ["English"]
    assert len(d["queue"]) == 1 and d["queue"][0]["progress"] == 75


@pytest.mark.asyncio
async def test_media_detail_tv_episode():
    from types import SimpleNamespace
    from vaultrequestrr.arr import ArrManager

    s = ArrInstance("a", "sonarr", "HD", "http://h:8989", "k", False, True, "now")

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/api/v3/series/302":
            return httpx.Response(200, json={"title": "Show", "monitored": True})
        if p == "/api/v3/episode":
            return httpx.Response(200, json=[
                {"id": 900, "seasonNumber": 1, "episodeNumber": 2,
                 "episodeFileId": 7, "monitored": True}
            ])
        if p == "/api/v3/episodefile/7":
            return httpx.Response(200, json={
                "quality": {"quality": {"name": "WEBDL-720p"}},
                "size": 500_000_000, "languages": [{"name": "English"}],
            })
        if p == "/api/v3/queue/details":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={})

    bot = SimpleNamespace(
        store=FakeStore([s]),
        seerr=FakeSeerrSvc(service_id=0, external=302, instances=[]),
    )
    bot.arr = ArrManager(bot, transport=httpx.MockTransport(handler))
    d = await bot.arr.media_detail("tv", 95396, season=1, episode=2)
    assert d["title"] == "Show" and d["episode_id"] == 900
    assert d["quality"] == "WEBDL-720p" and d["has_file"]


@pytest.mark.asyncio
async def test_releases_and_grab_movie():
    from types import SimpleNamespace
    from vaultrequestrr.arr import ArrManager

    hd = ArrInstance("a", "radarr", "HD", "http://h:7878", "k", False, True, "now")
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, dict(request.url.params)))
        if request.url.path == "/api/v3/release" and request.method == "GET":
            assert request.url.params["movieId"] == "110"
            return httpx.Response(200, json=[
                {"guid": "abc", "indexerId": 2, "title": "Matrix.2160p",
                 "quality": {"quality": {"name": "Bluray-2160p"}}, "size": 50, "seeders": 30,
                 "indexer": "MyTracker", "protocol": "torrent"},
            ])
        if request.url.path == "/api/v3/release" and request.method == "POST":
            assert json.loads(request.content) == {"guid": "abc", "indexerId": 2}
            return httpx.Response(201, json={})
        return httpx.Response(200, json={})

    bot = SimpleNamespace(
        store=FakeStore([hd]),
        seerr=FakeSeerrSvc(service_id=0, external=110, instances=[]),
    )
    bot.arr = ArrManager(bot, transport=httpx.MockTransport(handler))

    rels = await bot.arr.releases("movie", 603)
    assert len(rels) == 1
    assert rels[0]["quality"] == "Bluray-2160p" and rels[0]["seeders"] == 30
    assert rels[0]["guid"] == "abc" and rels[0]["indexer_id"] == 2

    await bot.arr.grab("movie", 603, "abc", 2)
    assert ("POST", "/api/v3/release", {}) in calls


@pytest.mark.asyncio
async def test_releases_tv_requires_episode():
    from types import SimpleNamespace
    from vaultrequestrr.arr import ArrManager

    s = ArrInstance("a", "sonarr", "HD", "http://h:8989", "k", False, True, "now")
    bot = SimpleNamespace(
        store=FakeStore([s]),
        seerr=FakeSeerrSvc(service_id=0, external=302, instances=[]),
    )
    bot.arr = ArrManager(bot, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    with pytest.raises(ArrError):
        await bot.arr.releases("tv", 95396)
