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
    STATUS_PARTIALLY_AVAILABLE,
    STATUS_PROCESSING,
    IssueInfo,
    PendingRequest,
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

    async def send(self, embed=None, embeds=None, view=None):
        items = embeds if embeds is not None else ([embed] if embed else [])
        title = next((e.title for e in items if e.title), None)
        self._sink.append((self.id, title))
        self._embeds.extend(items)


class FakeSeerr:
    def __init__(self, info=None, exc=None, issues=None, listed=None):
        self.info = info
        self.exc = exc
        self.issues = issues or []
        self.listed = listed or []  # PendingRequest rows for list_requests
        self.status_updates = []

    async def get_request(self, request_id):
        if self.exc:
            raise self.exc
        return self.info

    async def list_requests(self, *, filter="all", sort="added", take=100, skip=0):
        return self.listed[skip : skip + take]

    async def get_title(self, media_type, tmdb_id):
        return f"Title {tmdb_id}"

    async def list_issues(self, *, take=100):
        return self.issues

    async def update_issue_status(self, issue_id, *, resolved):
        self.status_updates.append((issue_id, resolved))

    async def get_poster_url(self, media_type, tmdb_id):
        return f"https://image.tmdb.org/t/p/w500/{tmdb_id}.jpg"

    async def get_quota(self, user_id):
        q = QuotaStatus(limit=5, used=2, remaining=3, restricted=False, days=7)
        return UserQuota(movie=q, tv=q)


class FakeArr:
    """media_detail stub for the re-grab import watcher."""

    def __init__(self, *, queue=(), has_file=False):
        self.queue = list(queue)
        self.has_file = has_file
        self.calls = []

    async def media_detail(self, media_type, tmdb_id, *, season=None, episode=None):
        self.calls.append((media_type, tmdb_id, season, episode))
        return {"queue": self.queue, "has_file": self.has_file}


class FakeBot:
    def __init__(
        self,
        store,
        seerr,
        *,
        notify_available=True,
        notify_declined=True,
        notify_issue_resolved=True,
        track_external=True,
    ):
        self.store = store
        self.seerr = seerr
        self.config = SimpleNamespace(poll_interval_seconds=60)
        self.runtime = SimpleNamespace(
            notify_on_available=notify_available,
            notify_on_declined=notify_declined,
            notify_on_issue_resolved=notify_issue_resolved,
            track_external_requests=track_external,
        )
        self.arr = FakeArr()
        self.sent = []
        self.embeds = []

    async def fetch_user(self, user_id):
        return FakeUser(user_id, self.sent, self.embeds)

    async def effective_webhook_secret(self):
        return getattr(self, "webhook_secret", "")

    async def issue_notify_ids(self):
        return {1}

    async def issues_channel_id(self):
        return None


async def _track(store, request_id=10, media_type="movie", title="The Matrix", seasons=None):
    await store.add_tracked_request(request_id, "42", media_type, 603, title, seasons)


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
async def test_no_dm_when_only_other_season_available(store):
    """A pre-existing season making the show PARTIALLY_AVAILABLE must not falsely
    mark a different requested season as landed (the S1-requested / S4-on-server bug)."""
    await _track(store, media_type="tv", seasons="1")
    info = RequestInfo(
        id=10,
        request_status=REQUEST_PENDING,
        media_status=STATUS_PARTIALLY_AVAILABLE,  # show rollup: S4 is already present
        media_type="tv",
        tmdb_id=603,
        season_status={1: STATUS_PROCESSING, 4: STATUS_AVAILABLE},
    )
    bot = FakeBot(store, FakeSeerr(info))
    svc = NotificationService(bot)

    await svc._poll()

    assert bot.sent == []  # requested S1 hasn't landed — no DM
    pending = await store.pending_tracked()
    assert len(pending) == 1  # still in flight


