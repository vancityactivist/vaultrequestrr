import json

import httpx
import pytest

from vaultrequestrr.seerr import SeerrClient, SeerrError, SeerrUser


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
