"""Async SQLite persistence layer (backed by ``aiosqlite``).

Two tables are maintained:

``users``
    One row per Telegram user (keyed by ``chat_id``). Holds the user's private
    Twitter cookies, optional xAI Grok API key, the id of the last tweet that
    was forwarded and an ``is_active`` switch used by the scheduler.

``translations_cache``
    Memoised Grok translations keyed by ``(tweet_id, chat_id)`` so that a user
    is never billed twice for translating the same tweet.

A single shared connection guarded by an :class:`asyncio.Lock` is used. That is
more than enough throughput for a bot that polls once every few minutes, and it
avoids SQLite's multi-connection write-contention problems.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

import aiosqlite

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    chat_id        INTEGER PRIMARY KEY,
    auth_token     TEXT NOT NULL,
    ct0            TEXT NOT NULL,
    xai_api_key    TEXT,
    last_tweet_id  TEXT,
    last_for_you_id TEXT,
    is_active      BOOLEAN NOT NULL DEFAULT 1,
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS translations_cache (
    tweet_id     TEXT NOT NULL,
    chat_id      INTEGER NOT NULL,
    translation  TEXT NOT NULL,
    PRIMARY KEY (tweet_id, chat_id)
);

CREATE TABLE IF NOT EXISTS feed_settings (
    chat_id                 INTEGER PRIMARY KEY,
    for_you_enabled         BOOLEAN NOT NULL DEFAULT 0,
    for_you_min_likes       INTEGER NOT NULL DEFAULT 0,
    for_you_min_retweets    INTEGER NOT NULL DEFAULT 0,
    for_you_min_impressions INTEGER NOT NULL DEFAULT 0
);
"""


@dataclass
class UserRecord:
    """Typed view over a row of the ``users`` table."""

    chat_id: int
    auth_token: str
    ct0: str
    xai_api_key: Optional[str]
    last_tweet_id: Optional[str]
    is_active: bool
    created_at: Optional[str] = None
    # Newest tweet already delivered from the filtered "For you" feed.
    last_for_you_id: Optional[str] = None

    @classmethod
    def from_row(cls, row: Sequence[Any]) -> "UserRecord":
        """Build a :class:`UserRecord` from an ``aiosqlite.Row`` / tuple."""
        keys = row.keys() if hasattr(row, "keys") else []
        return cls(
            chat_id=int(row["chat_id"]),
            auth_token=row["auth_token"],
            ct0=row["ct0"],
            xai_api_key=row["xai_api_key"],
            last_tweet_id=row["last_tweet_id"],
            is_active=bool(row["is_active"]),
            created_at=row["created_at"] if "created_at" in keys else None,
            last_for_you_id=(
                row["last_for_you_id"] if "last_for_you_id" in keys else None
            ),
        )



@dataclass
class FeedSettings:
    """Per-user dual-feed preferences (row of the ``feed_settings`` table).

    Defaults match the environment defaults, so a user without an explicit row
    simply follows the server-wide configuration.
    """

    chat_id: int
    for_you_enabled: bool = False
    for_you_min_likes: int = 0
    for_you_min_retweets: int = 0
    for_you_min_impressions: int = 0


