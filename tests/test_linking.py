import httpx
import pytest

from vaultrequestrr.linking import AccountLinker, LinkStatus
from vaultrequestrr.seerr import SeerrClient
from vaultrequestrr.store import LinkStore

USERS_PAYLOAD = {
    "results": [
        {"id": 1, "displayName": "Alice", "username": "alice", "plexUsername": "alice_plex", "email": "alice@example.com"},
    ]
}


@pytest.fixture
async def store(tmp_path):
    s = LinkStore(str(tmp_path / "links.sqlite3"))
    await s.connect()
    try:
        yield s
    finally:
        await s.close()


def seerr_with(handler) -> SeerrClient:
    return SeerrClient("http://seerr:5055", "key", transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_link_success_saves_and_writes_back(store):
    writeback = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/user":
            return httpx.Response(200, json=USERS_PAYLOAD)
        if path.endswith("/settings/notifications"):
            if request.method == "GET":
                return httpx.Response(200, json={})
            writeback["body"] = request.content
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"message": "nope"})

    seerr = seerr_with(handler)
    linker = AccountLinker(seerr, store)
    try:
        result = await linker.link("discord-1", "alice_plex")
    finally:
        await seerr.aclose()

    assert result.status is LinkStatus.LINKED
    assert result.user.id == 1
    assert await linker.is_linked("discord-1")
    link = await store.get("discord-1")
    assert link.seerr_user_id == 1
    assert writeback, "expected a write-back POST to Seerr notification settings"


@pytest.mark.asyncio
async def test_link_not_found(store):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=USERS_PAYLOAD)

    seerr = seerr_with(handler)
    linker = AccountLinker(seerr, store)
    try:
        result = await linker.link("discord-2", "does-not-exist")
    finally:
        await seerr.aclose()

    assert result.status is LinkStatus.NOT_FOUND
    assert not await linker.is_linked("discord-2")


@pytest.mark.asyncio
async def test_link_survives_writeback_failure(store):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/user":
            return httpx.Response(200, json=USERS_PAYLOAD)
        # notification settings calls fail
        return httpx.Response(500, json={"message": "boom"})

    seerr = seerr_with(handler)
    linker = AccountLinker(seerr, store)
    try:
        result = await linker.link("discord-3", "alice@example.com")
    finally:
        await seerr.aclose()

    # Local link must still succeed even though write-back failed.
    assert result.status is LinkStatus.LINKED
    assert await linker.is_linked("discord-3")
