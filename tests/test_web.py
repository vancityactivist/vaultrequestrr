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

    async def list_issues(self, *, take=100):
        return []

    async def update_issue_status(self, issue_id, *, resolved):
        self.status_updates.append((issue_id, resolved))


class FakeBot:
    def __init__(self, store):
        self.store = store
        self.seerr = FakeSeerr()
        self.linker = AccountLinker(self.seerr, store)
        self.config = SimpleNamespace(web_password="secret", web_port=5056)
        self.runtime = SimpleNamespace(
            require_linking=True,
            notify_on_available=True,
            notify_on_declined=True,
            notify_on_issue_resolved=True,
            log_level="INFO",
        )

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

    resp = await cli.post(
        "/issues/resolve", data={"issue_id": "5"}, allow_redirects=False
    )
    assert resp.status == 302 and resp.headers["Location"].startswith("/issues")
    assert dash.bot.seerr.status_updates == [(5, True)]
    one = await store.get_tracked_issue(5)
    assert one.status == 2  # ISSUE_RESOLVED


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
