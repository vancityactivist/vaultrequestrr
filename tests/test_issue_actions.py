from types import SimpleNamespace

import pytest

from vaultrequestrr.arr import ResearchResult
from vaultrequestrr.issue_actions import act_regrab, act_resolve, build_issue_view
from vaultrequestrr.notifications import NotificationService
from vaultrequestrr.seerr import ISSUE_OPEN, ISSUE_RESOLVED, SeerrError
from vaultrequestrr.store import LinkStore


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
async def test_act_regrab_grabs_and_resolves(store):
    await _add_issue(store, 5)
    seerr = FakeSeerr()
    arr = FakeArr(result=ResearchResult(True, "Grabbed “Good”."))
    bot = FakeBot(store, seerr, arr=arr, admins=(1,))
    inter = FakeInteraction(bot, user_id=1)

    await act_regrab(bot, inter, 5)

    # Visible "in progress" state shown on the card before the slow search.
    assert inter.response.edits and "Re-grabbing" in (inter.response.edits[0][0] or "")
    assert inter.response.edits[0][1] is None     # buttons dropped while searching
    assert arr.calls == [("movie", 603, None, None)]
    assert seerr.status_updates == [(5, True)]   # resolved on a real grab
    assert (await store.get_tracked_issue(5)).status == ISSUE_RESOLVED
    assert inter.original_edits and inter.original_edits[-1][1] is None  # card finalised


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