@pytest.mark.asyncio
async def test_notifies_when_requested_season_lands(store):
    await _track(store, media_type="tv", seasons="1")
    info = RequestInfo(
        id=10,
        request_status=REQUEST_PENDING,
        media_status=STATUS_PARTIALLY_AVAILABLE,
        media_type="tv",
        tmdb_id=603,
        season_status={1: STATUS_AVAILABLE, 4: STATUS_AVAILABLE},
    )
    bot = FakeBot(store, FakeSeerr(info))
    svc = NotificationService(bot)

    await svc._poll()

    assert bot.sent == [(42, "✅ Now available")]
    assert await store.pending_tracked() == []  # finalised


@pytest.mark.asyncio
async def test_all_seasons_request_falls_back_to_show_rollup(store):
    """An "all seasons" request has no specific target, so partial availability
    (content has started landing) still triggers the DM."""
    await _track(store, media_type="tv", seasons="all")
    info = RequestInfo(
        id=10,
        request_status=REQUEST_PENDING,
        media_status=STATUS_PARTIALLY_AVAILABLE,
        media_type="tv",
        tmdb_id=603,
        season_status={1: STATUS_AVAILABLE, 2: STATUS_PROCESSING},
    )
    bot = FakeBot(store, FakeSeerr(info))
    svc = NotificationService(bot)

    await svc._poll()

    assert bot.sent == [(42, "✅ Now available")]
    assert await store.pending_tracked() == []


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


# -- re-grab import watching -------------------------------------------------


def _ago(seconds):
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


async def _regrab_issue(store, *, at, release="New.Release", by="1"):
    await store.add_tracked_issue(
        5, "42", "movie", 603, "The Matrix", ISSUE_VIDEO, "broken", ISSUE_OPEN
    )
    await store.mark_issue(
        5, regrab_state="grabbed", regrab_release=release, regrab_by=by, regrab_at=at
    )


@pytest.mark.asyncio
async def test_regrab_resolves_only_after_import(store):
    await _regrab_issue(store, at=_ago(300))
    seerr = FakeSeerr(issues=[_issue(status=ISSUE_OPEN)])
    bot = FakeBot(store, seerr)
    bot.arr = FakeArr(queue=[], has_file=True)  # download finished & imported
    svc = NotificationService(bot)

    await svc._poll()

    assert seerr.status_updates == [(5, True)]
    tracked = await store.get_tracked_issue(5)
    assert tracked.regrab_state == "imported"
    assert tracked.status == ISSUE_RESOLVED and tracked.notified_resolved
    assert (42, "🛠️ Issue resolved") in bot.sent  # reporter DM'd on import


@pytest.mark.asyncio
async def test_regrab_waits_while_still_downloading(store):
    await _regrab_issue(store, at=_ago(300))
    seerr = FakeSeerr(issues=[_issue(status=ISSUE_OPEN)])
    bot = FakeBot(store, seerr)
    bot.arr = FakeArr(queue=[{"title": "x", "progress": 40}], has_file=False)
    svc = NotificationService(bot)

    await svc._poll()

    assert seerr.status_updates == []
    tracked = await store.get_tracked_issue(5)
    assert tracked.regrab_state == "grabbed" and tracked.status == ISSUE_OPEN


@pytest.mark.asyncio
async def test_regrab_grace_period_before_first_check(store):
    """Right after the grab the queue may be empty — don't judge yet."""
    await _regrab_issue(store, at=_ago(10))
    bot = FakeBot(store, FakeSeerr(issues=[_issue(status=ISSUE_OPEN)]))
    bot.arr = FakeArr(queue=[], has_file=False)
    svc = NotificationService(bot)

    await svc._poll()

    assert bot.arr.calls == []  # not even inspected yet
    assert (await store.get_tracked_issue(5)).regrab_state == "grabbed"


