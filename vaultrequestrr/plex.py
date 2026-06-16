"""Async client for the bits of the Plex API VaultRequestrr needs.

Two concerns live here:

* ``PlexAuth`` drives the "Login with Plex" PIN OAuth flow (so the admin never
  has to find their own token) and discovers the owner's servers.
* ``PlexClient`` lists a server's shareable libraries and shares the server with
  a friend by email.

Everything talks to ``plex.tv`` (the account API) rather than the local Plex
server, so it works regardless of where the bot runs relative to Plex. Library
listing and sharing both use the v1 ``servers/{machineId}`` endpoints, whose
section ``id`` values are what the share API expects (these differ from the local
server's section ``key`` values). All calls send a stable
``X-Plex-Client-Identifier`` and identify the app via ``X-Plex-Product``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urlencode
from xml.etree import ElementTree

import httpx

logger = logging.getLogger(__name__)

PLEX_PRODUCT = "VaultRequestrr"
PLEX_TV_V2 = "https://plex.tv/api/v2"
PLEX_TV_V1 = "https://plex.tv/api"
PLEX_AUTH_APP = "https://app.plex.tv/auth"


class PlexError(RuntimeError):
    """Raised when the Plex API returns an error or can't be reached."""


@dataclass(frozen=True)
class PlexLibrary:
    section_id: int  # plex.tv section id (used for sharing), not the local key
    title: str
    kind: str  # "movie", "show", "artist", "photo", …


@dataclass(frozen=True)
class PlexServer:
    name: str
    machine_id: str


def _headers(client_id: str, token: str | None = None, *, accept: str = "application/json") -> dict[str, str]:
    headers = {
        "X-Plex-Product": PLEX_PRODUCT,
        "X-Plex-Client-Identifier": client_id,
        "Accept": accept,
    }
    if token:
        headers["X-Plex-Token"] = token
    return headers


class PlexAuth:
    """Stateless helpers for the PIN login flow and server discovery."""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(timeout=20.0, transport=transport)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def create_pin(self, client_id: str) -> tuple[int, str, str]:
        """Create a login PIN. Returns (pin_id, code, auth_url)."""
        try:
            resp = await self._client.post(
                f"{PLEX_TV_V2}/pins",
                params={"strong": "true"},
                headers=_headers(client_id),
            )
        except httpx.HTTPError as exc:
            raise PlexError(f"Could not reach Plex: {exc}") from exc
        if not resp.is_success:
            raise PlexError(_error(resp))
        data = resp.json()
        pin_id = int(data["id"])
        code = str(data["code"])
        return pin_id, code, _auth_url(client_id, code)

    async def check_pin(self, pin_id: int, client_id: str, code: str | None = None) -> str | None:
        """Return the auth token once the user authorises the PIN, else None."""
        params = {"code": code} if code else None
        try:
            resp = await self._client.get(
                f"{PLEX_TV_V2}/pins/{pin_id}",
                params=params,
                headers=_headers(client_id),
            )
        except httpx.HTTPError as exc:
            raise PlexError(f"Could not reach Plex: {exc}") from exc
        if not resp.is_success:
            raise PlexError(_error(resp))
        return resp.json().get("authToken") or None

    async def list_servers(self, token: str, client_id: str) -> list[PlexServer]:
        """List the owner's Plex Media Servers (owned resources only)."""
        try:
            resp = await self._client.get(
                f"{PLEX_TV_V2}/resources",
                params={"includeHttps": "1"},
                headers=_headers(client_id, token),
            )
        except httpx.HTTPError as exc:
            raise PlexError(f"Could not reach Plex: {exc}") from exc
        if not resp.is_success:
            raise PlexError(_error(resp))

        servers: list[PlexServer] = []
        for res in resp.json() or []:
            provides = str(res.get("provides") or "")
            machine_id = res.get("clientIdentifier")
            if "server" not in provides.split(",") or not res.get("owned") or not machine_id:
                continue
            servers.append(
                PlexServer(name=str(res.get("name") or "Plex Server"), machine_id=str(machine_id))
            )
        return servers


class PlexClient:
    """Lists shareable libraries and shares a server, all via plex.tv."""

    def __init__(
        self,
        token: str,
        client_id: str,
        machine_id: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = token
        self._client_id = client_id
        self.machine_id = machine_id
        self._client = httpx.AsyncClient(timeout=15.0, transport=transport)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def list_libraries(self) -> list[PlexLibrary]:
        """List the server's libraries (with plex.tv section ids) from plex.tv."""
        root = await self._get_server_xml()
        libraries: list[PlexLibrary] = []
        for section in root.iter("Section"):
            raw_id = section.get("id")
            if raw_id is None:
                continue
            try:
                section_id = int(raw_id)
            except ValueError:
                continue
            libraries.append(
                PlexLibrary(
                    section_id=section_id,
                    title=section.get("title") or f"Library {section_id}",
                    kind=section.get("type") or "",
                )
            )
        return libraries

    async def share(
        self,
        invited_email: str,
        library_section_ids: list[int],
        *,
        allow_sync: bool = False,
    ) -> None:
        """Share this server with a friend by email via plex.tv.

        An empty ``library_section_ids`` shares every library.
        """
        if not library_section_ids:
            library_section_ids = [lib.section_id for lib in await self.list_libraries()]

        body = {
            "server_id": self.machine_id,
            "shared_server": {
                "library_section_ids": library_section_ids,
                "invited_email": invited_email,
            },
            "sharing_settings": {
                "allowSync": "1" if allow_sync else "0",
                "allowCameraUpload": "0",
                "allowChannels": "0",
            },
        }
        try:
            resp = await self._client.post(
                f"{PLEX_TV_V1}/servers/{self.machine_id}/shared_servers",
                headers=_headers(self._client_id, self._token),
                json=body,
            )
        except httpx.HTTPError as exc:
            raise PlexError(f"Could not reach Plex: {exc}") from exc

        if resp.is_success:
            return
        if resp.status_code == 422:
            raise PlexError("That account is already shared with — they may already have access.")
        if resp.status_code == 401:
            raise PlexError("Plex rejected the owner token — reconnect Plex in Settings.")
        raise PlexError(_error(resp))

    async def _get_server_xml(self) -> ElementTree.Element:
        try:
            resp = await self._client.get(
                f"{PLEX_TV_V1}/servers/{self.machine_id}",
                headers=_headers(self._client_id, self._token, accept="application/xml"),
            )
        except httpx.HTTPError as exc:
            raise PlexError(f"Could not reach Plex: {exc}") from exc
        if not resp.is_success:
            raise PlexError(_error(resp))
        try:
            return ElementTree.fromstring(resp.text)
        except ElementTree.ParseError as exc:
            raise PlexError(f"Unexpected Plex response: {exc}") from exc


def _auth_url(client_id: str, code: str, forward_url: str | None = None) -> str:
    params = {
        "clientID": client_id,
        "code": code,
        "context[device][product]": PLEX_PRODUCT,
    }
    if forward_url:
        params["forwardUrl"] = forward_url
    return f"{PLEX_AUTH_APP}#?{urlencode(params)}"


def _error(response: httpx.Response) -> str:
    detail = ""
    try:
        payload = response.json()
        if isinstance(payload, dict):
            errors = payload.get("errors")
            if isinstance(errors, list) and errors:
                detail = str(errors[0].get("message") or "")
    except ValueError:
        detail = (response.text or "").strip()[:200]
    return f"Plex API error {response.status_code}" + (f": {detail}" if detail else "")
