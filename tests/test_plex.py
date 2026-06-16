import json

import httpx
import pytest

from vaultrequestrr.plex import PlexAuth, PlexClient, PlexError


def auth_with(handler) -> PlexAuth:
    return PlexAuth(transport=httpx.MockTransport(handler))


def client_with(handler) -> PlexClient:
    return PlexClient("tok", "cid", "machine123", transport=httpx.MockTransport(handler))


# -- PIN OAuth flow --------------------------------------------------------


@pytest.mark.asyncio
async def test_create_pin_builds_auth_url():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v2/pins"
        assert request.url.params.get("strong") == "true"
        assert request.headers["X-Plex-Client-Identifier"] == "cid"
        assert request.headers["X-Plex-Product"] == "VaultRequestrr"
        return httpx.Response(201, json={"id": 42, "code": "ABCD"})

    auth = auth_with(handler)
    try:
        pin_id, code, url = await auth.create_pin("cid")
    finally:
        await auth.aclose()
    assert pin_id == 42 and code == "ABCD"
    assert url.startswith("https://app.plex.tv/auth#?")
    assert "clientID=cid" in url and "code=ABCD" in url


@pytest.mark.asyncio
async def test_check_pin_returns_token_when_authorised():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v2/pins/42"
        return httpx.Response(200, json={"id": 42, "authToken": "owner-token"})

    auth = auth_with(handler)
    try:
        token = await auth.check_pin(42, "cid", "ABCD")
    finally:
        await auth.aclose()
    assert token == "owner-token"


@pytest.mark.asyncio
async def test_check_pin_none_until_authorised():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": 42, "authToken": None})

    auth = auth_with(handler)
    try:
        assert await auth.check_pin(42, "cid") is None
    finally:
        await auth.aclose()


@pytest.mark.asyncio
async def test_list_servers_filters_to_owned_and_picks_remote_https():
    payload = [
        {
            "name": "Home",
            "clientIdentifier": "machine123",
            "provides": "server",
            "owned": True,
            "connections": [
                {"local": True, "protocol": "http", "uri": "http://192.168.1.5:32400"},
                {"local": False, "protocol": "https", "uri": "https://home.plex.direct:32400"},
            ],
        },
        {
            "name": "A Friend's",
            "clientIdentifier": "other",
            "provides": "server",
            "owned": False,
            "connections": [{"local": False, "protocol": "https", "uri": "https://nope"}],
        },
        {
            "name": "A Player",
            "clientIdentifier": "player",
            "provides": "client,player",
            "owned": True,
            "connections": [],
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v2/resources"
        assert request.headers["X-Plex-Token"] == "tok"
        return httpx.Response(200, json=payload)

    auth = auth_with(handler)
    try:
        servers = await auth.list_servers("tok", "cid")
    finally:
        await auth.aclose()
    assert len(servers) == 1
    assert servers[0].name == "Home"
    assert servers[0].machine_id == "machine123"


# -- libraries -------------------------------------------------------------


_SERVER_XML = """<?xml version="1.0" encoding="UTF-8"?>
<MediaContainer>
  <Server name="Home" machineIdentifier="machine123">
    <Section id="11" key="1" type="movie" title="Movies"/>
    <Section id="22" key="2" type="show" title="TV"/>
  </Server>
</MediaContainer>"""


@pytest.mark.asyncio
async def test_list_libraries_parses_plextv_sections():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/servers/machine123"
        assert request.headers["X-Plex-Token"] == "tok"
        return httpx.Response(200, text=_SERVER_XML)

    client = client_with(handler)
    try:
        libs = await client.list_libraries()
    finally:
        await client.aclose()
    # section_id is plex.tv's `id`, not the local `key`.
    assert [(l.section_id, l.title, l.kind) for l in libs] == [
        (11, "Movies", "movie"),
        (22, "TV", "show"),
    ]


# -- share -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_share_posts_v1_body():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/servers/machine123/shared_servers"
        assert request.method == "POST"
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, text="<MediaContainer/>")

    client = client_with(handler)
    try:
        await client.share("friend@example.com", [11, 22])
    finally:
        await client.aclose()
    assert captured["body"]["server_id"] == "machine123"
    assert captured["body"]["shared_server"]["invited_email"] == "friend@example.com"
    assert captured["body"]["shared_server"]["library_section_ids"] == [11, 22]


@pytest.mark.asyncio
async def test_share_empty_sections_shares_all():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text=_SERVER_XML)
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, text="<MediaContainer/>")

    client = client_with(handler)
    try:
        await client.share("friend@example.com", [])
    finally:
        await client.aclose()
    # Empty selection expands to every library's id.
    assert captured["body"]["shared_server"]["library_section_ids"] == [11, 22]


@pytest.mark.asyncio
async def test_share_already_shared_maps_to_friendly_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, text="<error/>")

    client = client_with(handler)
    try:
        with pytest.raises(PlexError, match="already"):
            await client.share("dup@example.com", [99])
    finally:
        await client.aclose()
