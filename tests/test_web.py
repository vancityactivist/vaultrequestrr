from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from vaultrequestrr.arr import ArrManager
from vaultrequestrr.linking import AccountLinker
from vaultrequestrr.store import LinkStore
from vaultrequestrr.web import WebDashboard


class FakeSeerr:
    def __init__(self):
        self.status_updates = []

    async def test_connection(self):
        return None

    async def list_service_instances(self, kind):
        return []

    async def list_issues(self, *, take=100):
        return []

    async def update_issue_status(self, issue_id, *, resolved):
        self.status_updates.append((issue_id, resolved))

    async def get_media_service(self, media_type, tmdb_id):
        return 0, 110


class FakePlex:
    async def list_libraries(self):
        from vaultrequestrr.plex import PlexLibrary

        return [PlexLibrary(1, "Movies", "movie"), PlexLibrary(2, "TV", "show")]

    async def aclose(self):
        pass


class FakeBot:
    def __init__(self, store):
        self.store = store
        self.seerr = FakeSeerr()
        self.linker = AccountLinker(self.seerr, store)
        self.arr = ArrManager(self)
        self.config = SimpleNamespace(
            web_password="secret",
            web_port=5056,
            seerr_url="http://seerr:5055",
            seerr_api_key="envkey",
        )
        self.runtime = SimpleNamespace(
            require_linking=True,
            notify_on_available=True,
            notify_on_declined=True,
            notify_on_issue_resolved=True,
            log_level="INFO",
        )
        self.applied = None
        self.plex = None
        self.plex_applied = None

    @property
    def seerr_url(self):
        return self.config.seerr_url

    async def apply_seerr_connection(self, url, key):
        self.applied = (url, key)

    async def plex_client_id(self):
        return "cid"

    async def apply_plex_connection(self, token, machine_id):
        self.plex_applied = (token, machine_id)
        self.plex = FakePlex()

    def is_ready(self):
        return True


@pytest.fixture
async def client(tmp_path):
    store = LinkStore(str(tmp_path / "links.sqlite3"))
    await store.connect()
    dash = WebDashboard(FakeBot(store))
    server = TestServer(dash.build_app())
    cli = TestClient(server)
    await cli.start_server()
    try:
        yield cli, store, dash
    finally:
        await cli.close()
        await store.close()


@pytest.mark.asyncio
async def test_requires_auth_redirects_to_login(client):
    cli, _store, _dash = client
    resp = await cli.get("/", allow_redirects=False)
    assert resp.status == 302
    assert resp.headers["Location"] == "/login"


@pytest.mark.asyncio
async def test_wrong_password_rejected(client):
    cli, _store, _dash = client
    resp = await cli.post("/login", data={"password": "nope"}, allow_redirects=False)
    assert resp.status == 302
    assert "error" in resp.headers["Location"]


@pytest.mark.asyncio
async def test_login_then_dashboard_and_links(client):
    cli, store, _dash = client
    await store.save("999", 7, "alice", "alice@example.com")

    # correct password sets a session cookie
    resp = await cli.post("/login", data={"password": "secret"}, allow_redirects=False)
    assert resp.status == 302 and resp.headers["Location"] == "/"

    home = await cli.get("/")
    assert home.status == 200
    text = await home.text()
    assert "Linked users" in text

    links = await cli.get("/links")
    body = await links.text()
    assert "alice" in body and "999" in body


@pytest.mark.asyncio
async def test_settings_toggle_updates_runtime(client):
    cli, _store, dash = client
    await cli.post("/login", data={"password": "secret"})

    # submit with only notify_on_available checked
    await cli.post(
        "/settings",
        data={"notify_on_available": "on", "log_level": "DEBUG"},
        allow_redirects=False,
    )
    rt = dash.bot.runtime
    assert rt.notify_on_available is True
    assert rt.require_linking is False  # unchecked => off
    assert rt.notify_on_declined is False
    assert rt.log_level == "DEBUG"


