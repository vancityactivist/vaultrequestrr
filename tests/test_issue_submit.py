from types import SimpleNamespace

import pytest

from vaultrequestrr.cogs.issues import IssueCog
from vaultrequestrr.seerr import ISSUE_VIDEO, SearchResult, SeerrError


def _result():
    return SearchResult(
        media_type="movie",
        tmdb_id=603,
        title="The Matrix",
        year="1999",
        overview=None,
        poster_url=None,
        status=5,
        media_id=42,
    )


class FakeSeerr:
    """create_issue raises the queued errors first, then succeeds."""

    def __init__(self, errors=(), supports_attribution=True):
        self._errors = list(errors)
        self.supports_issue_attribution = supports_attribution
        self.calls = []

    async def create_issue(self, media_id, issue_type, message, *,
                           user_id=None, problem_season=None, problem_episode=None):
        self.calls.append({"user_id": user_id, "message": message})
        if self._errors:
            raise self._errors.pop(0)
        return {"id": 9, "createdBy": {"id": user_id or 1}}

    def mark_issue_attribution_unsupported(self):
        self.supports_issue_attribution = False


class FakeStore:
    def __init__(self, link=None):
        self._link = link
        self.issues = []

    async def get(self, discord_id):
        return self._link

    async def add_tracked_issue(self, **kw):
        self.issues.append(kw)


class FakeNotifications:
    def __init__(self):
        self.filed = []

    async def notify_issue_filed(self, issue_id, **kw):
        self.filed.append(issue_id)


class FakeInteraction:
    def __init__(self):
        self.followups = []
        self.edits = []
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


def _cog(seerr, link=SimpleNamespace(seerr_user_id=7)):
    store = FakeStore(link=link)
    notifications = FakeNotifications()
    bot = SimpleNamespace(seerr=seerr, store=store, notifications=notifications)
    return IssueCog(bot), store, notifications


@pytest.mark.asyncio
async def test_attribution_stale_user_refiles_unattributed():
    # Seerr 3.4+ returns 404 when the linked account was deleted.
    seerr = FakeSeerr(errors=[SeerrError("Seerr returned 404: Issue user not found", status=404)])
    cog, store, notifications = _cog(seerr)
    interaction = FakeInteraction()

    await cog.submit_issue(interaction, _result(), ISSUE_VIDEO, "Neo", "no subs")

    assert [c["user_id"] for c in seerr.calls] == [7, None]
    assert seerr.calls[1]["message"].startswith("Reported by Neo")
    assert store.issues and store.issues[0]["issue_id"] == 9
    assert notifications.filed == [9]
    assert interaction.edits  # success embed shown
    assert not interaction.followups  # no error surfaced
    # A missing user is per-link — the capability stays on for everyone else.
    assert seerr.supports_issue_attribution is True


@pytest.mark.asyncio
async def test_attribution_permission_error_disables_and_refiles():
    seerr = FakeSeerr(errors=[SeerrError("Seerr returned 403", status=403)])
    cog, store, _ = _cog(seerr)
    interaction = FakeInteraction()

    await cog.submit_issue(interaction, _result(), ISSUE_VIDEO, "Neo", "no subs")

    assert [c["user_id"] for c in seerr.calls] == [7, None]
    assert store.issues  # still filed
    assert seerr.supports_issue_attribution is False


@pytest.mark.asyncio
async def test_attribution_unrelated_error_is_not_retried():
    seerr = FakeSeerr(errors=[SeerrError("Seerr returned 500", status=500)])
    cog, store, _ = _cog(seerr)
    interaction = FakeInteraction()

    await cog.submit_issue(interaction, _result(), ISSUE_VIDEO, "Neo", "no subs")

    assert len(seerr.calls) == 1
    assert not store.issues
    assert interaction.followups  # error reported to the user


@pytest.mark.asyncio
async def test_fallback_failure_reports_second_error():
    seerr = FakeSeerr(errors=[
        SeerrError("Seerr returned 404: Issue user not found", status=404),
        SeerrError("Seerr returned 500", status=500),
    ])
    cog, store, _ = _cog(seerr)
    interaction = FakeInteraction()

    await cog.submit_issue(interaction, _result(), ISSUE_VIDEO, "Neo", "no subs")

    assert len(seerr.calls) == 2
    assert not store.issues
    assert interaction.followups
    assert "500" in interaction.followups[0][0][0]


@pytest.mark.asyncio
async def test_unattributed_error_is_not_retried():
    # Without attribution there is nothing to fall back from.
    seerr = FakeSeerr(
        errors=[SeerrError("Seerr returned 404", status=404)],
        supports_attribution=False,
    )
    cog, store, _ = _cog(seerr)
    interaction = FakeInteraction()

    await cog.submit_issue(interaction, _result(), ISSUE_VIDEO, "Neo", "no subs")

    assert len(seerr.calls) == 1
    assert seerr.calls[0]["user_id"] is None
    assert not store.issues
    assert interaction.followups
