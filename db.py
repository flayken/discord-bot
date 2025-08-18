# -------------------- DB --------------------
DB_FILE = pathlib.Path(DB_PATH)
DB_FILE.parent.mkdir(parents=True, exist_ok=True)

async def add_column_if_missing(db, table: str, column: str, decl: str):
    async with db.execute(f"PRAGMA table_info({table})") as c:
        cols = [r[1] for r in await c.fetchall()]
    if column not in cols:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        await db.commit()
        log.info(f"[db] migrated: added {table}.{column}")

# --- place this helper ABOVE db_init() ---
async def migrate_solo_daily_schema(db: aiosqlite.Connection):
    """Migrate legacy solo_daily(guild_id,user_id,day,game,...) -> new (guild_id,user_id,date,plays)."""
    # If table doesn't exist yet, nothing to do.
    async with db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='solo_daily'") as cur:
        if not await cur.fetchone():
            return

    # Inspect current columns
    async with db.execute("PRAGMA table_info(solo_daily)") as cur:
        cols = [r[1] for r in await cur.fetchall()]

    # Already new shape? (has 'date', and no legacy 'day'/'game')
    if "date" in cols and "day" not in cols and "game" not in cols:
        return

    # Only migrate if legacy columns exist
    if "day" in cols:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS solo_daily_v2(
                guild_id INTEGER NOT NULL,
                user_id  INTEGER NOT NULL,
                date     TEXT    NOT NULL,
                plays    INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(guild_id, user_id, date)
            )
        """)

        has_plays = "plays" in cols  # Some legacy schemas already had a plays column
        if has_plays:
            await db.execute("""
                INSERT INTO solo_daily_v2(guild_id, user_id, date, plays)
                SELECT guild_id, user_id, day AS date, SUM(COALESCE(plays, 1))
                FROM solo_daily
                GROUP BY guild_id, user_id, day
            """)
        else:
            await db.execute("""
                INSERT INTO solo_daily_v2(guild_id, user_id, date, plays)
                SELECT guild_id, user_id, day AS date, COUNT(*)
                FROM solo_daily
                GROUP BY guild_id, user_id, day
            """)

        # Swap tables (drops any legacy unique index on (guild_id,user_id,day,game))
        await db.execute("DROP TABLE solo_daily")
        await db.execute("ALTER TABLE solo_daily_v2 RENAME TO solo_daily")
        await db.commit()


async def db_init():
    bot.db = await aiosqlite.connect(DB_FILE.as_posix())

    # --- Core tables (idempotent) ---
    await bot.db.execute("""CREATE TABLE IF NOT EXISTS wallet(
        guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        balance INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(guild_id, user_id))""")

    await bot.db.execute("""CREATE TABLE IF NOT EXISTS inv(
        guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        stones INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(guild_id, user_id))""")

    await bot.db.execute("""CREATE TABLE IF NOT EXISTS cooldown(
        guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        last_pray TEXT, last_beg TEXT,
        PRIMARY KEY(guild_id, user_id))""")

    await bot.db.execute("""CREATE TABLE IF NOT EXISTS ground(
        guild_id INTEGER NOT NULL PRIMARY KEY,
        pot INTEGER NOT NULL DEFAULT 0)""")

    await bot.db.execute("""CREATE TABLE IF NOT EXISTS role_tier(
        guild_id INTEGER NOT NULL, role_id INTEGER NOT NULL,
        min_balance INTEGER NOT NULL,
        PRIMARY KEY(guild_id, role_id))""")

    await bot.db.execute("""CREATE TABLE IF NOT EXISTS guild_cfg(
        guild_id INTEGER NOT NULL PRIMARY KEY,
        bounty_channel_id INTEGER,
        worldler_role_id INTEGER)""")

    await bot.db.execute("""CREATE TABLE IF NOT EXISTS bounty_state(
        guild_id INTEGER NOT NULL, date TEXT NOT NULL,
        drops_today INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(guild_id, date))""")

    # New-style solo_daily (will be ignored if legacy table exists; we migrate below)
    await bot.db.execute("""CREATE TABLE IF NOT EXISTS solo_daily(
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        date TEXT NOT NULL,
        plays INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(guild_id, user_id, date))""")

    # Streaks
    await bot.db.execute("""CREATE TABLE IF NOT EXISTS solo_streak(
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        last_date TEXT,
        cur INTEGER NOT NULL DEFAULT 0,
        best INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(guild_id, user_id))""")

    # Casino pot
    await bot.db.execute("""CREATE TABLE IF NOT EXISTS casino_pot(
        guild_id INTEGER NOT NULL PRIMARY KEY,
        pot INTEGER NOT NULL DEFAULT 10)""")

    # Anti-bully per-day stones
    await bot.db.execute("""CREATE TABLE IF NOT EXISTS stone_daily(
        guild_id INTEGER NOT NULL,
        attacker_id INTEGER NOT NULL,
        target_id INTEGER NOT NULL,
        date TEXT NOT NULL,
        count INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(guild_id, attacker_id, target_id, date))""")

        # Per-user stats for leaderboards
    await bot.db.execute("""CREATE TABLE IF NOT EXISTS stats(
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        bounties_won    INTEGER NOT NULL DEFAULT 0,
        stones_thrown   INTEGER NOT NULL DEFAULT 0,
        stoned_received INTEGER NOT NULL DEFAULT 0,
        solo_fails      INTEGER NOT NULL DEFAULT 0,
        snipes          INTEGER NOT NULL DEFAULT 0,  -- successful shots you made
        sniped          INTEGER NOT NULL DEFAULT 0,  -- times you were sniped
        PRIMARY KEY(guild_id, user_id)
    )""")

    await bot.db.execute("""CREATE TABLE IF NOT EXISTS ambient_rolls(
      guild_id INTEGER NOT NULL,
      slot     INTEGER NOT NULL,
      PRIMARY KEY (guild_id, slot)
    )""")




    await bot.db.commit()

    # --- Column migrations (idempotent) ---
    await add_column_if_missing(bot.db, "guild_cfg", "bounty_role_id", "INTEGER")
    await add_column_if_missing(bot.db, "guild_cfg", "last_bounty_ts", "INTEGER DEFAULT 0")
    await add_column_if_missing(bot.db, "guild_cfg", "solo_category_id", "INTEGER")
    await add_column_if_missing(bot.db, "guild_cfg", "announcements_channel_id", "INTEGER")
    await add_column_if_missing(bot.db, "guild_cfg", "last_bounty_hour", "INTEGER DEFAULT 0")
    await add_column_if_missing(bot.db, "inv", "badge", "INTEGER DEFAULT 0")
    await add_column_if_missing(bot.db, "inv", "chickens", "INTEGER DEFAULT 0")
    await add_column_if_missing(bot.db, "inv", "protected_until_ts", "INTEGER DEFAULT 0")
    await add_column_if_missing(bot.db, "inv", "sniper", "INTEGER DEFAULT 0")
    await add_column_if_missing(bot.db, "stats", "snipes", "INTEGER NOT NULL DEFAULT 0")
    await add_column_if_missing(bot.db, "stats", "sniped", "INTEGER NOT NULL DEFAULT 0")
    await add_column_if_missing(bot.db, "inv", "dungeon_tickets_t1", "INTEGER DEFAULT 0")
    await add_column_if_missing(bot.db, "inv", "dungeon_tickets_t2", "INTEGER DEFAULT 0")
    await add_column_if_missing(bot.db, "inv", "dungeon_tickets_t3", "INTEGER DEFAULT 0")
    await add_column_if_missing(bot.db, "guild_cfg", "suppress_bounty_ping", "INTEGER DEFAULT 0")
    await add_column_if_missing(bot.db, "guild_cfg", "drops_channel_id", "INTEGER")





    await bot.db.commit()

    # --- IMPORTANT: migrate legacy solo_daily schema (with day/game) to new date/plays ---
    try:
        await migrate_solo_daily_schema(bot.db)
    except Exception as e:
        log.warning(f"[db] solo_daily schema migration failed (will keep running): {e}")


# ------- DB helpers -------

# --- resilient sending helper (handles 5xx like 503 with retries) ---
async def safe_send(channel: discord.abc.Messageable, content=None, **kwargs):
    """Send a message with small retries on Discord 5xx."""
    backoffs = [0.6, 1.2, 2.4]  # total ~4.2s worst case
    for i in range(len(backoffs) + 1):
        try:
            return await channel.send(content, **kwargs)
        except (discord.errors.DiscordServerError, discord.HTTPException) as e:
            status = getattr(e, "status", None)
            # Retry only on transient server errors
            if isinstance(e, discord.errors.DiscordServerError) or status in (500, 502, 503, 504):
                if i < len(backoffs):
                    await asyncio.sleep(backoffs[i])
                    continue
                # Give up quietly after retries
                log.warning(f"[safe_send] giving up after retries: {e}")
                return None
            # Other errors: surface them so you notice real issues
            raise


# ---------- UI helpers: boxed/panel sends ----------
PANEL_COLOR = 0x2B2D31  # Discord dark-embed graphite

def make_panel(title: str, description: str = "", *, fields: list[tuple[str,str,bool]] | None = None, footer: str | None = None, icon: str | None = None) -> discord.Embed:
    t = f"{icon} {title}" if icon else title
    emb = discord.Embed(title=t, description=description, color=PANEL_COLOR)
    if fields:
        for name, value, inline in fields:
            emb.add_field(name=name, value=value, inline=inline)
    if footer:
        emb.set_footer(text=footer)
    return emb

async def send_boxed(target, title: str, description: str = "", *, fields: list[tuple[str,str,bool]] | None = None, footer: str | None = None, icon: str | None = None, ephemeral: bool = False):
    """Send a consistent boxed/panel message. Works with Interaction or channel."""
    emb = make_panel(title, description, fields=fields, footer=footer, icon=icon)
    if isinstance(target, discord.Interaction):
        if not target.response.is_done():
            return await target.response.send_message(embed=emb, ephemeral=ephemeral)
        return await target.followup.send(embed=emb, ephemeral=ephemeral)
    # channel-like
    return await safe_send(target, embed=emb)



async def get_balance(gid: int, uid: int) -> int:
    async with bot.db.execute("SELECT balance FROM wallet WHERE guild_id=? AND user_id=?", (gid, uid)) as cur:
        row = await cur.fetchone()
    return row[0] if row else 0

async def change_balance(gid: int, uid: int, delta: int, *, announce_channel_id: Optional[int] = None):
    await bot.db.execute("""
      INSERT INTO wallet(guild_id,user_id,balance) VALUES(?,?,?)
      ON CONFLICT(guild_id,user_id) DO UPDATE SET balance=wallet.balance+excluded.balance""",
      (gid, uid, delta))
    await bot.db.commit()
    try:
        await _sync_member_roles_after_balance_change(gid, uid, announce_channel_id)
    except Exception as e:
        log.warning(f"role sync after balance change failed: {e}")

async def get_stones(gid: int, uid: int) -> int:
    async with bot.db.execute("SELECT stones FROM inv WHERE guild_id=? AND user_id=?", (gid, uid)) as cur:
        row = await cur.fetchone()
    return row[0] if row else 0

async def change_stones(gid: int, uid: int, delta: int):
    await bot.db.execute("""
      INSERT INTO inv(guild_id,user_id,stones) VALUES(?,?,?)
      ON CONFLICT(guild_id,user_id) DO UPDATE SET stones=inv.stones+excluded.stones""",
      (gid, uid, delta))
    await bot.db.commit()

async def get_badge(gid: int, uid: int) -> int:
    async with bot.db.execute("SELECT badge FROM inv WHERE guild_id=? AND user_id=?", (gid, uid)) as cur:
        row = await cur.fetchone()
    return row[0] if row else 0

async def set_badge(gid: int, uid: int, val: int):
    await bot.db.execute("""
      INSERT INTO inv(guild_id,user_id,badge) VALUES(?,?,?)
      ON CONFLICT(guild_id,user_id) DO UPDATE SET badge=excluded.badge""",
      (gid, uid, val))
    await bot.db.commit()

async def get_chickens(gid: int, uid: int) -> int:
    async with bot.db.execute("SELECT chickens FROM inv WHERE guild_id=? AND user_id=?", (gid, uid)) as cur:
        row = await cur.fetchone()
    return row[0] if row else 0

async def change_chickens(gid: int, uid: int, delta: int):
    await bot.db.execute("""
      INSERT INTO inv(guild_id,user_id,chickens) VALUES(?,?,?)
      ON CONFLICT(guild_id,user_id) DO UPDATE SET chickens=inv.chickens+excluded.chickens""",
      (gid, uid, delta))
    await bot.db.commit()

async def get_protection_until(gid: int, uid: int) -> int:
    async with bot.db.execute("SELECT protected_until_ts FROM inv WHERE guild_id=? AND user_id=?", (gid, uid)) as cur:
        row = await cur.fetchone()
    return row[0] if row else 0

async def set_protection_until(gid: int, uid: int, ts: int):
    await bot.db.execute("""
      INSERT INTO inv(guild_id,user_id,protected_until_ts) VALUES(?,?,?)
      ON CONFLICT(guild_id,user_id) DO UPDATE SET protected_until_ts=excluded.protected_until_ts""",
      (gid, uid, ts))
    await bot.db.commit()

async def get_sniper(gid: int, uid: int) -> int:
    async with bot.db.execute("SELECT sniper FROM inv WHERE guild_id=? AND user_id=?", (gid, uid)) as cur:
        row = await cur.fetchone()
    return row[0] if row else 0

async def set_sniper(gid: int, uid: int, val: int):
    await bot.db.execute("""
      INSERT INTO inv(guild_id,user_id,sniper) VALUES(?,?,?)
      ON CONFLICT(guild_id,user_id) DO UPDATE SET sniper=excluded.sniper""",
      (gid, uid, val))
    await bot.db.commit()

async def get_pot(gid: int) -> int:
    async with bot.db.execute("SELECT pot FROM ground WHERE guild_id=?", (gid,)) as cur:
        row = await cur.fetchone()
    return row[0] if row else 0

async def add_to_pot(gid: int, delta: int):
    await bot.db.execute("""
      INSERT INTO ground(guild_id,pot) VALUES(?,?)
      ON CONFLICT(guild_id) DO UPDATE SET pot=ground.pot+excluded.pot""",
      (gid, delta))
    await bot.db.commit()

async def pop_all_from_pot(gid: int) -> int:
    async with bot.db.execute("SELECT pot FROM ground WHERE guild_id=?", (gid,)) as cur:
        row = await cur.fetchone()
    amt = row[0] if row else 0
    if amt > 0:
        await bot.db.execute("UPDATE ground SET pot=0 WHERE guild_id=?", (gid,))
        await bot.db.commit()
    return amt

async def _get_cd(gid: int, uid: int):
    async with bot.db.execute("SELECT last_pray,last_beg FROM cooldown WHERE guild_id=? AND user_id=?", (gid, uid)) as cur:
        row = await cur.fetchone()
    return row if row else (None, None)

async def _set_cd(gid: int, uid: int, field: str, val: str):
    await bot.db.execute("""
      INSERT INTO cooldown(guild_id,user_id,last_pray,last_beg) VALUES(?,?,NULL,NULL)
      ON CONFLICT(guild_id,user_id) DO NOTHING""", (gid, uid))
    await bot.db.execute(f"UPDATE cooldown SET {field}=? WHERE guild_id=? AND user_id=?", (val, gid, uid))
    await bot.db.commit()

async def get_cfg(gid: int):
    async with bot.db.execute(
        "SELECT bounty_channel_id, worldler_role_id, bounty_role_id, last_bounty_ts, "
        "solo_category_id, announcements_channel_id, last_bounty_hour, suppress_bounty_ping, "
        "drops_channel_id FROM guild_cfg WHERE guild_id=?",
        (gid,)
    ) as cur:
        row = await cur.fetchone()
    if row:
        return {
            "bounty_channel_id": row[0], "worldler_role_id": row[1], "bounty_role_id": row[2],
            "last_bounty_ts": row[3] or 0, "solo_category_id": row[4],
            "announcements_channel_id": row[5], "last_bounty_hour": row[6] or 0,
            "suppress_bounty_ping": row[7] or 0, "drops_channel_id": row[8],
        }
    return {
        "bounty_channel_id": None, "worldler_role_id": None, "bounty_role_id": None,
        "last_bounty_ts": 0, "solo_category_id": None, "announcements_channel_id": None,
        "last_bounty_hour": 0, "suppress_bounty_ping": 0, "drops_channel_id": None,
    }

async def set_cfg(gid: int, **kwargs):
    cfg = await get_cfg(gid); cfg.update(kwargs)
    await bot.db.execute("""
      INSERT INTO guild_cfg(
        guild_id, bounty_channel_id, worldler_role_id, bounty_role_id, last_bounty_ts,
        solo_category_id, announcements_channel_id, last_bounty_hour, suppress_bounty_ping,
        drops_channel_id
      )
      VALUES(?,?,?,?,?,?,?,?,?,?)
      ON CONFLICT(guild_id) DO UPDATE SET
        bounty_channel_id=excluded.bounty_channel_id,
        worldler_role_id=excluded.worldler_role_id,
        bounty_role_id=excluded.bounty_role_id,
        last_bounty_ts=excluded.last_bounty_ts,
        solo_category_id=excluded.solo_category_id,
        announcements_channel_id=excluded.announcements_channel_id,
        last_bounty_hour=excluded.last_bounty_hour,
        suppress_bounty_ping=excluded.suppress_bounty_ping,
        drops_channel_id=excluded.drops_channel_id
    """, (
        gid, cfg["bounty_channel_id"], cfg["worldler_role_id"], cfg["bounty_role_id"],
        cfg["last_bounty_ts"], cfg["solo_category_id"], cfg["announcements_channel_id"],
        cfg["last_bounty_hour"], cfg["suppress_bounty_ping"], cfg["drops_channel_id"],
    ))
    await bot.db.commit()




async def get_solo_plays_today(gid: int, uid: int, date: str) -> int:
    async with bot.db.execute(
        "SELECT plays FROM solo_daily WHERE guild_id=? AND user_id=? AND date=?",
        (gid, uid, date)
    ) as cur:
        row = await cur.fetchone()
    return row[0] if row else 0

async def inc_solo_plays_today(gid: int, uid: int, date: str):
    await bot.db.execute("""
      INSERT INTO solo_daily(guild_id,user_id,date,plays) VALUES(?,?,?,1)
      ON CONFLICT(guild_id,user_id,date) DO UPDATE SET plays=solo_daily.plays+1
    """, (gid, uid, date))
    await bot.db.commit()

# put this alongside the other DB helpers, right after inc_solo_plays_today(...)
async def dec_solo_plays_on_date(gid: int, uid: int, date: str):
    """Decrement the user's solo play count for a specific UK date (no-op if 0)."""
    await bot.db.execute(
        """
        UPDATE solo_daily
           SET plays = CASE WHEN plays > 0 THEN plays - 1 ELSE 0 END
         WHERE guild_id=? AND user_id=? AND date=?
        """,
        (gid, uid, date),
    )
    await bot.db.commit()


