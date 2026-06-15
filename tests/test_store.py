import pytest

from vaultrequestrr.store import LinkStore


@pytest.fixture
async def store(tmp_path):
    s = LinkStore(str(tmp_path / "links.sqlite3"))
    await s.connect()
    try:
        yield s
    finally:
        await s.close()


@pytest.mark.asyncio
async def test_get_missing_returns_none(store):
    assert await store.get("123") is None


@pytest.mark.asyncio
async def test_save_and_get(store):
    saved = await store.save("123", 7, "plexuser", "p@example.com")
    assert saved.seerr_user_id == 7

    link = await store.get("123")
    assert link is not None
    assert link.discord_id == "123"
    assert link.seerr_user_id == 7
    assert link.plex_username == "plexuser"
    assert link.email == "p@example.com"
    assert link.linked_at


@pytest.mark.asyncio
async def test_save_upserts(store):
    await store.save("123", 7, "old", "old@example.com")
    await store.save("123", 9, "new", "new@example.com")
    link = await store.get("123")
    assert link.seerr_user_id == 9
    assert link.plex_username == "new"


@pytest.mark.asyncio
async def test_remove(store):
    await store.save("123", 7, "u", "e")
    await store.remove("123")
    assert await store.get("123") is None


# -- app settings ----------------------------------------------------------


@pytest.mark.asyncio
async def test_app_setting_get_set_upsert(store):
    assert await store.get_setting("seerr_url") is None
    await store.set_setting("seerr_url", "http://a:5055")
    assert await store.get_setting("seerr_url") == "http://a:5055"
    await store.set_setting("seerr_url", "http://b:5055")
    assert await store.get_setting("seerr_url") == "http://b:5055"


# -- tracked issues --------------------------------------------------------


@pytest.mark.asyncio
async def test_tracked_issue_roundtrip_and_pending(store):
    await store.add_tracked_issue(
        issue_id=5,
        discord_id="123",
        media_type="movie",
        tmdb_id=603,
        title="The Matrix",
        issue_type=1,
        message="no subs",
        status=1,
    )

    pending = await store.pending_issues()
    assert [i.issue_id for i in pending] == [5]
    recent = await store.recent_issues()
    assert recent[0].title == "The Matrix" and recent[0].message == "no subs"

    one = await store.get_tracked_issue(5)
    assert one is not None and one.discord_id == "123" and not one.notified_resolved


@pytest.mark.asyncio
async def test_mark_issue_resolved_drops_from_pending(store):
    await store.add_tracked_issue(5, "123", "movie", 603, "X", 1, "m", 1)
    await store.mark_issue(5, status=2, notified_resolved=True)

    assert await store.pending_issues() == []
    one = await store.get_tracked_issue(5)
    assert one.status == 2 and one.notified_resolved


@pytest.mark.asyncio
async def test_persists_across_reconnect(tmp_path):
    path = str(tmp_path / "links.sqlite3")
    s1 = LinkStore(path)
    await s1.connect()
    await s1.save("123", 42, "u", "e")
    await s1.close()

    s2 = LinkStore(path)
    await s2.connect()
    link = await s2.get("123")
    await s2.close()
    assert link is not None and link.seerr_user_id == 42
