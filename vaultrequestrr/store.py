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
"""


@dataclass(frozen=True)
class AccountLink:
    discord_id: str
    seerr_user_id: int
    plex_username: str | None
    email: str | None
    linked_at: str


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
        await self._db.execute(_SCHEMA)
        await self._db.commit()

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


def _row_to_link(row: aiosqlite.Row) -> AccountLink:
    return AccountLink(
        discord_id=row["discord_id"],
        seerr_user_id=row["seerr_user_id"],
        plex_username=row["plex_username"],
        email=row["email"],
        linked_at=row["linked_at"],
    )
