"""Persistent Discord<->Seerr account link storage (SQLite via aiosqlite)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS account_links (
    discord_id     TEXT PRIMARY KEY,
    seerr_user_id  INTEGER NOT NULL,
    plex_username  TEXT,
    email          TEXT,
    linked_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tracked_requests (
    request_id          INTEGER PRIMARY KEY,
    discord_id          TEXT NOT NULL,
    media_type          TEXT NOT NULL,
    tmdb_id             INTEGER,
    title               TEXT,
    seasons             TEXT,
    request_status      INTEGER,
    media_status        INTEGER,
    notified_available  INTEGER NOT NULL DEFAULT 0,
    notified_declined   INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    updated_at          TEXT
);

CREATE TABLE IF NOT EXISTS app_settings (
    key    TEXT PRIMARY KEY,
    value  TEXT
);

CREATE TABLE IF NOT EXISTS tracked_issues (
    issue_id            INTEGER PRIMARY KEY,
    discord_id          TEXT NOT NULL,
    media_type          TEXT,
    tmdb_id             INTEGER,
    title               TEXT,
    issue_type          INTEGER,
    message             TEXT,
    status              INTEGER,
    problem_season      INTEGER,
    problem_episode     INTEGER,
    notified_resolved   INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    updated_at          TEXT
);
"""

# Columns added after the table first shipped; applied idempotently on connect.
_MIGRATIONS = {
    "tracked_issues": {
        "problem_season": "INTEGER",
        "problem_episode": "INTEGER",
    },
}


@dataclass(frozen=True)
class AccountLink:
    discord_id: str
    seerr_user_id: int
    plex_username: str | None
    email: str | None
    linked_at: str


@dataclass(frozen=True)
class TrackedRequest:
    request_id: int
    discord_id: str
    media_type: str
    tmdb_id: int | None
    title: str | None
    seasons: str | None
    request_status: int | None
    media_status: int | None
    notified_available: bool
    notified_declined: bool
    created_at: str
    updated_at: str | None


@dataclass(frozen=True)
class TrackedIssue:
    issue_id: int
    discord_id: str
    media_type: str | None
    tmdb_id: int | None
    title: str | None
    issue_type: int | None
    message: str | None
    status: int | None
    problem_season: int | None
    problem_episode: int | None
    notified_resolved: bool
    created_at: str
    updated_at: str | None


