from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from vaultrequestrr.arr import ResearchResult
from vaultrequestrr.issue_actions import (
    act_regrab,
    act_resolve,
    build_issue_view,
    sync_issue_cards,
)
from vaultrequestrr.notifications import NotificationService
from vaultrequestrr.seerr import ISSUE_OPEN, ISSUE_RESOLVED, SeerrError
from vaultrequestrr.store import LinkStore


def _now_iso(offset_seconds=0):
    return (
        datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)
    ).isoformat()


@pytest.fixture
async def store(tmp_path):
    s = LinkStore(str(tmp_path / "links.sqlite3"))
    await s.connect()
    try:
        yield s
    finally:
        await s.close()


async def _add_issue(store, issue_id=5, *, season=None, episode=None):
    await store.add_tracked_issue(
        issue_id=issue_id, discord_id="42", media_type="movie", tmdb_id=603,
        title="The Matrix", issue_type=1, message="frozen", status=ISSUE_OPEN,
        problem_season=season, problem_episode=episode,
    )


class FakeMessage:
    _next_id = 1000

    def __init__(self, channel_id):
        FakeMessage._next_id += 1
        self.id = FakeMessage._next_id
        self.channel = SimpleNamespace(id=channel_id)
        self.edits = []

    async def edit(self, *, content=None, view="keep"):
        self.edits.append((content, view))


class FakeUser:
    def __init__(self, user_id, sink):
        self.id = user_id
        self._sink = sink

    async def send(self, *, embed=None, embeds=None, view=None):
        items = embeds if embeds is not None else ([embed] if embed else [])
        self._sink.append((self.id, [e.title for e in items], view))
        # DMs land in a per-user DM channel; give it a distinct channel id.
        return FakeMessage(channel_id=90000 + self.id)


class FakeChannel:
    def __init__(self, sink, channel_id=555):
        self.id = channel_id
        self._sink = sink
        self.messages = {}

    async def send(self, *, embeds=None, view=None):
        self._sink.append(("channel", [e.title for e in (embeds or [])], view))
        msg = FakeMessage(channel_id=self.id)
        self.messages[msg.id] = msg
        return msg

    def get_partial_message(self, message_id):
        return self.messages.setdefault(message_id, FakeMessage(self.id))


class FakeSeerr:
    def __init__(self, *, fail_resolve=False):
        self.status_updates = []
        self._fail = fail_resolve

    async def get_poster_url(self, media_type, tmdb_id):
        return None

    async def update_issue_status(self, issue_id, *, resolved):
        if self._fail:
            raise SeerrError("already handled")
        self.status_updates.append((issue_id, resolved))


class FakeArr:
    def __init__(self, *, result=None, exc=None):
        self._result = result
        self._exc = exc
        self.calls = []

    async def research(self, media_type, tmdb_id, *, season=None, episode=None):
        self.calls.append((media_type, tmdb_id, season, episode))
        if self._exc:
            raise self._exc
        return self._result


class FakeBot:
    def __init__(self, store, seerr, *, arr=None, admins=(1,), channel_id=None):
        self.store = store
        self.seerr = seerr
        self.arr = arr
        self._admins = set(admins)
        self._channel_id = channel_id
        self.sent = []
        self.channel_sink = []
        self.channels = {}

    async def admin_ids(self):
        return set(self._admins)

    async def is_admin(self, discord_id):
        return int(discord_id) in self._admins

    async def approvals_channel_id(self):
        return self._channel_id

    async def issue_notify_ids(self):
        return set(self._admins)

    async def is_issue_handler(self, discord_id):
        return int(discord_id) in self._admins

    async def issues_channel_id(self):
        return self._channel_id

    def get_channel(self, cid):
        if cid not in self.channels:
            self.channels[cid] = FakeChannel(self.channel_sink, channel_id=cid)
        return self.channels[cid]

    async def fetch_channel(self, cid):
        return self.get_channel(cid)

    async def fetch_user(self, user_id):
        return FakeUser(user_id, self.sent)

    @property
    def config(self):
        return SimpleNamespace(poll_interval_seconds=60)


class FakeResponse:
    def __init__(self):
        self.messages = []
        self.edits = []
        self.deferred = False

    async def defer(self, *, ephemeral=False, thinking=False):
        self.deferred = True

    async def send_message(self, content=None, *, ephemeral=False):
        self.messages.append((content, ephemeral))

    async def edit_message(self, *, content=None, view="keep"):
        self.edits.append((content, view))


class FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, content=None, *, ephemeral=False):
        self.messages.append((content, ephemeral))


class FakeInteraction:
    def __init__(self, bot, user_id):
        self.client = bot
        self.user = SimpleNamespace(id=user_id, mention=f"<@{user_id}>", display_name="Admin")
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.original_edits = []

    async def edit_original_response(self, *, content=None, view="keep"):
        self.original_edits.append((content, view))


@pytest.mark.asyncio
async def test_act_resolve_marks_resolved(store):
    await _add_issue(store, 5)
    seerr = FakeSeerr()
    bot = FakeBot(store, seerr, admins=(1,))
    inter = FakeInteraction(bot, user_id=1)

    await act_resolve(bot, inter, 5)

    assert seerr.status_updates == [(5, True)]
    assert inter.response.edits and inter.response.edits[0][1] is None  # buttons cleared
    tracked = await store.get_tracked_issue(5)
    assert tracked.status == ISSUE_RESOLVED


@pytest.mark.asyncio
async def test_act_resolve_non_admin_denied(store):
    await _add_issue(store, 5)
    seerr = FakeSeerr()
    bot = FakeBot(store, seerr, admins=(1,))
    inter = FakeInteraction(bot, user_id=999)

    await act_resolve(bot, inter, 5)

    assert seerr.status_updates == []
    assert inter.response.messages and inter.response.messages[0][1] is True


@pytest.mark.asyncio
async def test_act_regrab_grabs_but_waits_for_import(store):
    """A grab must NOT resolve the issue — the poller resolves it on import."""
    await _add_issue(store, 5)
    seerr = FakeSeerr()
    arr = FakeArr(result=ResearchResult(True, "Grabbed “Good”.", release="Good"))
    bot = FakeBot(store, seerr, arr=arr, admins=(1,))
    inter = FakeInteraction(bot, user_id=1)

    await act_regrab(bot, inter, 5)

    # Visible "in progress" state shown on the card before the slow search.
    assert inter.response.edits and "Re-grabbing" in (inter.response.edits[0][0] or "")
    assert inter.response.edits[0][1] is None     # buttons dropped while searching
    assert arr.calls == [("movie", 603, None, None)]
    assert seerr.status_updates == []             # NOT resolved yet
    tracked = await store.get_tracked_issue(5)
    assert tracked.status == ISSUE_OPEN           # stays open until import
    assert tracked.regrab_state == "grabbed"
    assert tracked.regrab_release == "Good"
    assert tracked.regrab_by == "1" and tracked.regrab_at
    # Card says the issue stays open until the replacement lands.
    assert inter.original_edits and "stays" in (inter.original_edits[-1][0] or "")
    assert inter.original_edits[-1][1] is None


@pytest.mark.asyncio
async def test_act_regrab_blocked_when_already_resolved(store):
    """A stale card copy must not re-fire the delete-and-grab after resolution."""
    await _add_issue(store, 5)
    await store.mark_issue(5, status=ISSUE_RESOLVED)
    arr = FakeArr(result=ResearchResult(True, "x", release="x"))
    bot = FakeBot(store, FakeSeerr(), arr=arr, admins=(1,))
    inter = FakeInteraction(bot, user_id=1)

    await act_regrab(bot, inter, 5)

    assert arr.calls == []                        # nothing fired
    assert inter.response.edits and "already been resolved" in inter.response.edits[0][0]
    assert inter.response.edits[0][1] is None     # stale buttons stripped


@pytest.mark.asyncio
async def test_act_regrab_blocked_while_in_flight(store):
    """A second click (e.g. another admin's card) can't queue a duplicate download."""
    await _add_issue(store, 5)
    await store.mark_issue(5, regrab_state="grabbed", regrab_at=_now_iso())
    arr = FakeArr(result=ResearchResult(True, "x", release="x"))
    bot = FakeBot(store, FakeSeerr(), arr=arr, admins=(1, 2))
    inter = FakeInteraction(bot, user_id=2)

    await act_regrab(bot, inter, 5)

    assert arr.calls == []
    assert inter.response.edits and "already in flight" in inter.response.edits[0][0]