# NEW: anti-bully per-day stone count helpers
async def get_stone_count_today(gid: int, attacker: int, target: int, date: str) -> int:
    async with bot.db.execute("""SELECT count FROM stone_daily
                                 WHERE guild_id=? AND attacker_id=? AND target_id=? AND date=?""",
                              (gid, attacker, target, date)) as cur:
        row = await cur.fetchone()
    return row[0] if row else 0

async def inc_stone_count_today(gid: int, attacker: int, target: int, date: str, delta: int):
    await bot.db.execute("""
      INSERT INTO stone_daily(guild_id, attacker_id, target_id, date, count)
      VALUES(?,?,?,?,?)
      ON CONFLICT(guild_id, attacker_id, target_id, date)
      DO UPDATE SET count = stone_daily.count + excluded.count
    """, (gid, attacker, target, date, delta))
    await bot.db.commit()

# NEW: Casino pot helpers
CASINO_BASE_POT = 5  # updated starting/reset pot
async def get_casino_pot(gid: int) -> int:
    async with bot.db.execute("SELECT pot FROM casino_pot WHERE guild_id=?", (gid,)) as cur:
        row = await cur.fetchone()
    if row:
        return row[0]
    # init row at base 5
    await bot.db.execute("INSERT OR IGNORE INTO casino_pot(guild_id, pot) VALUES(?, ?)", (gid, CASINO_BASE_POT))
    await bot.db.commit()
    return CASINO_BASE_POT

