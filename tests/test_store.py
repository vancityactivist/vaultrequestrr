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
async def test_tracked_issue_stores_season_and_episode(store):
    await store.add_tracked_issue(
        issue_id=8, discord_id="1", media_type="tv", tmdb_id=95396, title="Severance",
        issue_type=1, message="bad ep", status=1, problem_season=1, problem_episode=2,
    )
    one = await store.get_tracked_issue(8)
    assert one.problem_season == 1 and one.problem_episode == 2


@pytest.mark.asyncio
async def test_mark_issue_resolved_drops_from_pending(store):
    await store.add_tracked_issue(5, "123", "movie", 603, "X", 1, "m", 1)
    await store.mark_issue(5, status=2, notified_resolved=True)

    assert await store.pending_issues() == []
    one = await store.get_tracked_issue(5)
    assert one.status == 2 and one.notified_resolved


@pytest.mark.asyncio
async def test_migration_adds_episode_columns_to_old_db(tmp_path):
    import aiosqlite

    path = str(tmp_path / "old.sqlite3")
    # Simulate a DB created before problem_season/episode existed.
    async with aiosqlite.connect(path) as db:
        await db.execute(
            """
            CREATE TABLE tracked_issues (
                issue_id INTEGER PRIMARY KEY, discord_id TEXT NOT NULL, media_type TEXT,
                tmdb_id INTEGER, title TEXT, issue_type INTEGER, message TEXT, status INTEGER,
                notified_resolved INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT
            )
            """
        )
        await db.commit()

    s = LinkStore(path)
    await s.connect()  # should ALTER in the new columns without error
    try:
        await s.add_tracked_issue(
            1, "1", "tv", 1, "X", 1, "m", 1, problem_season=2, problem_episode=3
        )
        one = await s.get_tracked_issue(1)
        assert one.problem_season == 2 and one.problem_episode == 3
    finally:
        await s.close()


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


@pytest.mark.asyncio
async def test_arr_instance_crud(store):
    inst = await store.add_arr_instance(
        kind="radarr", label="Radarr", base_url="http://r:7878", api_key="k",
    )
    assert inst.id and inst.is_default is False
    fetched = await store.get_arr_instance(inst.id)
    assert fetched is not None and fetched.base_url == "http://r:7878"

    await store.update_arr_instance(
        inst.id, label="Radarr HD", base_url="http://r:7878", api_key="k2", is_4k=True,
    )
    updated = await store.get_arr_instance(inst.id)
    assert updated.label == "Radarr HD" and updated.api_key == "k2" and updated.is_4k

    await store.delete_arr_instance(inst.id)
    assert await store.get_arr_instance(inst.id) is None


@pytest.mark.asyncio
async def test_arr_default_is_unique_per_kind(store):
    a = await store.add_arr_instance(
        kind="radarr", label="HD", base_url="http://a", api_key="k", is_default=True,
    )
    b = await store.add_arr_instance(
        kind="radarr", label="4K", base_url="http://b", api_key="k", is_4k=True, is_default=True,
    )
    # Sonarr default is independent of Radarr's.
    s = await store.add_arr_instance(
        kind="sonarr", label="Sonarr", base_url="http://s", api_key="k", is_default=True,
    )
    radarrs = {i.label: i.is_default for i in await store.list_arr_instances("radarr")}
    assert radarrs == {"HD": False, "4K": True}  # newest default wins
    assert (await store.get_arr_instance(s.id)).is_default is True
    assert len(await store.list_arr_instances()) == 3
    assert len(await store.list_arr_instances("sonarr")) == 1