@pytest.mark.asyncio
async def test_regrab_failure_reopens_and_renotifies(store):
    """Queue empty + no file long after the grab: the download died. Flag it."""
    await _regrab_issue(store, at=_ago(30 * 60))
    seerr = FakeSeerr(issues=[_issue(status=ISSUE_OPEN)])
    bot = FakeBot(store, seerr)
    bot.arr = FakeArr(queue=[], has_file=False)
    svc = NotificationService(bot)

    await svc._poll()

    assert seerr.status_updates == []             # issue stays open
    tracked = await store.get_tracked_issue(5)
    assert tracked.regrab_state == "failed" and tracked.status == ISSUE_OPEN
    assert (1, "⚠️ Re-grab failed") in bot.sent   # admins get actionable cards back


@pytest.mark.asyncio
async def test_poll_cadence_adapts_to_webhook(store):
    bot = FakeBot(store, FakeSeerr())
    bot.config = SimpleNamespace(poll_interval_seconds=600)
    svc = NotificationService(bot)

    # No webhook configured -> tight cadence so polling stays near-real-time.
    bot.webhook_secret = ""
    await svc._poll()
    assert round(svc._loop.seconds) == 120

    # Webhook configured -> relax to the configured backstop.
    bot.webhook_secret = "abc"
    await svc._poll()
    assert round(svc._loop.seconds) == 600


@pytest.mark.asyncio
async def test_check_request_triggers_dm(store):
    """The webhook entry point re-checks one request and notifies, like the poller."""
    await _track(store)
    info = RequestInfo(id=10, request_status=REQUEST_PENDING, media_status=STATUS_AVAILABLE, media_type="movie", tmdb_id=603)
    bot = FakeBot(store, FakeSeerr(info))
    svc = NotificationService(bot)

    await svc.check_request(10)

    assert bot.sent == [(42, "✅ Now available")]
    assert await store.pending_tracked() == []  # finalised


@pytest.mark.asyncio
async def test_check_request_unknown_is_noop(store):
    bot = FakeBot(store, FakeSeerr())
    svc = NotificationService(bot)

    await svc.check_request(999)  # not tracked

    assert bot.sent == []


@pytest.mark.asyncio
async def test_check_issue_triggers_dm(store):
    await store.add_tracked_issue(5, "42", "movie", 603, "The Matrix", ISSUE_VIDEO, "no subs", ISSUE_OPEN)
    bot = FakeBot(store, FakeSeerr(issues=[_issue(status=ISSUE_RESOLVED)]))
    svc = NotificationService(bot)

    await svc.check_issue(5)

    assert bot.sent == [(42, "🛠️ Issue resolved")]
    one = await store.get_tracked_issue(5)
    assert one.notified_resolved


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


# -- external (web-UI) request adoption ------------------------------------


def _listed_request(
    req_id,
    *,
    created_at,
    media_status=STATUS_PROCESSING,
    request_status=REQUEST_PENDING,
    requested_by=7,
):
    return PendingRequest(
        id=req_id,
        media_type="movie",
        tmdb_id=600 + req_id,
        requested_by_id=requested_by,
        requested_by_name="Alice",
        seasons=[],
        created_at=created_at,
        request_status=request_status,
        media_status=media_status,
    )


@pytest.mark.asyncio
async def test_webhook_adopts_external_request_and_dms(store):
    await store.save("42", 7, "alice", "alice@example.com")
    info = RequestInfo(
        id=555, request_status=2, media_status=STATUS_AVAILABLE,
        media_type="movie", tmdb_id=603, requested_by_id=7,
    )
    bot = FakeBot(store, FakeSeerr(info=info))
    svc = NotificationService(bot)

    await svc.check_request(555)  # webhook path: not tracked yet -> adopt

    tracked = await store.get_tracked(555)
    assert tracked is not None
    assert tracked.source == "seerr"
    assert tracked.discord_id == "42"
    assert tracked.title == "Title 603"
    assert tracked.notified_available
    assert [uid for uid, _ in bot.sent] == [42]