@pytest.mark.asyncio
async def test_issues_page_lists_and_resolves(client):
    cli, store, dash = client
    await store.save("42", 7, "neo", "neo@example.com")
    await store.add_tracked_issue(5, "42", "movie", 603, "The Matrix", 1, "no subs", 1)
    await cli.post("/login", data={"password": "secret"})

    page = await cli.get("/issues")
    body = await page.text()
    assert "The Matrix" in body and "neo" in body and "Video" in body and "Resolve" in body
    assert "Re-search" in body

    resp = await cli.post(
        "/issues/resolve", data={"issue_id": "5"}, allow_redirects=False
    )
    assert resp.status == 302 and resp.headers["Location"].startswith("/issues")
    assert dash.bot.seerr.status_updates == [(5, True)]
    one = await store.get_tracked_issue(5)
    assert one.status == 2  # ISSUE_RESOLVED


@pytest.mark.asyncio
async def test_issue_research_action_invokes_arr(client, monkeypatch):
    cli, store, _dash = client
    await store.add_tracked_issue(5, "42", "movie", 603, "The Matrix", 1, "bad", 1)
    await cli.post("/login", data={"password": "secret"})

    calls = []

    async def fake_research(media_type, tmdb_id, *, season=None, episode=None):
        calls.append((media_type, tmdb_id, season, episode))
        return "Deleted the current file and started a new search."

    monkeypatch.setattr(_dash.bot.arr, "research", fake_research)

    resp = await cli.post(
        "/issues/research", data={"issue_id": "5"}, allow_redirects=False
    )
    assert resp.status == 302 and resp.headers["Location"].startswith("/issues")
    assert calls == [("movie", 603, None, None)]


@pytest.mark.asyncio
async def test_settings_page_renders(client):
    cli, _store, _dash = client
    await cli.post("/login", data={"password": "secret"})
    page = await cli.get("/settings")
    body = await page.text()
    assert page.status == 200
    assert "Seerr connection" in body and "Bot settings" in body
    assert "Radarr / Sonarr connections" in body  # editable arr manager
    assert "http://seerr:5055" in body  # current URL pre-filled


@pytest.mark.asyncio
async def test_arr_add_and_delete(client, monkeypatch):
    cli, store, dash = client
    await cli.post("/login", data={"password": "secret"})

    async def ok_probe(base_url, api_key):
        return None

    monkeypatch.setattr(dash, "_probe_arr", staticmethod(ok_probe))

    resp = await cli.post(
        "/settings/arr/add",
        data={"kind": "radarr", "label": "Radarr 4K", "base_url": "http://r:7878/",
              "api_key": "abc", "is_4k": "on", "is_default": "on"},
        allow_redirects=False,
    )
    assert resp.status == 302
    instances = await store.list_arr_instances()
    assert len(instances) == 1
    inst = instances[0]
    assert inst.label == "Radarr 4K" and inst.is_4k and inst.is_default
    assert inst.base_url == "http://r:7878"  # trailing slash trimmed

    resp = await cli.post(
        "/settings/arr/delete", data={"id": inst.id}, allow_redirects=False
    )
    assert resp.status == 302
    assert await store.list_arr_instances() == []


@pytest.mark.asyncio
async def test_arr_add_rejects_unreachable(client, monkeypatch):
    cli, store, dash = client
    await cli.post("/login", data={"password": "secret"})

    from vaultrequestrr.arr import ArrError

    async def bad_probe(base_url, api_key):
        raise ArrError("connection refused")

    monkeypatch.setattr(dash, "_probe_arr", staticmethod(bad_probe))

    resp = await cli.post(
        "/settings/arr/add",
        data={"kind": "radarr", "label": "Bad", "base_url": "http://x", "api_key": "k"},
        allow_redirects=False,
    )
    assert resp.status == 302 and "couldn" in resp.headers["Location"].lower()
    assert await store.list_arr_instances() == []  # not persisted


class _FakeProbe:
    """Stand-in for SeerrClient used to validate a connection edit."""

    ok = True

    def __init__(self, url, key):
        _FakeProbe.last = (url, key)

    async def test_connection(self):
        if not _FakeProbe.ok:
            from vaultrequestrr.seerr import SeerrError

            raise SeerrError("bad host")

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_connection_action_validates_and_persists(client, monkeypatch):
    cli, store, dash = client
    await cli.post("/login", data={"password": "secret"})
    monkeypatch.setattr("vaultrequestrr.web.SeerrClient", _FakeProbe)
    _FakeProbe.ok = True

    resp = await cli.post(
        "/settings/connection",
        data={"seerr_url": "http://new:5055/", "seerr_api_key": "newkey"},
        allow_redirects=False,
    )
    assert resp.status == 302
    assert await store.get_setting("seerr_url") == "http://new:5055"  # trailing slash trimmed
    assert await store.get_setting("seerr_api_key") == "newkey"
    assert dash.bot.applied == ("http://new:5055", "newkey")


