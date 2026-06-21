from types import SimpleNamespace

import pytest

from vaultrequestrr.cogs.requests import RequestCog
from vaultrequestrr.seerr import QuotaStatus, SearchResult, SeerrError, UserQuota


def _quota(remaining):
    q = QuotaStatus(limit=5, used=5 - remaining, remaining=remaining, restricted=remaining <= 0, days=7)
    return UserQuota(movie=q, tv=q)


class FakeSeerr:
    def __init__(self, quota=None, quota_exc=None, created_status=None):
        self._quota = quota
        self._quota_exc = quota_exc
        self._created_status = created_status
        self.created = []
        self.overrides = []

    async def get_quota(self, user_id):
        if self._quota_exc:
            raise self._quota_exc
        return self._quota

    async def create_request(self, media_type, tmdb_id, *, user_id, seasons, **overrides):
        self.created.append((media_type, tmdb_id, user_id, seasons))
        self.overrides.append(overrides)
        out = {"id": 77}
        if self._created_status is not None:
            out["status"] = self._created_status
        return out


class FakeStore:
    def __init__(self):
        self.tracked = []

    async def add_tracked_request(self, **kw):
        self.tracked.append(kw)


class FakeNotifications:
    def __init__(self):
        self.pending = []

    async def notify_pending_approval(self, request_id, **kw):
        self.pending.append((request_id, kw))


class FakeInteraction:
    def __init__(self):
        self.edits = []
        self.followups = []
        self.user = SimpleNamespace(id=42, display_name="Neo")

    async def edit_original_response(self, **kw):
        self.edits.append(kw)

    @property
    def followup(self):
        interaction = self

        class _F:
            async def send(self, *a, **k):
                interaction.followups.append((a, k))

        return _F()


def _cog(seerr, store, notifications=None, anime_routing=None):
    async def _routing(media_type):
        return anime_routing.get(media_type) if anime_routing else None

    bot = SimpleNamespace(
        seerr=seerr, store=store, notifications=notifications, anime_routing=_routing
    )
    return RequestCog(bot)


def _result():
    return SearchResult("movie", 603, "The Matrix", "1999", "x", None, None)


@pytest.mark.asyncio
async def test_submit_blocks_when_out_of_quota():
    seerr = FakeSeerr(quota=_quota(0))
    cog = _cog(seerr, FakeStore())
    inter = FakeInteraction()

    await cog._submit(inter, "movie", _result(), None, user_id=7)

    assert seerr.created == []  # never hit the API
    assert inter.edits and inter.edits[0]["embed"].title == "⚠️ Out of quota"


@pytest.mark.asyncio
async def test_submit_proceeds_when_quota_lookup_fails():
    seerr = FakeSeerr(quota_exc=SeerrError("boom"))
    store = FakeStore()
    cog = _cog(seerr, store)
    inter = FakeInteraction()

    await cog._submit(inter, "movie", _result(), None, user_id=7)

    # A quota hiccup must not block a legitimate request.
    assert seerr.created == [("movie", 603, 7, None)]
    assert store.tracked and store.tracked[0]["request_id"] == 77


@pytest.mark.asyncio
async def test_submit_pending_notifies_admins():
    seerr = FakeSeerr(quota=_quota(3), created_status=1)  # REQUEST_PENDING
    notif = FakeNotifications()
    cog = _cog(seerr, FakeStore(), notif)
    inter = FakeInteraction()

    await cog._submit(inter, "movie", _result(), None, user_id=7)

    assert notif.pending and notif.pending[0][0] == 77
    assert notif.pending[0][1]["requester_label"] == "Neo"


@pytest.mark.asyncio
async def test_submit_auto_approved_does_not_notify():
    seerr = FakeSeerr(quota=_quota(3), created_status=2)  # REQUEST_APPROVED
    notif = FakeNotifications()
    cog = _cog(seerr, FakeStore(), notif)
    inter = FakeInteraction()

    await cog._submit(inter, "movie", _result(), None, user_id=7)

    assert notif.pending == []


@pytest.mark.asyncio
async def test_submit_non_anime_sends_no_routing_overrides():
    seerr = FakeSeerr(quota=_quota(3))
    cog = _cog(seerr, FakeStore(), anime_routing={"tv": {"server_id": 2}})
    inter = FakeInteraction()

    await cog._submit(inter, "movie", _result(), None, user_id=7)

    assert seerr.overrides == [{}]  # anime_routing never consulted for a normal request


@pytest.mark.asyncio
async def test_submit_anime_forwards_routing_overrides():
    seerr = FakeSeerr(quota=_quota(3))
    routing = {"tv": {"server_id": 2, "profile_id": 7}}
    cog = _cog(seerr, FakeStore(), anime_routing=routing)
    inter = FakeInteraction()
    result = SearchResult("tv", 1399, "Naruto", "2002", "x", None, None)

    await cog._submit(inter, "tv", result, "all", user_id=7, anime=True)

    assert seerr.overrides == [{"server_id": 2, "profile_id": 7}]


@pytest.mark.asyncio
async def test_submit_anime_unconfigured_falls_back_to_default_routing():
    seerr = FakeSeerr(quota=_quota(3))
    # Only Sonarr configured; an anime *movie* has no Radarr routing → no overrides.
    cog = _cog(seerr, FakeStore(), anime_routing={"tv": {"server_id": 2}})
    inter = FakeInteraction()

    await cog._submit(inter, "movie", _result(), None, user_id=7, anime=True)

    assert seerr.created == [("movie", 603, 7, None)]
    assert seerr.overrides == [{}]