async def set_casino_pot(gid: int, pot_val: int):
    await bot.db.execute("""
      INSERT INTO casino_pot(guild_id, pot) VALUES(?,?)
      ON CONFLICT(guild_id) DO UPDATE SET pot=excluded.pot
    """, (gid, pot_val))
    await bot.db.commit()

# ------- streak helpers -------
async def _get_streak(gid: int, uid: int):
    async with bot.db.execute("SELECT last_date,cur,best FROM solo_streak WHERE guild_id=? AND user_id=?", (gid, uid)) as cur:
        row = await cur.fetchone()
    if not row:
        return None, 0, 0
    return row[0], row[1], row[2]

async def update_streak_on_play(gid: int, uid: int, today_str: str):
    last_date, cur, best = await _get_streak(gid, uid)
    if last_date == today_str:
        return  # already counted today
    if last_date is None:
        cur = 1
    else:
        try:
            last = dt_date.fromisoformat(last_date)
            today = dt_date.fromisoformat(today_str)
            delta = (today - last).days
            if delta == 1:
                cur = cur + 1
            else:
                cur = 1
        except Exception:
            cur = 1
    best = max(best, cur)
    await bot.db.execute("""
      INSERT INTO solo_streak(guild_id,user_id,last_date,cur,best) VALUES(?,?,?,?,?)
      ON CONFLICT(guild_id,user_id) DO UPDATE SET
        last_date=excluded.last_date,
        cur=excluded.cur,
        best=CASE WHEN excluded.cur>solo_streak.best THEN excluded.cur ELSE solo_streak.best END
    """, (gid, uid, today_str, cur, best))
    await bot.db.commit()