@pytest.mark.asyncio
async def test_webhook_adoption_skips_unlinked_requester(store):
    info = RequestInfo(
        id=556, request_status=2, media_status=STATUS_AVAILABLE,
        media_type="movie", tmdb_id=603, requested_by_id=99,  # never linked
    )
    bot = FakeBot(store, FakeSeerr(info=info))
    svc = NotificationService(bot)

    await svc.check_request(556)

    assert await store.get_tracked(556) is None
    assert bot.sent == []


@pytest.mark.asyncio
async def test_adoption_respects_toggle(store):
    await store.save("42", 7, "alice", "alice@example.com")
    info = RequestInfo(
        id=557, request_status=2, media_status=STATUS_AVAILABLE,
        media_type="movie", tmdb_id=603, requested_by_id=7,
    )
    bot = FakeBot(store, FakeSeerr(info=info), track_external=False)
    svc = NotificationService(bot)

    await svc.check_request(557)

    assert await store.get_tracked(557) is None
    assert bot.sent == []


@pytest.mark.asyncio
async def test_adopted_tv_request_records_seasons(store):
    await store.save("42", 7, "alice", "alice@example.com")
    info = RequestInfo(
        id=558, request_status=REQUEST_PENDING, media_status=STATUS_PROCESSING,
        media_type="tv", tmdb_id=1399, requested_by_id=7, requested_seasons=[2, 1],
    )
    bot = FakeBot(store, FakeSeerr(info=info))
    svc = NotificationService(bot)

    await svc.check_request(558)

    tracked = await store.get_tracked(558)
    assert tracked is not None and tracked.seasons == "1,2"
    assert bot.sent == []  # still processing — DM comes when it lands


@pytest.mark.asyncio
async def test_first_sweep_backfills_without_dms(store):
    await store.save("42", 7, "alice", "alice@example.com")
    listed = [
        _listed_request(1, created_at="2026-08-02T00:00:00.000Z", media_status=STATUS_AVAILABLE),
        _listed_request(2, created_at="2026-08-01T00:00:00.000Z", request_status=REQUEST_DECLINED),
        _listed_request(3, created_at="2026-07-31T00:00:00.000Z"),  # still processing
        _listed_request(4, created_at="2026-07-30T00:00:00.000Z", requested_by=99),  # unlinked
    ]
    bot = FakeBot(store, FakeSeerr(listed=listed))
    svc = NotificationService(bot)

    await svc._sync_external_requests()

    # Terminal-state requests adopted pre-notified: history without a DM blast.
    assert (await store.get_tracked(1)).notified_available
    assert (await store.get_tracked(2)).notified_declined
    in_flight = await store.get_tracked(3)
    assert not in_flight.notified_available and not in_flight.notified_declined
    assert await store.get_tracked(4) is None  # unlinked requester skipped
    from vaultrequestrr.notifications import EXTERNAL_CURSOR_KEY

    assert await store.get_setting(EXTERNAL_CURSOR_KEY) == "2026-08-02T00:00:00.000Z"
    assert bot.sent == []


@pytest.mark.asyncio
async def test_sweep_adopts_only_newer_than_cursor(store):
    from vaultrequestrr.notifications import EXTERNAL_CURSOR_KEY

    await store.save("42", 7, "alice", "alice@example.com")
    await store.set_setting(EXTERNAL_CURSOR_KEY, "2026-08-01T00:00:00.000Z")
    listed = [
        _listed_request(5, created_at="2026-08-02T00:00:00.000Z", media_status=STATUS_AVAILABLE),
        _listed_request(6, created_at="2026-07-30T00:00:00.000Z", media_status=STATUS_AVAILABLE),
    ]
    bot = FakeBot(store, FakeSeerr(listed=listed))
    svc = NotificationService(bot)

    await svc._sync_external_requests()

    adopted = await store.get_tracked(5)
    # Adopted live (not backfill): stays pending so the next check DMs it.
    assert adopted is not None and not adopted.notified_available
    assert await store.get_tracked(6) is None  # older than the cursor
    assert await store.get_setting(EXTERNAL_CURSOR_KEY) == "2026-08-02T00:00:00.000Z"
