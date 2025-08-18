# worldle_bot/core/db.py
"""
Database setup and helpers for Worldle bot.
Uses SQLite (aiosqlite).
"""

import logging
import aiosqlite

from . import config

log = logging.getLogger("worldle_bot")

# The bot instance will attach db here
db: aiosqlite.Connection | None = None


# -------------------------------------------------------------------
# Init
# -------------------------------------------------------------------
async def db_init():
    """Initialize the SQLite database and create required tables."""
    global db
    if db:
        return db

    db = await aiosqlite.connect(config.DB_PATH)
    await db.execute("PRAGMA journal_mode=WAL;")
    await db.execute("PRAGMA foreign_keys=ON;")

    # --- Minimal schema (expand with your real schema) ---
    await db.execute(
        """CREATE TABLE IF NOT EXISTS balances (
               guild_id   INTEGER NOT NULL,
               user_id    INTEGER NOT NULL,
               balance    INTEGER NOT NULL DEFAULT 0,
               stones     INTEGER NOT NULL DEFAULT 0,
               PRIMARY KEY (guild_id, user_id)
           )"""
    )

    await db.execute(
        """CREATE TABLE IF NOT EXISTS ground (
               guild_id INTEGER PRIMARY KEY,
               pot      INTEGER NOT NULL DEFAULT 0
           )"""
    )

    await db.execute(
        """CREATE TABLE IF NOT EXISTS ambient_rolls (
               guild_id INTEGER NOT NULL,
               slot     INTEGER NOT NULL,
               UNIQUE(guild_id, slot)
           )"""
    )

    await db.commit()
    log.info("Database initialized.")
    return db


# -------------------------------------------------------------------
# Convenience helpers
# -------------------------------------------------------------------
async def get_balance(gid: int, uid: int) -> int:
    async with db.execute(
        "SELECT balance FROM balances WHERE guild_id=? AND user_id=?",
        (gid, uid),
    ) as cur:
        row = await cur.fetchone()
    return row[0] if row else 0


async def change_balance(gid: int, uid: int, delta: int, announce_channel_id: int | None = None) -> int:
    """Increase/decrease a user’s balance. Returns new balance."""
    cur = await db.execute(
        "INSERT INTO balances(guild_id, user_id, balance, stones) VALUES(?, ?, ?, 0) "
        "ON CONFLICT(guild_id, user_id) DO UPDATE SET balance = balance + excluded.balance",
        (gid, uid, delta),
    )
    await db.commit()
    return await get_balance(gid, uid)


async def get_stones(gid: int, uid: int) -> int:
    async with db.execute(
        "SELECT stones FROM balances WHERE guild_id=? AND user_id=?",
        (gid, uid),
    ) as cur:
        row = await cur.fetchone()
    return row[0] if row else 0


async def change_stones(gid: int, uid: int, delta: int) -> int:
    """Increase/decrease a user’s stones. Returns new stone count."""
    cur = await db.execute(
        "INSERT INTO balances(guild_id, user_id, balance, stones) VALUES(?, ?, 0, ?) "
        "ON CONFLICT(guild_id, user_id) DO UPDATE SET stones = stones + excluded.stones",
        (gid, uid, delta),
    )
    await db.commit()
    return await get_stones(gid, uid)