# ------- stats helpers (for leaderboards) -------
STAT_FIELDS = {"bounties_won", "stones_thrown", "stoned_received", "solo_fails", "snipes", "sniped"}

async def inc_stat(gid: int, uid: int, field: str, delta: int = 1):
    """
    Robust UPSERT that applies the delta on first insert AND on updates.
    We insert a row with zeros except the target field set to `delta`,
    then on conflict we add the `excluded` values to existing stats.
    """
    if field not in STAT_FIELDS:
        raise ValueError(f"invalid stat field: {field}")

    vals = {
        "bounties_won": 0,
        "stones_thrown": 0,
        "stoned_received": 0,
        "solo_fails": 0,
        "snipes": 0,
        "sniped": 0,
    }
    vals[field] = int(delta)

    await bot.db.execute("""
        INSERT INTO stats(
            guild_id, user_id,
            bounties_won, stones_thrown, stoned_received, solo_fails, snipes, sniped
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(guild_id, user_id) DO UPDATE SET
            bounties_won    = stats.bounties_won    + excluded.bounties_won,
            stones_thrown   = stats.stones_thrown   + excluded.stones_thrown,
            stoned_received = stats.stoned_received + excluded.stoned_received,
            solo_fails      = stats.solo_fails      + excluded.solo_fails,
            snipes          = stats.snipes          + excluded.snipes,
            sniped          = stats.sniped          + excluded.sniped
    """, (
        gid, uid,
        vals["bounties_won"],
        vals["stones_thrown"],
        vals["stoned_received"],
        vals["solo_fails"],
        vals["snipes"],
        vals["sniped"],
    ))
    await bot.db.commit()




async def get_top_stats(gid: int, field: str, limit: int = 10):
    if field not in STAT_FIELDS:
        raise ValueError(f"invalid stat field: {field}")
    async with bot.db.execute(f"""
        SELECT user_id, {field} FROM stats
        WHERE guild_id=?
        ORDER BY {field} DESC, user_id ASC
        LIMIT ?
    """, (gid, limit)) as cur:
        return await cur.fetchall()

async def get_my_stat(gid: int, uid: int, field: str) -> int:
    if field not in STAT_FIELDS:
        raise ValueError(f"invalid stat field: {field}")
    async with bot.db.execute(f"SELECT {field} FROM stats WHERE guild_id=? AND user_id=?", (gid, uid)) as cur:
        row = await cur.fetchone()
    return int(row[0]) if row else 0