class LinkStore:
    def __init__(self, database_path: str) -> None:
        self._path = database_path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        directory = os.path.dirname(self._path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._apply_migrations()
        await self._db.commit()

    async def _apply_migrations(self) -> None:
        """Add columns introduced after a table first shipped (idempotent)."""
        for table, columns in _MIGRATIONS.items():
            async with self._db.execute(f"PRAGMA table_info({table})") as cursor:
                existing = {row["name"] for row in await cursor.fetchall()}
            for name, decl in columns.items():
                if name not in existing:
                    await self._db.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {decl}"
                    )

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("LinkStore.connect() must be called before use")
        return self._db

    async def get(self, discord_id: str) -> AccountLink | None:
        async with self._conn.execute(
            "SELECT * FROM account_links WHERE discord_id = ?", (discord_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return _row_to_link(row) if row else None

    async def save(
        self,
        discord_id: str,
        seerr_user_id: int,
        plex_username: str | None,
        email: str | None,
    ) -> AccountLink:
        linked_at = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            """
            INSERT INTO account_links (discord_id, seerr_user_id, plex_username, email, linked_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(discord_id) DO UPDATE SET
                seerr_user_id = excluded.seerr_user_id,
                plex_username = excluded.plex_username,
                email = excluded.email,
                linked_at = excluded.linked_at
            """,
            (discord_id, seerr_user_id, plex_username, email, linked_at),
        )
        await self._conn.commit()
        return AccountLink(discord_id, seerr_user_id, plex_username, email, linked_at)

    async def remove(self, discord_id: str) -> None:
        await self._conn.execute(
            "DELETE FROM account_links WHERE discord_id = ?", (discord_id,)
        )
        await self._conn.commit()

    async def list_links(self) -> list[AccountLink]:
        async with self._conn.execute(
            "SELECT * FROM account_links ORDER BY linked_at DESC"
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_link(row) for row in rows]

    # -- tracked requests (notifications + activity log) -------------------

    async def add_tracked_request(
        self,
        request_id: int,
        discord_id: str,
        media_type: str,
        tmdb_id: int | None,
        title: str | None,
        seasons: str | None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            """
            INSERT INTO tracked_requests
                (request_id, discord_id, media_type, tmdb_id, title, seasons, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(request_id) DO UPDATE SET
                discord_id = excluded.discord_id,
                title = excluded.title,
                seasons = excluded.seasons
            """,
            (request_id, discord_id, media_type, tmdb_id, title, seasons, now, now),
        )
        await self._conn.commit()

    async def pending_tracked(self) -> list[TrackedRequest]:
        async with self._conn.execute(
            """
            SELECT * FROM tracked_requests
            WHERE notified_available = 0 AND notified_declined = 0
            ORDER BY created_at ASC
            """
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_tracked(row) for row in rows]

    async def recent_tracked(self, limit: int = 50) -> list[TrackedRequest]:
        async with self._conn.execute(
            "SELECT * FROM tracked_requests ORDER BY created_at DESC LIMIT ?", (limit,)
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_tracked(row) for row in rows]

    async def mark_tracked(
        self,
        request_id: int,
        *,
        request_status: int | None = None,
        media_status: int | None = None,
        notified_available: bool | None = None,
        notified_declined: bool | None = None,
    ) -> None:
        sets = ["updated_at = ?"]
        params: list[object] = [datetime.now(timezone.utc).isoformat()]
        if request_status is not None:
            sets.append("request_status = ?")
            params.append(request_status)
        if media_status is not None:
            sets.append("media_status = ?")
            params.append(media_status)
        if notified_available is not None:
            sets.append("notified_available = ?")
            params.append(1 if notified_available else 0)
        if notified_declined is not None:
            sets.append("notified_declined = ?")
            params.append(1 if notified_declined else 0)
        params.append(request_id)
        await self._conn.execute(
            f"UPDATE tracked_requests SET {', '.join(sets)} WHERE request_id = ?", params
        )
        await self._conn.commit()

    async def remove_tracked(self, request_id: int) -> None:
        await self._conn.execute(
            "DELETE FROM tracked_requests WHERE request_id = ?", (request_id,)
        )
        await self._conn.commit()

    # -- app settings (web-editable overrides, persisted across restarts) --

    async def get_setting(self, key: str) -> str | None:
        async with self._conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (key,)
        ) as cursor:
            row = await cursor.fetchone()
        return row["value"] if row else None

    async def set_setting(self, key: str, value: str) -> None:
        await self._conn.execute(
            """
            INSERT INTO app_settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        await self._conn.commit()

    # -- tracked issues (resolution notifications + dashboard) -------------

    async def add_tracked_issue(
        self,
        issue_id: int,
        discord_id: str,
        media_type: str | None,
        tmdb_id: int | None,
        title: str | None,
        issue_type: int | None,
        message: str | None,
        status: int | None,
        problem_season: int | None = None,
        problem_episode: int | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            """
            INSERT INTO tracked_issues
                (issue_id, discord_id, media_type, tmdb_id, title, issue_type,
                 message, status, problem_season, problem_episode, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(issue_id) DO UPDATE SET
                discord_id = excluded.discord_id,
                title = excluded.title,
                issue_type = excluded.issue_type,
                message = excluded.message,
                status = excluded.status,
                problem_season = excluded.problem_season,
                problem_episode = excluded.problem_episode
            """,
            (issue_id, discord_id, media_type, tmdb_id, title, issue_type,
             message, status, problem_season, problem_episode, now, now),
        )
        await self._conn.commit()

    async def pending_issues(self) -> list[TrackedIssue]:
        async with self._conn.execute(
            """
            SELECT * FROM tracked_issues
            WHERE notified_resolved = 0
            ORDER BY created_at ASC
            """
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_tracked_issue(row) for row in rows]

    async def recent_issues(self, limit: int = 100) -> list[TrackedIssue]:
        async with self._conn.execute(
            "SELECT * FROM tracked_issues ORDER BY created_at DESC LIMIT ?", (limit,)
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_tracked_issue(row) for row in rows]

    async def get_tracked_issue(self, issue_id: int) -> TrackedIssue | None:
        async with self._conn.execute(
            "SELECT * FROM tracked_issues WHERE issue_id = ?", (issue_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return _row_to_tracked_issue(row) if row else None

    async def mark_issue(
        self,
        issue_id: int,
        *,
        status: int | None = None,
        notified_resolved: bool | None = None,
    ) -> None:
        sets = ["updated_at = ?"]
        params: list[object] = [datetime.now(timezone.utc).isoformat()]
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if notified_resolved is not None:
            sets.append("notified_resolved = ?")
            params.append(1 if notified_resolved else 0)
        params.append(issue_id)
        await self._conn.execute(
            f"UPDATE tracked_issues SET {', '.join(sets)} WHERE issue_id = ?", params
        )
        await self._conn.commit()

    async def remove_issue(self, issue_id: int) -> None:
        await self._conn.execute(
            "DELETE FROM tracked_issues WHERE issue_id = ?", (issue_id,)
        )
        await self._conn.commit()


def _row_to_tracked(row: aiosqlite.Row) -> TrackedRequest:
    return TrackedRequest(
        request_id=row["request_id"],
        discord_id=row["discord_id"],
        media_type=row["media_type"],
        tmdb_id=row["tmdb_id"],
        title=row["title"],
        seasons=row["seasons"],
        request_status=row["request_status"],
        media_status=row["media_status"],
        notified_available=bool(row["notified_available"]),
        notified_declined=bool(row["notified_declined"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_tracked_issue(row: aiosqlite.Row) -> TrackedIssue:
    return TrackedIssue(
        issue_id=row["issue_id"],
        discord_id=row["discord_id"],
        media_type=row["media_type"],
        tmdb_id=row["tmdb_id"],
        title=row["title"],
        issue_type=row["issue_type"],
        message=row["message"],
        status=row["status"],
        problem_season=row["problem_season"],
        problem_episode=row["problem_episode"],
        notified_resolved=bool(row["notified_resolved"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_link(row: aiosqlite.Row) -> AccountLink:
    return AccountLink(
        discord_id=row["discord_id"],
        seerr_user_id=row["seerr_user_id"],
        plex_username=row["plex_username"],
        email=row["email"],
        linked_at=row["linked_at"],
    )