@pytest.mark.asyncio
async def test_connection_action_rejects_bad_connection(client, monkeypatch):
    cli, store, dash = client
    await cli.post("/login", data={"password": "secret"})
    monkeypatch.setattr("vaultrequestrr.web.SeerrClient", _FakeProbe)
    _FakeProbe.ok = False

    resp = await cli.post(
        "/settings/connection",
        data={"seerr_url": "http://bad:5055", "seerr_api_key": "x"},
        allow_redirects=False,
    )
    assert resp.status == 302 and "connect" in resp.headers["Location"].lower()
    assert await store.get_setting("seerr_url") is None  # not persisted
    assert dash.bot.applied is None  # live client untouched


@pytest.mark.asyncio
async def test_connection_action_keeps_existing_key_when_blank(client, monkeypatch):
    cli, store, dash = client
    await cli.post("/login", data={"password": "secret"})
    await store.set_setting("seerr_api_key", "saved")
    monkeypatch.setattr("vaultrequestrr.web.SeerrClient", _FakeProbe)
    _FakeProbe.ok = True

    await cli.post(
        "/settings/connection",
        data={"seerr_url": "http://new:5055", "seerr_api_key": ""},
        allow_redirects=False,
    )
    # blank key field keeps the stored key, and that's what gets applied
    assert await store.get_setting("seerr_api_key") == "saved"
    assert dash.bot.applied == ("http://new:5055", "saved")


@pytest.mark.asyncio
async def test_logs_page_shows_records(client):
    import logging

    from vaultrequestrr import logbuffer

    cli, _store, _dash = client
    await cli.post("/login", data={"password": "secret"})
    logbuffer.install()
    logging.getLogger("vaultrequestrr.webtest").warning("LOGS-PAGE-MARKER")

    resp = await cli.get("/logs")
    assert resp.status == 200
    text = await resp.text()
    assert "LOGS-PAGE-MARKER" in text


@pytest.mark.asyncio
async def test_unlink_action(client):
    cli, store, _dash = client
    await store.save("999", 7, "alice", "a@e.com")
    await cli.post("/login", data={"password": "secret"})

    await cli.post("/links/unlink", data={"discord_id": "999"}, allow_redirects=False)
    assert await store.get("999") is None


# -- Plex invites ----------------------------------------------------------


class _FakePlexAuth:
    """Stand-in for PlexAuth in the web login/server flow."""

    token = "owner-token"
    servers = None

    def __init__(self, *, transport=None):
        pass

    async def create_pin(self, client_id):
        return 42, "ABCD", "https://app.plex.tv/auth#?clientID=cid&code=ABCD"

    async def check_pin(self, pin_id, client_id, code=None):
        return _FakePlexAuth.token

    async def list_servers(self, token, client_id):
        from vaultrequestrr.plex import PlexServer

        if _FakePlexAuth.servers is not None:
            return _FakePlexAuth.servers
        return [PlexServer("Home", "machine123")]

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_settings_page_shows_login_when_no_plex(client):
    cli, _store, _dash = client
    await cli.post("/login", data={"password": "secret"})
    body = await (await cli.get("/settings")).text()
    assert "Plex Invites" in body and "Login with Plex" in body


@pytest.mark.asyncio
async def test_plex_login_and_poll_persists_token(client, monkeypatch):
    cli, store, _dash = client
    await cli.post("/login", data={"password": "secret"})
    monkeypatch.setattr("vaultrequestrr.web.PlexAuth", _FakePlexAuth)
    _FakePlexAuth.token = "owner-token"

    r = await cli.post("/settings/plex/login")
    d = await r.json()
    assert d["pin_id"] == 42 and "auth_url" in d

    poll = await (await cli.get(f"/settings/plex/poll?pin_id={d['pin_id']}")).json()
    assert poll["authenticated"] is True
    assert await store.get_setting("plex_token") == "owner-token"


