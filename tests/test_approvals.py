from types import SimpleNamespace

import pytest

from vaultrequestrr.approvals import act_on, build_approval_view
from vaultrequestrr.notifications import NotificationService
from vaultrequestrr.seerr import REQUEST_APPROVED, REQUEST_DECLINED, SeerrError
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

    async def send(self, *, embed=None, embeds=None, view=None):
        items = embeds if embeds is not None else ([embed] if embed else [])
        self._sink.append((self.id, [e.title for e in items], view))


class FakeChannel:
    def __init__(self, sink):
        self._sink = sink

    async def send(self, *, embeds=None, view=None):
        self._sink.append(("channel", [e.title for e in (embeds or [])], view))


class FakeSeerr:
    def __init__(self, *, fail=False):
        self.approved = []
        self.declined = []
        self._fail = fail

    async def get_poster_url(self, media_type, tmdb_id):
        return None

    async def approve_request(self, request_id):
        if self._fail:
            raise SeerrError("already handled")
        self.approved.append(request_id)

    async def decline_request(self, request_id):
        self.declined.append(request_id)


class FakeBot:
    def __init__(self, store, seerr, *, admins=(1,), channel_id=None):
        self.store = store
        self.seerr = seerr
        self._admins = set(admins)
        self._channel_id = channel_id
        self.sent = []
        self.channel_sink = []

    async def admin_ids(self):
        return set(self._admins)

    async def is_admin(self, discord_id):
        return int(discord_id) in self._admins

    async def approvals_channel_id(self):
        return self._channel_id

    def get_channel(self, cid):
        return FakeChannel(self.channel_sink)

    async def fetch_user(self, user_id):
        return FakeUser(user_id, self.sent)

    @property
    def config(self):
        return SimpleNamespace(poll_interval_seconds=60)


class FakeResponse:
    def __init__(self):
        self.messages = []
        self.edits = []

    async def send_message(self, content=None, *, ephemeral=False):
        self.messages.append((content, ephemeral))

    async def edit_message(self, *, content=None, view="keep"):
        self.edits.append((content, view))


class FakeInteraction:
    def __init__(self, bot, user_id):
        self.client = bot
        self.user = SimpleNamespace(id=user_id, mention=f"<@{user_id}>", display_name="Admin")
        self.response = FakeResponse()


@pytest.mark.asyncio
async def test_act_on_approve_calls_seerr_and_dms_requester(store):
    await store.add_tracked_request(11, "42", "tv", 1396, "Breaking Bad", "all")
    seerr = FakeSeerr()
    bot = FakeBot(store, seerr, admins=(1,))
    inter = FakeInteraction(bot, user_id=1)

    await act_on(bot, inter, 11, approve=True)

    assert seerr.approved == [11]
    assert inter.response.edits and inter.response.edits[0][1] is None  # buttons cleared
    tracked = await store.get_tracked(11)
    assert tracked.request_status == REQUEST_APPROVED
    # requester (discord 42) was DM'd the outcome
    assert any(uid == 42 for uid, _titles, _v in bot.sent)


@pytest.mark.asyncio
async def test_act_on_decline_suppresses_poller_dm(store):
    await store.add_tracked_request(11, "42", "movie", 603, "The Matrix", None)
    seerr = FakeSeerr()
    bot = FakeBot(store, seerr, admins=(1,))
    inter = FakeInteraction(bot, user_id=1)

    await act_on(bot, inter, 11, approve=False)

    assert seerr.declined == [11]
    tracked = await store.get_tracked(11)
    assert tracked.request_status == REQUEST_DECLINED and tracked.notified_declined


@pytest.mark.asyncio
async def test_act_on_non_admin_denied(store):
    seerr = FakeSeerr()
    bot = FakeBot(store, seerr, admins=(1,))
    inter = FakeInteraction(bot, user_id=999)  # not an admin

    await act_on(bot, inter, 11, approve=True)

    assert seerr.approved == []
    assert inter.response.messages and inter.response.messages[0][1] is True  # ephemeral refusal


@pytest.mark.asyncio
async def test_act_on_seerr_error_is_reported(store):
    bot = FakeBot(store, FakeSeerr(fail=True), admins=(1,))
    inter = FakeInteraction(bot, user_id=1)

    await act_on(bot, inter, 11, approve=True)

    # surfaced as an ephemeral message, no message edit
    assert inter.response.messages and inter.response.messages[0][1] is True
    assert inter.response.edits == []


@pytest.mark.asyncio
async def test_notify_pending_approval_dms_admins_and_posts_channel(store):
    bot = FakeBot(store, FakeSeerr(), admins=(1, 2), channel_id=555)
    svc = NotificationService(bot)

    await svc.notify_pending_approval(
        11, media_type="movie", tmdb_id=603, title="The Matrix",
        requester_label="Neo", seasons=None,
    )

    dmed = {uid for uid, _t, _v in bot.sent}
    assert dmed == {1, 2}  # each admin DM'd
    assert len(bot.channel_sink) == 1  # and posted to the channel
    # the DM carried an actionable view
    assert all(v is not None for _u, _t, v in bot.sent)


def test_build_approval_view_has_two_buttons():
    view = build_approval_view(11)
    labels = {getattr(c, "label", None) or getattr(getattr(c, "item", None), "label", None) for c in view.children}
    assert "Approve" in labels and "Decline" in labels
