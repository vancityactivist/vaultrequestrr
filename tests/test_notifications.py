from types import SimpleNamespace

import pytest

from vaultrequestrr.notifications import NotificationService
from vaultrequestrr.seerr import (
    ISSUE_OPEN,
    ISSUE_RESOLVED,
    ISSUE_VIDEO,
    REQUEST_DECLINED,
    REQUEST_PENDING,
    STATUS_AVAILABLE,
    STATUS_PROCESSING,
    IssueInfo,
    QuotaStatus,
    RequestInfo,
    SeerrError,
    UserQuota,
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
    def __init__(self, user_id, sink, embeds):
        self.id = user_id
        self._sink = sink
        self._embeds = embeds

    async def send(self, embed=None, embeds=None):
        items = embeds if embeds is not None else ([embed] if embed else [])
        title = next((e.title for e in items if e.title), None)
        self._sink.append((self.id, title))
        self._embeds.extend(items)


class FakeSeerr:
    def __init__(self, info=None, exc=None, issues=None):
        self.info = info
        self.exc = exc
        self.issues = issues or []

    async def get_request(self, request_id):
        if self.exc:
            raise self.exc
        return self.info

    async def list_issues(self, *, take=100):
        return self.issues

    async def get_poster_url(self, media_type, tmdb_id):
        return f"https://image.tmdb.org/t/p/w500/{tmdb_id}.jpg"

    async def get_quota(self, user_id):
        q = QuotaStatus(limit=5, used=2, remaining=3, restricted=False, days=7)
        return UserQuota(movie=q, tv=q)


class FakeBot:
    def __init__(
        self,
        store,
        seerr,
        *,
        notify_available=True,
        notify_declined=True,
        notify_issue_resolved=True,
    ):
        self.store = store
        self.seerr = seerr
        self.config = SimpleNamespace(poll_interval_seconds=60)
        self.runtime = SimpleNamespace(
            notify_on_available=notify_available,
            notify_on_declined=notify_declined,
            notify_on_issue_resolved=notify_issue_resolved,
        )
        self.sent = []
        self.embeds = []

    async def fetch_user(self, user_id):
        return FakeUser(user_id, self.sent, self.embeds)


async def _track(store, request_id=10, media_type="movie", title="The Matrix"):
    await store.add_tracked_request(request_id, "42", media_type, 603, title, None)


@pytest.mark.asyncio
async def test_notifies_on_available(store):
    await _track(store)
    await store.save("42", 7, "neo", "neo@example.com")
    info = RequestInfo(id=10, request_status=REQUEST_PENDING, media_status=STATUS_AVAILABLE, media_type="movie", tmdb_id=603)
    bot = FakeBot(store, FakeSeerr(info))
    svc = NotificationService(bot)

    await svc._poll()

    assert bot.sent == [(42, "✅ Now available")]
    assert await store.pending_tracked() == []  # finalised

    # Richer DM: a full-width cover-art banner above the details, plus a
    # remaining-quota reminder.
    banner, body = bot.embeds
    assert banner.image.url == "https://image.tmdb.org/t/p/w500/603.jpg"
    quota_fields = [f for f in body.fields if "quota" in f.name.lower()]
    assert quota_fields and "3" in quota_fields[0].value


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


def _issue(issue_id=5, status=ISSUE_RESOLVED):
    return IssueInfo(
        id=issue_id,
        issue_type=ISSUE_VIDEO,
        status=status,
        media_type="movie",
        tmdb_id=603,
        created_by_name="Admin",
        created_at="2026-06-15T00:00:00Z",
    )


@pytest.mark.asyncio
async def test_notifies_when_issue_resolved(store):
    await store.add_tracked_issue(5, "42", "movie", 603, "The Matrix", ISSUE_VIDEO, "no subs", ISSUE_OPEN)
    bot = FakeBot(store, FakeSeerr(issues=[_issue(status=ISSUE_RESOLVED)]))
    svc = NotificationService(bot)

    await svc._poll()

    assert bot.sent == [(42, "🛠️ Issue resolved")]
    assert await store.pending_issues() == []  # finalised, no repeat DMs
    one = await store.get_tracked_issue(5)
    assert one.status == ISSUE_RESOLVED and one.notified_resolved


@pytest.mark.asyncio
async def test_open_issue_stays_pending(store):
    await store.add_tracked_issue(5, "42", "movie", 603, "X", ISSUE_VIDEO, "m", ISSUE_OPEN)
    bot = FakeBot(store, FakeSeerr(issues=[_issue(status=ISSUE_OPEN)]))
    svc = NotificationService(bot)

    await svc._poll()

    assert bot.sent == []
    assert len(await store.pending_issues()) == 1


@pytest.mark.asyncio
async def test_resolved_issue_finalised_without_dm_when_off(store):
    await store.add_tracked_issue(5, "42", "movie", 603, "X", ISSUE_VIDEO, "m", ISSUE_OPEN)
    bot = FakeBot(store, FakeSeerr(issues=[_issue()]), notify_issue_resolved=False)
    svc = NotificationService(bot)

    await svc._poll()

    assert bot.sent == []
    assert await store.pending_issues() == []  # still finalised


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