@pytest.mark.asyncio
async def test_act_regrab_allows_retry_when_in_flight_grab_is_stale(store):
    """A download stuck for hours shouldn't block a fresh re-grab forever."""
    await _add_issue(store, 5)
    await store.mark_issue(
        5, regrab_state="grabbed", regrab_at=_now_iso(-7 * 60 * 60)  # 7h ago
    )
    arr = FakeArr(result=ResearchResult(True, "Grabbed “New”.", release="New"))
    bot = FakeBot(store, FakeSeerr(), arr=arr, admins=(1,))
    inter = FakeInteraction(bot, user_id=1)

    await act_regrab(bot, inter, 5)

    assert arr.calls == [("movie", 603, None, None)]  # allowed through


@pytest.mark.asyncio
async def test_act_resolve_stale_card_after_resolution(store):
    await _add_issue(store, 5)
    seerr = FakeSeerr()
    bot = FakeBot(store, seerr, admins=(1, 2))
    await act_resolve(bot, FakeInteraction(bot, user_id=1), 5)

    # Second admin clicks their own (stale) copy of the card.
    inter2 = FakeInteraction(bot, user_id=2)
    await act_resolve(bot, inter2, 5)

    assert seerr.status_updates == [(5, True)]    # resolved exactly once
    assert inter2.response.edits and "already been resolved" in inter2.response.edits[0][0]


@pytest.mark.asyncio
async def test_notify_issue_filed_records_card_copies(store):
    """Each broadcast copy is remembered so outcomes can be synced onto it."""
    bot = FakeBot(store, FakeSeerr(), admins=(1, 2), channel_id=555)
    svc = NotificationService(bot)

    await svc.notify_issue_filed(
        5, media_type="movie", tmdb_id=603, title="The Matrix", issue_type=1,
        reporter_label="Neo", season=None, episode=None, message="frozen",
    )

    records = await store.list_issue_messages(5)
    assert len(records) == 3                      # 2 admin DMs + channel post
    assert {r.channel_id for r in records} == {90001, 90002, 555}


@pytest.mark.asyncio
async def test_sync_issue_cards_edits_every_copy_except_clicked(store):
    bot = FakeBot(store, FakeSeerr(), admins=(1,), channel_id=555)
    await store.add_issue_message(5, 555, 111)
    await store.add_issue_message(5, 556, 222)

    await sync_issue_cards(bot, 5, "✅ Done", skip_message_id=111)

    edited = bot.get_channel(556).messages[222].edits
    assert edited == [("✅ Done", None)]           # content set, buttons removed
    assert 111 not in bot.get_channel(555).messages  # clicked copy untouched


@pytest.mark.asyncio
async def test_act_regrab_no_release_keeps_issue_open(store):
    await _add_issue(store, 5)
    seerr = FakeSeerr()
    arr = FakeArr(result=ResearchResult(False, "No releases found."))
    bot = FakeBot(store, seerr, arr=arr, admins=(1,))
    inter = FakeInteraction(bot, user_id=1)

    await act_regrab(bot, inter, 5)

    assert seerr.status_updates == []            # not resolved without a grab
    assert (await store.get_tracked_issue(5)).status == ISSUE_OPEN
    # Outcome surfaced on the card, with the buttons restored for a retry.
    assert inter.original_edits and "No releases found." in (inter.original_edits[-1][0] or "")
    assert inter.original_edits[-1][1] is not None


@pytest.mark.asyncio
async def test_act_regrab_non_admin_denied(store):
    await _add_issue(store, 5)
    arr = FakeArr(result=ResearchResult(True, "x"))
    bot = FakeBot(store, FakeSeerr(), arr=arr, admins=(1,))
    inter = FakeInteraction(bot, user_id=999)

    await act_regrab(bot, inter, 5)

    assert arr.calls == []
    assert inter.response.messages and inter.response.messages[0][1] is True


@pytest.mark.asyncio
async def test_notify_issue_filed_dms_admins_and_posts_channel(store):
    bot = FakeBot(store, FakeSeerr(), admins=(1, 2), channel_id=555)
    svc = NotificationService(bot)

    await svc.notify_issue_filed(
        5, media_type="movie", tmdb_id=603, title="The Matrix", issue_type=1,
        reporter_label="Neo", season=None, episode=None, message="frozen at 10m",
    )

    dmed = {uid for uid, _t, _v in bot.sent}
    assert dmed == {1, 2}
    assert len(bot.channel_sink) == 1
    assert all(v is not None for _u, _t, v in bot.sent)  # actionable buttons attached


def test_build_issue_view_has_two_buttons():
    view = build_issue_view(5)
    labels = {
        getattr(c, "label", None) or getattr(getattr(c, "item", None), "label", None)
        for c in view.children
    }
    assert "Re-grab" in labels and "Resolve" in labels