@pytest.mark.asyncio
async def test_plex_poll_not_yet_authorised(client, monkeypatch):
    cli, store, _dash = client
    await cli.post("/login", data={"password": "secret"})
    monkeypatch.setattr("vaultrequestrr.web.PlexAuth", _FakePlexAuth)
    _FakePlexAuth.token = None
    try:
        poll = await (await cli.get("/settings/plex/poll?pin_id=42")).json()
        assert poll["authenticated"] is False
        assert await store.get_setting("plex_token") is None
    finally:
        _FakePlexAuth.token = "owner-token"


@pytest.mark.asyncio
async def test_plex_server_action_connects(client, monkeypatch):
    cli, store, dash = client
    await cli.post("/login", data={"password": "secret"})
    await store.set_setting("plex_token", "owner-token")

    resp = await cli.post(
        "/settings/plex/server",
        data={"server": "machine123|Home"},
        allow_redirects=False,
    )
    assert resp.status == 302
    assert await store.get_setting("plex_machine_id") == "machine123"
    assert await store.get_setting("plex_server_name") == "Home"
    assert dash.bot.plex_applied == ("owner-token", "machine123")


@pytest.mark.asyncio
async def test_plex_invite_settings_saved(client):
    cli, store, dash = client
    await cli.post("/login", data={"password": "secret"})
    # Pretend Plex is connected so the controls render and save.
    await store.set_setting("plex_token", "owner-token")
    await store.set_setting("plex_machine_id", "machine123")
    await store.set_setting("plex_server_name", "Home")
    dash.bot.plex = FakePlex()

    page = await (await cli.get("/settings")).text()
    assert "Enable" in page and "Movies" in page  # library list rendered

    resp = await cli.post(
        "/settings/plex",
        data=[("enabled", "on"), ("limit", "5"), ("library", "1"), ("library", "2")],
        allow_redirects=False,
    )
    assert resp.status == 302
    assert await store.get_setting("plex_invites_enabled") == "1"
    assert await store.get_setting("plex_invite_limit") == "5"
    assert await store.get_setting("plex_shared_libraries") == "1,2"


@pytest.mark.asyncio
async def test_plex_disconnect_clears(client):
    cli, store, dash = client
    await cli.post("/login", data={"password": "secret"})
    await store.set_setting("plex_token", "owner-token")
    await store.set_setting("plex_machine_id", "machine123")
    dash.bot.plex = FakePlex()

    resp = await cli.post("/settings/plex/disconnect", allow_redirects=False)
    assert resp.status == 302
    assert not await store.get_setting("plex_token")
    assert not await store.get_setting("plex_machine_id")
    assert dash.bot.plex is None


@pytest.mark.asyncio
async def test_per_user_invite_limit_override(client):
    cli, store, _dash = client
    await store.save("999", 7, "alice", "a@e.com")
    await cli.post("/login", data={"password": "secret"})

    resp = await cli.post(
        "/links/limit", data={"discord_id": "999", "limit": "10"}, allow_redirects=False
    )
    assert resp.status == 302 and resp.headers["Location"].startswith("/links")
    assert (await store.get("999")).invite_limit == 10

    # blank clears the override
    await cli.post("/links/limit", data={"discord_id": "999", "limit": ""})
    assert (await store.get("999")).invite_limit is None


@pytest.mark.asyncio
async def test_links_page_shows_invite_column(client):
    cli, store, _dash = client
    await store.save("999", 7, "alice", "a@e.com")
    await store.add_invite("999", "friend@e.com", status="sent")
    await cli.post("/login", data={"password": "secret"})
    body = await (await cli.get("/links")).text()
    assert "Invites (used / limit)" in body


@pytest.mark.asyncio
async def test_invites_page_lists_sent(client):
    cli, store, _dash = client
    await store.save("999", 7, "alice", "a@e.com")
    await store.add_invite("999", "friend@e.com", status="sent")
    await store.add_invite("999", "dupe@e.com", status="failed")
    await cli.post("/login", data={"password": "secret"})
    body = await (await cli.get("/invites")).text()
    assert "friend@e.com" in body and "dupe@e.com" in body
    assert "alice" in body and "Sent" in body and "Failed" in body