class Database:
    """Thin async wrapper around a single SQLite connection."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._conn: Optional[aiosqlite.Connection] = None
        # Serialises writes so two coroutines never interleave on one connection.
        self._lock = asyncio.Lock()

    # -- lifecycle ---------------------------------------------------------
    async def connect(self) -> None:
        """Open the connection and apply sensible pragmas."""
        if self._conn is not None:
            return

        # Make sure the parent directory exists before SQLite tries to create
        # the database file (SQLite will not create missing directories).
        self._path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        # WAL improves concurrent read/write behaviour significantly.
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute("PRAGMA foreign_keys=ON;")
        await self._conn.commit()
        logger.info("Connected to SQLite database at %s", self._path)

    async def init(self) -> None:
        """Create tables if they do not yet exist."""
        if self._conn is None:
            await self.connect()
        assert self._conn is not None
        await self._conn.executescript(_SCHEMA)
        # Runtime migration: databases created before the dual-feed update do
        # not have the ``last_for_you_id`` column yet (fresh databases already
        # got it from CREATE TABLE above, so this then fails with a duplicate
        # -column error which is harmless).
        try:
            await self._conn.execute(
                "ALTER TABLE users ADD COLUMN last_for_you_id TEXT"
            )
        except sqlite3.OperationalError:
            pass
        await self._conn.commit()
        logger.info("Database schema ready.")

    async def close(self) -> None:
        """Close the underlying connection (idempotent)."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
            logger.info("SQLite connection closed.")

    # -- internal helpers --------------------------------------------------
    def _connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.connect() must be awaited before use.")
        return self._conn

    # -- users -------------------------------------------------------------
    async def upsert_user(
        self,
        chat_id: int,
        auth_token: str,
        ct0: str,
        xai_api_key: Optional[str] = None,
    ) -> None:
        """Insert a new user or refresh an existing user's credentials.

        Re-onboarding an existing user keeps their ``last_tweet_id`` (so they
        are not flooded with old tweets after refreshing expired cookies) but
        always re-activates the row.
        """
        conn = self._connection()
        async with self._lock:
            await conn.execute(
                """
                INSERT INTO users (chat_id, auth_token, ct0, xai_api_key, is_active)
                VALUES (?, ?, ?, ?, 1)
                ON CONFLICT(chat_id) DO UPDATE SET
                    auth_token  = excluded.auth_token,
                    ct0         = excluded.ct0,
                    xai_api_key = COALESCE(excluded.xai_api_key, users.xai_api_key),
                    is_active   = 1
                """,
                (chat_id, auth_token, ct0, xai_api_key),
            )
            await conn.commit()
        logger.info("Upserted profile for chat_id=%s", chat_id)

    async def get_user(self, chat_id: int) -> Optional[UserRecord]:
        """Return the profile for ``chat_id`` or ``None`` if not registered."""
        conn = self._connection()
        async with conn.execute(
            "SELECT * FROM users WHERE chat_id = ?", (chat_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return UserRecord.from_row(row) if row is not None else None

    async def get_active_users(self) -> list[UserRecord]:
        """Return every user whose feed forwarding is currently enabled."""
        conn = self._connection()
        async with conn.execute(
            "SELECT * FROM users WHERE is_active = 1"
        ) as cursor:
            rows = await cursor.fetchall()
        return [UserRecord.from_row(row) for row in rows]

    async def set_active(self, chat_id: int, is_active: bool) -> None:
        """Enable/disable forwarding for a single user."""
        conn = self._connection()
        async with self._lock:
            await conn.execute(
                "UPDATE users SET is_active = ? WHERE chat_id = ?",
                (1 if is_active else 0, chat_id),
            )
            await conn.commit()
        logger.info("Set is_active=%s for chat_id=%s", is_active, chat_id)
    async def update_last_tweet_id(self, chat_id: int, tweet_id: str) -> None:
        """Persist the most recently forwarded tweet id for a user."""
        conn = self._connection()
        async with self._lock:
            await conn.execute(
                "UPDATE users SET last_tweet_id = ? WHERE chat_id = ?",
                (tweet_id, chat_id),
            )
            await conn.commit()

    async def update_last_for_you_id(self, chat_id: int, tweet_id: str) -> None:
        """Persist the newest tweet id delivered from the filtered For-you feed."""
        conn = self._connection()
        async with self._lock:
            await conn.execute(
                "UPDATE users SET last_for_you_id = ? WHERE chat_id = ?",
                (tweet_id, chat_id),
            )
            await conn.commit()

    async def save_session(self, chat_id: int, auth_token: str, ct0: str) -> None:
        """Store/refresh a user's X session cookies (mobile-webview flow).

        Thin wrapper over :meth:`upsert_user` so the webview and the text-cookie
        onboarding share one code path (existing keys/settings are preserved).
        """
        await self.upsert_user(chat_id, auth_token, ct0)

    # -- feed settings -----------------------------------------------------
    async def get_feed_settings(self, chat_id: int) -> Optional[FeedSettings]:
        """Return the user's feed preferences, or ``None`` when never set.

        ``None`` means "no overrides" - callers fall back to the server-wide
        environment configuration (``FOR_YOU_*`` settings).
        """
        conn = self._connection()
        async with conn.execute(
            "SELECT * FROM feed_settings WHERE chat_id = ?", (chat_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return FeedSettings(
            chat_id=chat_id,
            for_you_enabled=bool(row["for_you_enabled"]),
            for_you_min_likes=int(row["for_you_min_likes"]),
            for_you_min_retweets=int(row["for_you_min_retweets"]),
            for_you_min_impressions=int(row["for_you_min_impressions"]),
        )

    async def upsert_feed_settings(self, feed: FeedSettings) -> None:
        """Insert or update the feed preferences for ``feed.chat_id``."""
        conn = self._connection()
        async with self._lock:
            await conn.execute(
                """
                INSERT INTO feed_settings (
                    chat_id, for_you_enabled, for_you_min_likes,
                    for_you_min_retweets, for_you_min_impressions
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    for_you_enabled         = excluded.for_you_enabled,
                    for_you_min_likes       = excluded.for_you_min_likes,
                    for_you_min_retweets    = excluded.for_you_min_retweets,
                    for_you_min_impressions = excluded.for_you_min_impressions
                """,
                (
                    feed.chat_id,
                    1 if feed.for_you_enabled else 0,
                    feed.for_you_min_likes,
                    feed.for_you_min_retweets,
                    feed.for_you_min_impressions,
                ),
            )
            await conn.commit()
        logger.info("Saved feed settings for chat_id=%s", feed.chat_id)

    async def set_grok_key(self, chat_id: int, api_key: Optional[str]) -> None:
        """Store (or clear, when ``api_key`` is ``None``) the user's xAI key."""
        conn = self._connection()
        async with self._lock:
            await conn.execute(
                "UPDATE users SET xai_api_key = ? WHERE chat_id = ?",
                (api_key, chat_id),
            )
            await conn.commit()
        logger.info(
            "Updated Grok key for chat_id=%s (set=%s)", chat_id, api_key is not None
        )

    async def delete_user(self, chat_id: int) -> None:
        """Wipe a user's credentials and any cached translations."""
        conn = self._connection()
        async with self._lock:
            await conn.execute("DELETE FROM users WHERE chat_id = ?", (chat_id,))
            await conn.execute(
                "DELETE FROM translations_cache WHERE chat_id = ?", (chat_id,)
            )
            await conn.commit()
        logger.info("Deleted all data for chat_id=%s", chat_id)

    # -- translation cache -------------------------------------------------
    async def get_cached_translation(
        self, tweet_id: str, chat_id: int
    ) -> Optional[str]:
        """Return a previously stored translation, if any."""
        conn = self._connection()
        async with conn.execute(
            "SELECT translation FROM translations_cache "
            "WHERE tweet_id = ? AND chat_id = ?",
            (tweet_id, chat_id),
        ) as cursor:
            row = await cursor.fetchone()
        return row["translation"] if row is not None else None

    async def save_translation(
        self, tweet_id: str, chat_id: int, translation: str
    ) -> None:
        """Cache a translation so identical requests are never re-billed."""
        conn = self._connection()
        async with self._lock:
            await conn.execute(
                """
                INSERT INTO translations_cache (tweet_id, chat_id, translation)
                VALUES (?, ?, ?)
                ON CONFLICT(tweet_id, chat_id) DO UPDATE SET
                    translation = excluded.translation
                """,
                (tweet_id, chat_id, translation),
            )
            await conn.commit()

    # -- aggregates --------------------------------------------------------
    async def count_users(self) -> tuple[int, int]:
        """Return ``(total_users, active_users)`` - handy for the health page."""
        conn = self._connection()
        async with conn.execute("SELECT COUNT(*) AS c FROM users") as cursor:
            total_row = await cursor.fetchone()
        async with conn.execute(
            "SELECT COUNT(*) AS c FROM users WHERE is_active = 1"
        ) as cursor:
            active_row = await cursor.fetchone()
        return int(total_row["c"]), int(active_row["c"])


