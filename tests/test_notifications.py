from types import SimpleNamespace

import pytest

from vaultrequestrr.notifications import NotificationService
from vaultrequestrr.seerr import (
    REQUEST_DECLINED,
    REQUEST_PENDING,
    STATUS_AVAILABLE,
    STATUS_PROCESSING,
    RequestInfo,
    SeerrError,
)
from vaultrequestrr.store import LinkStore


@pytest.fixture
async def store(tmp_path):
    s = LinkStore(str(tmp_path / "links.sqlite3"))
    await s.connect()
    try:
        yield s
    finally:
        await s.close()


class FakeUser:
    def __init__(self, user_id, sink):
        self.id = user_id
        self._sink = sink

    async def send(self, embed=None):
        self._sink.append((self.id, embed.title if embed else None))


class FakeSeerr:
    def __init__(self, info=None, exc=None):
        self.info = info
        self.exc = exc

    async def get_request(self, request_id):
        if self.exc:
            raise self.exc
        return self.info


class FakeBot:
    def __init__(self, store, seerr, *, notify_available=True, notify_declined=True):
        self.store = store
        self.seerr = seerr
        self.config = SimpleNamespace(poll_interval_seconds=60)
        self.runtime = SimpleNamespace(
            notify_on_available=notify_available,
            notify_on_declined=notify_declined,
        )
        self.sent = []

    async def fetch_user(self, user_id):
        return FakeUser(user_id, self.sent)


async def _track(store, request_id=10, media_type="movie", title="The Matrix"):
    await store.add_tracked_request(request_id, "42", media_type, 603, title, None)


@pytest.mark.asyncio
async def test_notifies_on_available(store):
    await _track(store)
    info = RequestInfo(id=10, request_status=REQUEST_PENDING, media_status=STATUS_AVAILABLE, media_type="movie", tmdb_id=603)
    bot = FakeBot(store, FakeSeerr(info))
    svc = NotificationService(bot)

    await svc._poll()

    assert bot.sent == [(42, "✅ Now available")]
    assert await store.pending_tracked() == []  # finalised


@pytest.mark.asyncio
async def test_notifies_on_declined(store):
    await _track(store)
    info = RequestInfo(id=10, request_status=REQUEST_DECLINED, media_status=None, media_type="movie", tmdb_id=603)
    bot = FakeBot(store, FakeSeerr(info))
    svc = NotificationService(bot)

    await svc._poll()

    assert bot.sent == [(42, "❌ Request declined")]
    assert await store.pending_tracked() == []


@pytest.mark.asyncio
async def test_no_dm_while_in_flight(store):
    await _track(store)
    info = RequestInfo(id=10, request_status=REQUEST_PENDING, media_status=STATUS_PROCESSING, media_type="movie", tmdb_id=603)
    bot = FakeBot(store, FakeSeerr(info))
    svc = NotificationService(bot)

    await svc._poll()

    assert bot.sent == []
    pending = await store.pending_tracked()
    assert len(pending) == 1 and pending[0].media_status == STATUS_PROCESSING


@pytest.mark.asyncio
async def test_finalises_without_dm_when_notifications_off(store):
    await _track(store)
    info = RequestInfo(id=10, request_status=REQUEST_PENDING, media_status=STATUS_AVAILABLE, media_type="movie", tmdb_id=603)
    bot = FakeBot(store, FakeSeerr(info), notify_available=False)
    svc = NotificationService(bot)

    await svc._poll()

    assert bot.sent == []  # no DM
    assert await store.pending_tracked() == []  # but still finalised so we stop polling


@pytest.mark.asyncio
async def test_404_stops_tracking(store):
    await _track(store)
    bot = FakeBot(store, FakeSeerr(exc=SeerrError("Seerr returned 404: not found")))
    svc = NotificationService(bot)

    await svc._poll()

    assert bot.sent == []
    # request removed entirely
    async with store._conn.execute("SELECT COUNT(*) AS c FROM tracked_requests") as cur:
        row = await cur.fetchone()
    assert row["c"] == 0
