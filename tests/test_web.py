from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

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

    async def get_arr_config(self, media_type, service_id):
        return "http://radarr:7878", "key"


class FakeBot:
    def __init__(self, store):
        self.store = store
        self.seerr = FakeSeerr()
        self.linker = AccountLinker(self.seerr, store)
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

    @property
    def seerr_url(self):
        return self.config.seerr_url

    async def apply_seerr_connection(self, url, key):
        self.applied = (url, key)

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

    async def fake_research(seerr, media_type, tmdb_id, *, season=None, episode=None):
        calls.append((media_type, tmdb_id, season, episode))
        return "Deleted the current file and started a new search."

    monkeypatch.setattr("vaultrequestrr.web.research_media", fake_research)

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
    assert "Seerr connection" in body and "Bot settings" in body and "Download managers" in body
    assert "http://seerr:5055" in body  # current URL pre-filled


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