@pytest.mark.asyncio
async def test_dashboard_shows_plex_status(client):
    cli, store, dash = client
    await cli.post("/login", data={"password": "secret"})
    body = await (await cli.get("/")).text()
    assert "Plex" in body and "Not connected" in body
    assert "Invites sent" in body


@pytest.mark.asyncio
async def test_media_page_renders_detail(client, monkeypatch):
    cli, store, dash = client
    await cli.post("/login", data={"password": "secret"})

    from vaultrequestrr.store import ArrInstance

    inst = ArrInstance("a", "radarr", "Radarr 4K", "http://r", "k", True, True, "now")

    async def fake_detail(media_type, tmdb_id, *, season=None, episode=None):
        return {
            "instance": inst, "media_type": media_type, "tmdb_id": tmdb_id,
            "external_id": 110, "episode_id": None, "season": season, "episode": episode,
            "title": "The Matrix", "monitored": True, "has_file": True,
            "quality": "Bluray-1080p", "size": 8_000_000_000, "languages": ["English"],
            "queue": [{"title": "rel", "status": "downloading", "progress": 42, "timeleft": "1h"}],
        }

    monkeypatch.setattr(dash.bot.arr, "media_detail", fake_detail)

    page = await cli.get("/media?type=movie&tmdb=603")
    body = await page.text()
    assert page.status == 200
    assert "The Matrix" in body and "Bluray-1080p" in body
    assert "Radarr 4K" in body and "7.5 GB" in body  # size formatted
    assert "42%" in body  # queue progress


@pytest.mark.asyncio
async def test_media_page_handles_no_instance(client):
    # The real ArrManager runs: with no configured instances it raises ArrError,
    # which the page renders as a friendly message instead of 500ing.
    cli, _store, _dash = client
    await cli.post("/login", data={"password": "secret"})
    page = await cli.get("/media?type=movie&tmdb=603")
    assert page.status == 200
    assert "Radarr" in await page.text()


@pytest.mark.asyncio
async def test_media_research_action(client, monkeypatch):
    cli, _store, dash = client
    await cli.post("/login", data={"password": "secret"})

    calls = []

    async def fake_research(media_type, tmdb_id, *, season=None, episode=None):
        calls.append((media_type, tmdb_id, season, episode))
        return "Deleted the current file and started a new search."

    monkeypatch.setattr(dash.bot.arr, "research", fake_research)
    resp = await cli.post(
        "/media/research",
        data={"type": "tv", "tmdb": "95396", "season": "1", "episode": "2"},
        allow_redirects=False,
    )
    assert resp.status == 302
    loc = resp.headers["Location"]
    assert loc.startswith("/media?") and "tmdb=95396" in loc
    assert calls == [("tv", 95396, 1, 2)]


@pytest.mark.asyncio
async def test_media_search_and_grab(client, monkeypatch):
    cli, _store, dash = client
    await cli.post("/login", data={"password": "secret"})

    async def fake_releases(media_type, tmdb_id, *, season=None, episode=None):
        return [
            {"guid": "g1", "indexer_id": 7, "title": "Matrix.2160p.BluRay",
             "quality": "Bluray-2160p", "size": 50_000_000_000, "seeders": 42,
             "indexer": "MyTracker", "protocol": "torrent"},
        ]

    monkeypatch.setattr(dash.bot.arr, "releases", fake_releases)

    page = await cli.get("/media/search?type=movie&tmdb=603")
    body = await page.text()
    assert page.status == 200
    assert "Matrix.2160p.BluRay" in body and "MyTracker" in body and "42" in body
    assert 'name="guid" value="g1"' in body  # grab form wired

    grabbed = []

    async def fake_grab(media_type, tmdb_id, guid, indexer_id):
        grabbed.append((media_type, tmdb_id, guid, indexer_id))

    monkeypatch.setattr(dash.bot.arr, "grab", fake_grab)
    resp = await cli.post(
        "/media/grab",
        data={"type": "movie", "tmdb": "603", "guid": "g1", "indexer_id": "7"},
        allow_redirects=False,
    )
    assert resp.status == 302 and resp.headers["Location"].startswith("/media?")
    assert grabbed == [("movie", 603, "g1", 7)]
