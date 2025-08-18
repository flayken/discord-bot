# Wordle World bot (UK reset + anti-bully + casino/Word Pot)
# Python 3.12; deps: discord.py==2.4.0, python-dotenv==1.0.1, requests==2.32.3, aiosqlite==0.20.0

import os, json, random, pathlib, logging, requests, re, asyncio, time
from typing import Optional, Tuple
from datetime import datetime, timezone, date as dt_date
from zoneinfo import ZoneInfo  # NEW: UK local-time resets

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv
import aiosqlite

# -------------------- basic setup --------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("wordle")

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

DB_PATH = os.getenv("DB_PATH", "wordle_world.db")
START_BONUS = int(os.getenv("START_BONUS", "5"))

# Named item emoji ENV (custom emoji in your emoji server)
EMO_BADGE_NAME   = os.getenv("WW_BADGE_NAME",   "ww_badge")
EMO_CHICKEN_NAME = os.getenv("WW_CHICKEN_NAME", "ww_chicken")
EMO_SHEKEL_NAME  = os.getenv("WW_SHEKEL_NAME",  "ww_shekel")
EMO_STONE_NAME   = os.getenv("WW_STONE_NAME",   "ww_stone")
EMO_SNIPER_NAME  = os.getenv("WW_SNIPER_NAME",  "ww_sniper")
EMO_BOUNTY_NAME  = os.getenv("WW_BOUNTY_NAME",  "ww_bounty")

INTENTS = discord.Intents.default()
INTENTS.message_content = True
bot = discord.Client(intents=INTENTS)
tree = app_commands.CommandTree(bot)


# Ambient shekel drop config (per-guild, one roll per 20-min slot)
SHEKEL_DROP_CHANCE = float(os.getenv("SHEKEL_DROP_CHANCE", "0.10"))  # 10% chance
SHEKEL_DROP_MIN    = int(os.getenv("SHEKEL_DROP_MIN", "1"))
SHEKEL_DROP_MAX    = int(os.getenv("SHEKEL_DROP_MAX", "5"))
if SHEKEL_DROP_MIN > SHEKEL_DROP_MAX:
    SHEKEL_DROP_MIN, SHEKEL_DROP_MAX = SHEKEL_DROP_MAX, SHEKEL_DROP_MIN


# -------------------- time utils --------------------
# Keep GMT seconds and hour-buckets for hourly bounty logic.
def gmt_now_s() -> int:
    return int(time.time())

def current_hour_index_gmt() -> int:
    return gmt_now_s() // 3600

# NEW: UK-local (Europe/London) day string for all "daily reset" features
UK_TZ = ZoneInfo("Europe/London")
def uk_today_str() -> str:
    # Date string in Europe/London (00:00 UK time, handles BST/GMT automatically)
    return datetime.now(UK_TZ).date().isoformat()

def _current_20m_slot() -> int:
    """Integer index for 20-minute windows. Used to coordinate one roll per slot per guild."""
    return int(time.time() // 1200)


# -------------------- env tiers --------------------
def env_default_tiers():
    raw = os.getenv("DEFAULT_TIERS_JSON")
    if not raw:
        return []
    try:
        arr = json.loads(raw)
        out=[]
        for name, min_bal in arr:
            out.append((str(name), int(min_bal)))
        out.sort(key=lambda x: x[1])
        return out
    except Exception as e:
        log.warning(f"DEFAULT_TIERS_JSON parse failed: {e}")
        return []

DEFAULT_TIERS = env_default_tiers()
WORLDLER_ROLE_NAME = "Worldler"
BOUNTY_ROLE_NAME   = "Bounty Hunter"

# -------------------- emoji helpers --------------------

def EMO_GATE_SCROLL(tier: int) -> str:
    """Return the custom gate scroll emoji for a given tier (1‑3)."""
    return get_named_emoji(f"ww_t{tier}_gate_scroll")

EMO_BADGE   = lambda: "<:ww_badge:1404182337230602343>"
EMO_CHICKEN = lambda: "<:ww_chicken:1406752722002120704>"
EMO_SHEKEL  = lambda: "<:ww_shekel:1406746588831027353>"
EMO_STONE   = lambda: "<:ww_stone:1406746605842862100>"
EMO_SNIPER  = lambda: "<:ww_sniper:1406747636429754508>"
EMO_BOUNTY  = lambda: "<:ww_bounty:1406783901032251492>"
EMO_DUNGEON = lambda: get_named_emoji("ww_gate")  # custom WW gate emoji


def get_named_emoji(name: str) -> str:
    e = discord.utils.find(lambda em: em.name.lower() == name.lower(), bot.emojis)
    return str(e) if e else ""

EMO_BADGE   = lambda: "<:ww_badge:1404182337230602343>"
EMO_CHICKEN = lambda: "<:ww_chicken:1406752722002120704>"
EMO_SHEKEL  = lambda: "<:ww_shekel:1406746588831027353>"
EMO_STONE   = lambda: "<:ww_stone:1406746605842862100>"
EMO_SNIPER  = lambda: "<:ww_sniper:1406747636429754508>"
EMO_BOUNTY  = lambda: "<:ww_bounty:1406783901032251492>"
EMO_DUNGEON  = lambda: "<:ww_t3_gate_scroll:1406748502649733190>"

# -------------------- deps sanity --------------------
def log_deps_health():
    from importlib.metadata import version, PackageNotFoundError
    for pkg in ("discord.py","python-dotenv","requests","aiosqlite"):
        try:
            v = version(pkg)
            log.info(f"dep {pkg:<12}: OK  {v}")
        except PackageNotFoundError:
            log.warning(f"dep {pkg:<12}: MISSING")

# -------------------- word lists (NYT + British guesses) --------------------
ANS_LOCAL = pathlib.Path("answers_nyt.txt")
ALLOWED_LOCAL = pathlib.Path("allowed_nyt.txt")
ANS_URL = "https://raw.githubusercontent.com/LaurentLessard/wordlesolver/main/solutions_nyt.txt"
ALLOWED_URL = "https://raw.githubusercontent.com/LaurentLessard/wordlesolver/main/nonsolutions_nyt.txt"

def _download(url: str, path: pathlib.Path):
    r = requests.get(url, timeout=20); r.raise_for_status()
    path.write_text(r.text, encoding="utf-8")

def _parse_words(text: str) -> list[str]:
    t = text.strip()
    if t.startswith("[") and t.endswith("]"):
        try:
            arr = json.loads(t)
            return [w.lower() for w in arr if isinstance(w, str) and len(w)==5 and w.isalpha()]
        except Exception:
            pass
    lines = [w.strip().lower() for w in t.replace("\r\n","\n").split("\n") if w.strip()]
    if len(lines) > 1:
        return [w for w in lines if len(w)==5 and w.isalpha()]
    words = re.findall(r"[A-Za-z]{5}", t)
    out, seen = [], set()
    for w in (w.lower() for w in words):
        if w not in seen and len(w)==5 and w.isalpha():
            seen.add(w); out.append(w)
    return out

def ensure_word_lists():
    if not ANS_LOCAL.exists(): _download(ANS_URL, ANS_LOCAL)
    if not ALLOWED_LOCAL.exists(): _download(ALLOWED_URL, ALLOWED_LOCAL)
    answers = _parse_words(ANS_LOCAL.read_text(encoding="utf-8"))
    allowed = _parse_words(ALLOWED_LOCAL.read_text(encoding="utf-8"))
    return answers, allowed

# British dictionary (extra guesses)
BRITISH_LOCAL = pathlib.Path("british_words.txt")
BRITISH_WORDS_URL = os.getenv(
    "BRITISH_WORDS_URL",
    "https://raw.githubusercontent.com/SublimeText/Dictionaries/master/English%20(British).dic"
)
ALLOWED_EXTRA_LOCAL = pathlib.Path("allowed_extra.txt")
BRITISH_5_BUILTIN = {"fibre","litre","metre","mould","sabre","odour","enrol","storey","tyres"}

def _safe_download_to(path: pathlib.Path, url: str):
    try:
        if url:
            r = requests.get(url, timeout=20); r.raise_for_status()
            path.write_text(r.text, encoding="utf-8")
    except Exception as e:
        log.warning(f"[dict] British list download failed: {e}")

def ensure_british_words() -> set[str]:
    if not BRITISH_LOCAL.exists():
        _safe_download_to(BRITISH_LOCAL, BRITISH_WORDS_URL)
    extra = set()
    if BRITISH_LOCAL.exists():
        extra |= set(_parse_words(BRITISH_LOCAL.read_text(encoding="utf-8")))
    if ALLOWED_EXTRA_LOCAL.exists():
        extra |= set(_parse_words(ALLOWED_EXTRA_LOCAL.read_text(encoding="utf-8")))
    extra |= {w for w in BRITISH_5_BUILTIN if len(w)==5 and w.isalpha()}
    return extra

def _generate_us_variants(word: str) -> set[str]:
    w = word.lower()
    cands = set()
    if w.endswith("re"):  cands.add(w[:-2] + "er")
    if "our" in w:        cands.add(w.replace("our", "or"))
    if "ae" in w:         cands.add(w.replace("ae", "e"))
    if "oe" in w:         cands.add(w.replace("oe", "e"))
    if "ll" in w:         cands.add(w.replace("ll", "l"))
    if w.endswith("ise"): cands.add(w[:-3] + "ize")
    if w.endswith("yse"): cands.add(w[:-3] + "yze")
    return {c for c in cands if len(c) == 5 and c.isalpha()}

ANSWERS, ALLOWED = ensure_word_lists()
BRITISH_ALLOWED = ensure_british_words()
VALID_BASE = set(ANSWERS) | set(ALLOWED)
EXTRA_ALLOWED = {w for w in BRITISH_ALLOWED if w not in VALID_BASE}
VALID_GUESSES = VALID_BASE | EXTRA_ALLOWED

log.info(
    "word lists: answers=%d, allowed=%d, extras=%d, valid=%d, cigar? %s, fibre? %s",
    len(ANSWERS), len(ALLOWED), len(EXTRA_ALLOWED), len(VALID_GUESSES),
    "yes" if "cigar" in VALID_GUESSES else "no",
    "yes" if "fibre" in VALID_GUESSES else "no",
)

def is_valid_guess(word: str) -> bool:
    if word in VALID_GUESSES:
        return True
    for cand in _generate_us_variants(word):
        if cand in VALID_BASE:
            return True
    return False

# -------------------- tiles --------------------
FALLBACK_COLOR = {"green": "🟩", "yellow": "🟨", "gray": "⬛", "red": "🟥"}
emoji_lookup = {"green": {}, "yellow": {}, "gray": {}, "red": {}}
BLANK_TILE: Optional[str] = None

def build_emoji_lookup():
    global emoji_lookup, BLANK_TILE
    emoji_lookup = {"green": {}, "yellow": {}, "gray": {}, "red": {}}
    BLANK_TILE = None

    # wl_g_*, wl_y_*, wl_x_* (gray), wl_r_* (RED for "Not used")
    cmap = {"g": "green", "y": "yellow", "x": "gray", "r": "red"}

    for e in bot.emojis:
        n = (e.name or "").lower()

        # blank tile (optional)
        if n.startswith("wl_blank") and BLANK_TILE is None:
            BLANK_TILE = str(e)
            continue

        if not n.startswith("wl_"):
            continue

        parts = n.split("_", 2)
        if len(parts) != 3:
            continue
        _, c, ch = parts
        if c in cmap and len(ch) == 1 and ch.isalpha():
            emoji_lookup[cmap[c]][ch] = str(e)

    log.info(
        "[emoji] loaded tiles | g=%d y=%d x=%d r=%d | blank=%s",
        len(emoji_lookup["green"]),
        len(emoji_lookup["yellow"]),
        len(emoji_lookup["gray"]),
        len(emoji_lookup["red"]),
        "yes" if BLANK_TILE else "no",
    )

def render_tile(letter: str, color: str) -> str:
    em = emoji_lookup.get(color, {}).get(letter.lower())
    return em if em else f"{FALLBACK_COLOR[color]}{letter.upper()}"


# -------------------- scoring/render --------------------
def score_guess(guess: str, answer: str):
    result = ["gray"]*5
    counts = {}
    for c in answer: counts[c] = counts.get(c,0) + 1
    for i, ch in enumerate(guess):
        if ch == answer[i]:
            result[i] = "green"; counts[ch] -= 1
    for i, ch in enumerate(guess):
        if result[i] == "gray" and counts.get(ch,0) > 0:
            result[i] = "yellow"; counts[ch] -= 1
    return result

def render_row(word: str, colors: list[str]) -> str:
    return "".join(render_tile(ch, col) for ch, col in zip(word, colors))

def render_board(guesses: list[dict], total_rows=5) -> str:
    rows = [render_row(g["word"], g["colors"]) for g in guesses]
    blank = BLANK_TILE if BLANK_TILE else "⬛"
    while len(rows) < total_rows:
        rows.append(blank*5)
    return "\n".join(rows)

# Solo payout 1..5 -> 5..1
def payout_for_attempt(n: int) -> int:
    return {1:5, 2:4, 3:3, 4:2, 5:1}.get(n, 0)

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



##-------------------------------------HELPERS----------------------------------


# -------------------- Dungeon UI (paged) --------------------

# 2 short-paragraph lore blurbs per tier (ancient wizards & lost knowledge theme)
_DUNGEON_LORE = {
    3: (
        "Past a sealed lectern is a staircase that wasn’t there until you needed it. "
        "Shelves drift like constellations; books never written sit beside those never allowed. "
        "Invisible librarians reshelve fate.",

        "The Arch-Archivist hid the **Index of Forgotten Things** here. Every answer has teeth. "
        "You won’t leave unchanged—but if you are brave, you will leave **true**."
    ),
    2: (
        "Beneath the archive is the **Scriptorium of Errata** where apprentices rewrote history "
        "in erasable ink. The floor is a palimpsest of mistakes that learned to whisper.",

        "Wards flicker; glyphs bargain. The **Corrector’s Quill** grants power but demands precision. "
        "Those who rush their answers feed the quill more than ink."
    ),
    1: (
        "At the root lies the **Redacted Sanctum**, where the First Wizard hid knowledge from even themself. "
        "Light arrives late; echoes arrive early. Letters arrange themselves if you stare long enough.",

        "A bell tolls when truth is near. Toll it too often and the room tolls back. "
        "Answers here are heavy and must be lifted carefully."
    ),
}

def _tier_stats(tier: int) -> tuple[int, int]:
    """Returns (tries_per_round, reward_multiplier) using your code's rules:
       T3=5×1, T2=4×2, T1=3×3."""
    tries = 5 if tier == 3 else 4 if tier == 2 else 3
    mult  = 1 if tier == 3 else 2 if tier == 2 else 3
    return tries, mult

def _loot_lines_for_tier(tier: int) -> list[str]:
    """Matches your real loot logic:
       • Every solve: +1 Stone
       • T3: 10% chance → +1 Tier 2 Ticket
       • T2: 10% chance → +1 Tier 1 Ticket
       • T1: no ticket drops
    """
    lines = [f"• +1 {EMO_STONE()} on every solved word"]
    if tier == 3:
        lines.append("• 10% chance per solve → +1 Tier 2 Ticket")
    elif tier == 2:
        lines.append("• 10% chance per solve → +1 Tier 1 Ticket")
    return lines

async def _ticket_count_for(gid: int, uid: int, tier: int) -> int:
    if tier == 3:
        return await get_dungeon_tickets_t3(gid, uid)
    elif tier == 2:
        return await get_dungeon_tickets_t2(gid, uid)
    else:
        return await get_dungeon_tickets_t1(gid, uid)

async def _build_dungeon_embed(gid: int, uid: int, page: str) -> discord.Embed:
    """
    page ∈ {"t1","t2","t3","rules"}
    """
    shek, stone = EMO_SHEKEL(), EMO_STONE()
    if page == "rules":
        desc = (
            "Co-op Wordle, but perilous:\n"
            "• Solve a word to add shekels to the **pool** (amount depends on attempt & tier multiplier).\n"
            "• After each solve, the **owner** chooses: **Continue** (⏩) or **Cash Out** ().\n"
            "• If a round **fails**, the pool is **halved (rounded up)** and paid, then the dungeon closes.\n"
            "• Max tries per round depend on tier. T1 is hardest but pays the most per solve.\n"
            "• Tickets are consumed on entry. T3 tickets are bought in /shop; T2/T1 drop in lower floors."
        )
        flds = [
            ("Tries & Multipliers",
             "• Tier 3: 5 tries · ×1 rewards\n"
             "• Tier 2: 4 tries · ×2 rewards\n"
             "• Tier 1: 3 tries · ×3 rewards",
             False),
            ("General Tips",
             "• Bring friends; participants share the pool.\n"
             "• Precision beats speed—wrong paths burn attempts.\n"
             "• Cash out if the hint matrix looks grim.",
             False),
        ]
        return make_panel(
            title=f"{EMO_DUNGEON()} Dungeon — Rules",
            description=desc,
            fields=flds
        )

    tier = 3 if page == "t3" else 2 if page == "t2" else 1
    tries, mult = _tier_stats(tier)
    lore1, lore2 = _DUNGEON_LORE[tier]
    tix = await _ticket_count_for(gid, uid, tier)

    desc = f"**Tier {tier}** — {['','The Scriptorium of Errata','The Unbound Stacks'][tier-1] if tier!=1 else 'The Redacted Sanctum'}\n\n{lore1}\n\n{lore2}"
    loot = "\n".join(_loot_lines_for_tier(tier))

    flds = [
        ("Rewards", f"Tier {tier} multiplier ×**{mult}** · **{tries}** tries per round", False),
        ("Loot Table", loot, False),
        ("To enter", f"Use **/worldle_dungeon tier:Tier {tier}** — or click the green button below.", False),
    ]
    return make_panel(
        title=f"{EMO_DUNGEON()} Dungeon — Tier {tier}",
        description=desc,
        fields=flds
    )


class DungeonView(discord.ui.View):
    """4-page dungeon guide; Enter button calls /worldle_dungeon and live-updates ticket count.
       On the Rules page the Enter button is removed entirely.
    """
    def __init__(self, inter: discord.Interaction, start_tier: int = 3):
        super().__init__(timeout=300)
        self.inter = inter
        self.guild_id = inter.guild.id
        self.user_id = inter.user.id
        self.page = f"t{start_tier}"  # 't1' | 't2' | 't3' | 'rules'
        self.message: discord.Message | None = None  # set by the command after sending

        # Attach custom gate scroll emojis to navigation + action buttons
        for t, btn in ((1, self.t1_button), (2, self.t2_button), (3, self.t3_button)):
            emo = EMO_GATE_SCROLL(t)
            if emo:
                btn.emoji = emo
        emo = EMO_GATE_SCROLL(start_tier)
        if emo:
            self.enter_button.emoji = emo

    # ---------- internal helpers ----------
    def _current_tier(self) -> int:
        if self.page == "t3":
            return 3
        if self.page == "t2":
            return 2
        return 1

    def _has_enter_button(self) -> bool:
        # Is the green button currently attached to the view?
        return any(isinstance(child, discord.ui.Button) and child is self.enter_button for child in self.children)

    async def refresh_page(self, i: discord.Interaction | None = None):
        """Rebuild embed + sync presence/label of the Enter button."""
        emb = await _build_dungeon_embed(self.guild_id, self.user_id, self.page)

        # Manage presence of Enter button (remove on Rules; ensure present otherwise)
        if self.page == "rules":
            if self._has_enter_button():
                self.remove_item(self.enter_button)
        else:
            if not self._has_enter_button():
                # Re-attach the same Button instance so we don't lose its callback
                self.add_item(self.enter_button)

            # Update label with current ticket count and disable if 0
            t = self._current_tier()
            count = await _ticket_count_for(self.guild_id, self.user_id, t)
            self.enter_button.label = f"Enter Dungeon — T{t} ({count} tickets)"
            self.enter_button.disabled = (count <= 0)
            emo = EMO_GATE_SCROLL(t)
            if emo:
                self.enter_button.emoji = emo

        # Push UI
        if i is not None:
            try:
                await i.response.edit_message(embed=emb, view=self)
            except discord.InteractionResponded:
                if self.message:
                    await self.message.edit(embed=emb, view=self)
        elif self.message is not None:
            await self.message.edit(embed=emb, view=self)

    # ---------- navigation ----------
    @discord.ui.button(label="Tier 1", style=discord.ButtonStyle.secondary)
    async def t1_button(self, i: discord.Interaction, _btn: discord.ui.Button):
        self.page = "t1"
        await self.refresh_page(i)

    @discord.ui.button(label="Tier 2", style=discord.ButtonStyle.secondary)
    async def t2_button(self, i: discord.Interaction, _btn: discord.ui.Button):
        self.page = "t2"
        await self.refresh_page(i)

    @discord.ui.button(label="Tier 3", style=discord.ButtonStyle.secondary)
    async def t3_button(self, i: discord.Interaction, _btn: discord.ui.Button):
        self.page = "t3"
        await self.refresh_page(i)

    @discord.ui.button(label="Rules", style=discord.ButtonStyle.secondary)
    async def rules_button(self, i: discord.Interaction, _btn: discord.ui.Button):
        self.page = "rules"
        await self.refresh_page(i)

    # ---------- primary action ----------
    @discord.ui.button(label="Enter Dungeon", style=discord.ButtonStyle.success, row=1)
    async def enter_button(self, i: discord.Interaction, _btn: discord.ui.Button):
        """Invoke your /worldle_dungeon slash command, then refresh the ticket count."""
        # Determine tier from current page (if on rules we shouldn't have this button at all)
        t = self._current_tier()

        # Look up the app command on your global tree and call its callback
        cmd = tree.get_command("worldle_dungeon")  # keep this name in sync with your slash command
        if cmd is None:
            return await i.response.send_message("Dungeon command not found.", ephemeral=True)

        choice = app_commands.Choice(name=f"Tier {t}", value=t)

        binding = getattr(cmd, "binding", None)
        if binding is not None:
            await cmd.callback(binding, i, choice)
        else:
            await cmd.callback(i, choice)

        # After the command runs, tickets might be consumed—refresh the label in-place.
        # Use the stored message to avoid response state conflicts.
        # A tiny yield lets anything deferred finish updating your storage.
        await asyncio.sleep(0)
        await self.refresh_page()  # edits self.message with new ticket count










# --- Dailies helpers (UK reset) ---
async def get_solo_wordles_left(guild_id: int, user_id: int) -> int:
    """How many solo games remain today for this user (max 5/day, UK-local reset)."""
    today = uk_today_str()
    plays = await get_solo_plays_today(guild_id, user_id, today)
    return max(0, 5 - int(plays))

async def has_prayed_today(guild_id: int, user_id: int) -> bool:
    """True if user already did /pray today (UK-local reset)."""
    today = uk_today_str()
    last_pray, _ = await _get_cd(guild_id, user_id)
    return (last_pray == today)

async def has_begged_today(guild_id: int, user_id: int) -> bool:
    """True if user already did /beg today (UK-local reset)."""
    today = uk_today_str()
    _, last_beg = await _get_cd(guild_id, user_id)
    return (last_beg == today)


# --- Word Pot total (use casino pot) ---
async def get_word_pot_total(guild_id: int) -> int:
    """
    Drop-in replacement: report the casino Word Pot amount.
    """
    try:
        return await get_casino_pot(guild_id)
    except Exception:
        return 0



async def _build_shop_embed(gid: int, uid: int) -> discord.Embed:
    bal = await get_balance(gid, uid)
    shek = EMO_SHEKEL()
    lines = [
        "🛒 **Shop** — buy with the buttons, or sell with the **💸 Sell** button (or `/sell`).",
        "",
        f"**Your balance:** **{bal} {shek}**",
        "",
    ]
    for key in SHOP_ORDER:
        item = SHOP_ITEMS[key]
        price = item["price"]
        lines.append(
            f"• **{item['label']}** — {price} {shek}{'' if price==1 else 's'}\n  _{item['desc']}_"
        )
    lines.append("\nTip: For bulk buying via slash command, use `/buy item:{name} amount:{n}`.")
    return make_panel("Shop", "\n".join(lines), icon="🛒")


async def _build_sell_embed(gid: int, uid: int, owned: list[dict]) -> discord.Embed:
    bal = await get_balance(gid, uid)
    shek = EMO_SHEKEL()

    if not owned:
        desc = f"**Your balance:** **{bal} {shek}**\n\nYou currently have **nothing** you can sell."
        return make_panel("Sell Items", desc, icon="🛍️")

    lines = [f"**Your balance:** **{bal} {shek}**", "", "You can sell:"]
    for it in owned:
        lines.append(
            f"• {it['emoji']} **{it['label']}** — you own **{it['count']}** · sell price **{it['price_each']} {shek}** each"
        )

    return make_panel(
        title="Sell Items",
        description="Pick an item from the selector below:",
        fields=[("Details", "\n".join(lines), False)],
        icon="🛍️",
    )


async def _get_owned_sellables(gid: int, uid: int) -> list[dict]:
    """
    Return a list of items the user can sell, each as:
      { key, label, count, price_each, emoji }
    Only include items with count > 0.
    """
    # Pull counts
    stones   = await get_stones(gid, uid)
    chickens = await get_chickens(gid, uid)
    badge    = await get_badge(gid, uid)
    sniper   = await get_sniper(gid, uid)
    t3       = await get_dungeon_tickets_t3(gid, uid)

    catalog = []
    def _add(key: str, count: int, label: str, price_each: int, emoji: str):
        if count and count > 0:
            catalog.append({
                "key": key,
                "label": label,
                "count": int(count),
                "price_each": int(price_each),
                "emoji": emoji,
            })

    _add("stone",     stones,   "Stone",                   SHOP_ITEMS["stone"]["price"],     EMO_STONE())
    _add("badge",     badge,    "Bounty Hunter Badge",     SHOP_ITEMS["badge"]["price"],     EMO_BADGE())
    _add("chicken",   chickens, "Fried Chicken",           SHOP_ITEMS["chicken"]["price"],   EMO_CHICKEN())
    _add("sniper",    sniper,   "Sniper",                  SHOP_ITEMS["sniper"]["price"],    EMO_SNIPER())
    _add("ticket_t3", t3,       "Dungeon Ticket (Tier 3)", SHOP_ITEMS["ticket_t3"]["price"], EMO_DUNGEON())
    return catalog


async def _shop_perform_sell(i: discord.Interaction, key: str, qty: int):
    """
    Performs a SELL and replies PUBLICLY (non-ephemeral) on both success and errors.
    Mirrors the logic of the /sell command, but as a reusable helper for UI.
    """
    async def _send(msg: str):
        return await send_boxed(i, "Shop — Sell", msg, icon="🛍️", ephemeral=False)

    if not i.guild or not i.channel:
        return await _send("Run this in a server.")

    gid, uid, cid = i.guild.id, i.user.id, i.channel.id
    key = str(key).strip().lower()

    if qty <= 0:
        return await _send("Quantity must be at least 1.")

    # Stones
    if key == "stone":
        have = await get_stones(gid, uid)
        if have < qty:
            return await _send("You don't have that many stones to sell.")
        await change_stones(gid, uid, -qty)
        refund = SHOP_ITEMS["stone"]["price"] * qty
        await change_balance(gid, uid, refund, announce_channel_id=cid)
        bal = await get_balance(gid, uid)
        return await _send(f"Sold **{qty}× {EMO_STONE()} Stone** for **{refund} {EMO_SHEKEL()}**. New balance: **{bal}**.")

    # Badge (one-time)
    if key == "badge":
        have = await get_badge(gid, uid)
        if have < 1 or qty != 1:
            return await _send("You can only sell **one** Bounty Hunter Badge if you own it.")
        await set_badge(gid, uid, 0)
        refund = SHOP_ITEMS["badge"]["price"]
        await change_balance(gid, uid, refund, announce_channel_id=cid)
        # Remove bounty role if present
        try:
            rid = (await get_cfg(gid))["bounty_role_id"]
            role = i.guild.get_role(rid) if rid else None
            member = i.guild.get_member(uid) or await i.guild.fetch_member(uid)
            if role and member and bot_can_manage_role(i.guild, role):
                await member.remove_roles(role, reason="Sold Bounty Hunter Badge")
        except Exception as e:
            log.warning(f"sell badge role removal failed: {e}")
        bal = await get_balance(gid, uid)
        return await _send(f"Sold **{EMO_BADGE()} Bounty Hunter Badge** for **{refund} {EMO_SHEKEL()}**. New balance: **{bal}**.")

    # Chicken
    if key == "chicken":
        have = await get_chickens(gid, uid)
        if have < qty:
            return await _send("You don't have that many fried chicken to sell.")
        await change_chickens(gid, uid, -qty)
        refund = SHOP_ITEMS["chicken"]["price"] * qty
        await change_balance(gid, uid, refund, announce_channel_id=cid)
        bal = await get_balance(gid, uid)
        return await _send(f"Sold **{qty}× {EMO_CHICKEN()} Fried Chicken** for **{refund} {EMO_SHEKEL()}**. New balance: **{bal}**.")

    # Sniper (one-time)
    if key == "sniper":
        have = await get_sniper(gid, uid)
        if have < 1 or qty != 1:
            return await _send("You can only sell **one** Sniper if you own it.")
        await set_sniper(gid, uid, 0)
        refund = SHOP_ITEMS["sniper"]["price"]
        await change_balance(gid, uid, refund, announce_channel_id=cid)
        bal = await get_balance(gid, uid)
        return await _send(f"Sold **{EMO_SNIPER()} Sniper** for **{refund} {EMO_SHEKEL()}**. New balance: **{bal}**.")

    # Tier-3 Ticket
    if key == "ticket_t3":
        have = await get_dungeon_tickets_t3(gid, uid)
        if have < qty:
            return await _send("You don't have that many Tier-3 tickets to sell.")
        await change_dungeon_tickets_t3(gid, uid, -qty)
        refund = SHOP_ITEMS["ticket_t3"]["price"] * qty
        await change_balance(gid, uid, refund, announce_channel_id=cid)
        bal = await get_balance(gid, uid)
        return await _send(f"Sold **{qty}× {EMO_DUNGEON()} Tier-3 Ticket** for **{refund} {EMO_SHEKEL()}**. New balance: **{bal}**.")

    return await _send("Can't sell that item here.")



# -------------------- DUNGEON emojis/helpers --------------------
EMO_DUNGEON_NAME = os.getenv("WW_DUNGEON_NAME", "ww_dungeon")

def EMO_DUNGEON() -> str:
    e = discord.utils.find(lambda em: em.name.lower() == EMO_DUNGEON_NAME.lower(), bot.emojis)
    return str(e) if e else "🌀"  # fallback

def _dungeon_join_emoji_matches(emoji: discord.PartialEmoji) -> bool:
    if emoji.is_unicode_emoji():
        return emoji.name == "🌀"
    return (emoji.name or "").lower() == EMO_DUNGEON_NAME.lower()

def _lock_emoji_matches(emoji: discord.PartialEmoji) -> bool:
    return emoji.is_unicode_emoji() and emoji.name == "🔒"

def _continue_emoji_matches(emoji: discord.PartialEmoji) -> bool:
    return emoji.is_unicode_emoji() and emoji.name == "⏩"

def _cashout_emoji_matches(emoji: discord.PartialEmoji) -> bool:
    return emoji.is_unicode_emoji() and emoji.name == "💰"


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

# ---- SHOP helpers (buttons + slash share this) ----
async def _shop_perform_buy(i: discord.Interaction, key: str, qty: int, *, _from_modal: bool = False):
    """
    Performs the shop purchase and replies:
      • PUBLIC message on success
      • Ephemeral message on validation / balance errors

    Works whether called from a slash command, a button, or a modal (deferred).
    """

    async def _send(msg: str, *, ephemeral: bool = False):
        return await send_boxed(
            i,
            title="Shop",
            description=msg,
            icon="🛒",
            ephemeral=ephemeral,
        )

    if not i.guild or not i.channel:
        return await _send("Run this in a server.", ephemeral=True)

    gid, cid, uid = i.guild.id, i.channel.id, i.user.id

    # Canonical catalog for buy prices (must match your SHOP_ITEMS)
    CATALOG = {
        "stone":     {"label": "Stone",                    "price": 1},
        "badge":     {"label": "Bounty Hunter Badge",      "price": 5},
        "chicken":   {"label": "Fried Chicken",            "price": 2},
        "sniper":    {"label": "Sniper",                   "price": 100},
        "ticket_t3": {"label": "Dungeon Ticket (Tier 3)",  "price": 5},
    }
    # Accept a few friendly aliases
    ALIASES = {
        "stones": "stone",
        "bounty_hunter_badge": "badge",
        "fried_chicken": "chicken",
        "sniper_rifle": "sniper",
        "tier3": "ticket_t3",
        "tier_3": "ticket_t3",
        "t3": "ticket_t3",
        "t3_ticket": "ticket_t3",
        "dungeon_ticket": "ticket_t3",
        "ticket_t3": "ticket_t3",
    }

    key_norm = str(key).strip().lower().replace(" ", "_")
    key_norm = ALIASES.get(key_norm, key_norm)

    # Validate item & quantity
    item = CATALOG.get(key_norm)
    if not item:
        return await _send(f"Unknown shop item: `{key}`.", ephemeral=True)
    if qty <= 0:
        return await _send("Quantity must be at least 1.", ephemeral=True)
    if qty > 100_000:
        return await _send("That quantity is too large. Try something smaller.", ephemeral=True)

    # One-time items must be bought singly
    if key_norm in ("badge", "sniper") and qty != 1:
        return await _send("That item can only be bought one at a time.", ephemeral=True)

    price = int(item["price"])
    cost = price * qty

    # Economy glue → your DB helpers
    async def _get_balance(uid_: int) -> int:
        return await get_balance(gid, uid_)

    async def _add_balance(uid_: int, delta: int) -> None:
        await change_balance(gid, uid_, delta, announce_channel_id=cid)

    async def _inv_add(uid_: int, item_key: str, amount: int) -> None:
        if item_key == "stone":
            await change_stones(gid, uid_, amount)
        elif item_key == "chicken":
            await change_chickens(gid, uid_, amount)
        elif item_key == "badge":
            if await get_badge(gid, uid_) >= 1:
                raise RuntimeError("You already own the Bounty Hunter Badge.")
            await set_badge(gid, uid_, 1)
            # Grant bounty role if we can
            try:
                rid = await ensure_bounty_role(i.guild)
                role = i.guild.get_role(rid) if rid else None
                member = i.guild.get_member(uid_) or await i.guild.fetch_member(uid_)
                if role and member and bot_can_manage_role(i.guild, role):
                    await member.add_roles(role, reason="Bought Bounty Hunter Badge")
            except Exception as e:
                log.warning(f"grant bounty role failed: {e}")
        elif item_key == "sniper":
            if await get_sniper(gid, uid_) >= 1:
                raise RuntimeError("You already own the Sniper.")
            await set_sniper(gid, uid_, 1)
        elif item_key == "ticket_t3":
            await change_dungeon_tickets_t3(gid, uid_, amount)
        else:
            raise RuntimeError(f"Unhandled inventory item: {item_key}")

    balance = await _get_balance(uid)
    if balance < cost:
        short = cost - balance
        return await _send(
            f"Not enough shekels: need **{short}** more (cost **{cost}**, you have **{balance}**).",
            ephemeral=True,
        )

    # Charge and deliver
    try:
        await _add_balance(uid, -cost)
        await _inv_add(uid, key_norm, qty)
    except RuntimeError as e:
        # Refund if inventory op failed for a known reason
        try:
            await _add_balance(uid, +cost)
        except Exception:
            pass
        return await _send(str(e), ephemeral=True)

    new_balance = await _get_balance(uid)
    label = item["label"]

    # PUBLIC success message
    await _send(
        f"**{i.user.mention}** bought **{qty}× {label}** for **{cost}** {EMO_SHEKEL()}. "
        f"New balance: **{new_balance}**.",
        ephemeral=False,
    )






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
    # 🔄 refresh any open /dailies panels in this guild
    try:
        await DailiesView.refresh_pot_label_for_guild(gid)
    except Exception as e:
        log.warning(f"[dailies] pot refresh failed for guild {gid}: {e}")


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

# -------------------- role tiers --------------------
def bot_can_manage_role(guild: discord.Guild, role: discord.Role) -> bool:
    me = guild.me
    return bool(me and me.guild_permissions.manage_roles and role < me.top_role)

async def ensure_worldler_role(guild: discord.Guild) -> int:
    cfg = await get_cfg(guild.id)
    if cfg["worldler_role_id"]:
        role = guild.get_role(cfg["worldler_role_id"])
        if role: return role.id
    role = discord.utils.find(lambda r: r.name.lower()==WORLDLER_ROLE_NAME.lower(), guild.roles)
    if role is None:
        if not guild.me or not guild.me.guild_permissions.manage_roles:
            log.warning(f"[worldler] Missing Manage Roles in guild {guild.id}")
            return 0
        role = await guild.create_role(name=WORLDLER_ROLE_NAME, reason="Wordle World membership role")
    await set_cfg(guild.id, worldler_role_id=role.id)
    return role.id

async def ensure_bounty_role(guild: discord.Guild) -> int:
    cfg = await get_cfg(guild.id)
    if cfg["bounty_role_id"]:
        role = guild.get_role(cfg["bounty_role_id"])
        if role: return role.id
    role = discord.utils.find(lambda r: r.name.lower()==BOUNTY_ROLE_NAME.lower(), guild.roles)
    if role is None:
        if not guild.me or not guild.me.guild_permissions.manage_roles:
            log.warning(f"[bounty-role] Missing Manage Roles in guild {guild.id}")
            return 0
        role = await guild.create_role(name=BOUNTY_ROLE_NAME, reason="Wordle World bounty role")
    await set_cfg(guild.id, bounty_role_id=role.id)
    return role.id

async def is_worldler(guild: discord.Guild, member: discord.abc.User) -> bool:
    cfg = await get_cfg(guild.id)
    rid = cfg["worldler_role_id"]
    if not rid:
        return False
    try:
        m = guild.get_member(member.id) or await guild.fetch_member(member.id)
        return any(r.id == rid for r in m.roles)
    except Exception:
        return False

async def sync_member_role_tiers(guild: discord.Guild, member: discord.Member):
    if not await is_worldler(guild, member):
        return
    async with bot.db.execute("SELECT role_id,min_balance FROM role_tier WHERE guild_id=? ORDER BY min_balance ASC",(guild.id,)) as cur:
        rows = await cur.fetchall()
    if not rows: return
    manageable=[]
    for role_id, min_bal in rows:
        role = guild.get_role(role_id)
        if role and bot_can_manage_role(guild, role):
            manageable.append((role, min_bal))
    if not manageable: return

    async with bot.db.execute("SELECT balance FROM wallet WHERE guild_id=? AND user_id=?", (guild.id, member.id)) as cur:
        rr = await cur.fetchone()
    bal = rr[0] if rr else 0

    want_ids = {r.id for (r, minimum) in manageable if bal >= minimum}
    tier_ids = {r.id for (r, _) in manageable}
    have_ids = {r.id for r in member.roles}

    to_add = [guild.get_role(rid) for rid in (want_ids - have_ids) if guild.get_role(rid)]
    to_remove = [guild.get_role(rid) for rid in ((tier_ids - want_ids) & have_ids) if guild.get_role(rid)]

    if to_add:
        try: await member.add_roles(*to_add, reason="Wordle World tier sync")
        except Exception as e: log.warning(f"add_roles failed: {e}")
    if to_remove:
        try: await member.remove_roles(*to_remove, reason="Wordle World tier sync")
        except Exception as e: log.warning(f"remove_roles failed: {e}")

async def _sync_member_roles_after_balance_change(gid: int, uid: int, channel_id: Optional[int]):
    guild = discord.utils.get(bot.guilds, id=gid)
    if not guild: return
    try:
        member = guild.get_member(uid) or await guild.fetch_member(uid)
    except Exception:
        return
    await sync_member_role_tiers(guild, member)

async def ensure_default_tiers(guild: discord.Guild):
    for idx, (name, min_bal) in enumerate(DEFAULT_TIERS):
        role = discord.utils.find(lambda r: r.name.lower()==name.lower(), guild.roles)
        if role is None:
            if not guild.me or not guild.me.guild_permissions.manage_roles:
                log.warning(f"[tiers] Missing Manage Roles in guild {guild.id}")
                return
            role = await guild.create_role(name=name, reason="Wordle World auto tier")
        await bot.db.execute("""
          INSERT INTO role_tier(guild_id,role_id,min_balance) VALUES(?,?,?)
          ON CONFLICT(guild_id,role_id) DO UPDATE SET min_balance=excluded.min_balance
        """, (guild.id, role.id, int(min_bal)))
    await bot.db.commit()

# -------------------- state --------------------
solo_games: dict[Tuple[int,int,int], dict] = {}     # (gid, cid, uid) -> {answer, guesses[], max, legend, origin_cid}
bounty_games: dict[int, dict] = {}                  # gid -> {answer, channel_id, started_at}
pending_bounties: dict[int, dict] = {}              # gid -> {message_id, channel_id, users:set, hour_idx}
duels: dict[int, dict] = {}                         # duel_id -> data
_next_duel_id = 1
solo_channels: dict[Tuple[int,int], int] = {}       # (gid, uid) -> channel_id

# NEW: Casino (Word Pot)
casino_games: dict[Tuple[int,int,int], dict] = {}   # (gid, cid, uid) -> {answer, guesses[], max=3, legend, origin_cid, staked:int}
casino_channels: dict[Tuple[int,int], int] = {}     # (gid, uid) -> channel_id

# -------------------- DUNGEON globals --------------------
pending_dungeon_gates_by_msg: dict[int, dict] = {}   # gate_msg_id -> {...}
dungeon_games: dict[int, dict] = {}                  # ch_id -> game dict


# ---- Announce cards (UI/UX) ----
CARD_COLOR_DEFAULT = 0x2B2D31
CARD_COLOR_SUCCESS = 0x57F287  # green
CARD_COLOR_FAIL    = 0xED4245  # red
CARD_COLOR_INFO    = 0x5865F2  # blurple

def make_card(title: str, description: Optional[str] = None, *, fields: Optional[list[tuple[str,str,bool]]] = None, color: Optional[int] = None) -> discord.Embed:
    """
    Build a neat announcement embed. Use field tuples as (name, value, inline).
    """
    emb = discord.Embed(
        title=title,
        description=description or "",
        color=(color if color is not None else CARD_COLOR_DEFAULT),
    )
    if fields:
        for name, value, inline in fields:
            emb.add_field(name=name, value=(value or "—"), inline=inline)
    return emb


COLOR_PRIORITY = {"gray":0, "yellow":1, "green":2}

def update_legend(legend: dict[str,str], word: str, colors: list[str]):
    for ch, col in zip(word, colors):
        prev = legend.get(ch)
        if prev is None or COLOR_PRIORITY[col] > COLOR_PRIORITY[prev]:
            legend[ch] = col

ALPHABET = list("abcdefghijklmnopqrstuvwxyz")

def legend_overview(legend: dict[str, str], guesses: Optional[list[dict]] = None) -> str:
    """Render the legend:
       - Correct (green), Present (yellow)
       - Absent (shown with RED tiles)
       - Not used (letters never guessed; shown with GREY tiles)
    """
    if not legend:
        # still show Not used if we have guesses
        used_letters = set()
        if guesses:
            for g in guesses:
                used_letters.update(g.get("word", ""))
        else:
            return ""
    else:
        greens  = sorted([ch for ch, c in legend.items() if c == "green"])
        yellows = sorted([ch for ch, c in legend.items() if c == "yellow"])
        grays   = sorted([ch for ch, c in legend.items() if c == "gray"])

    # Determine which letters are not used yet
    if guesses:
        used_letters = set()
        for g in guesses:
            used_letters.update(g.get("word", ""))
    else:
        used_letters = set(legend.keys())

    not_used = [ch for ch in "abcdefghijklmnopqrstuvwxyz" if ch not in used_letters]

    parts = []
    if legend:
        if greens:
            parts.append("**Correct**: " + " ".join(render_tile(ch, "green") for ch in greens))
        if yellows:
            parts.append("**Present**: " + " ".join(render_tile(ch, "yellow") for ch in yellows))
        if grays:
            # show ABSENT with RED tiles
            parts.append("**Absent**: " + " ".join(render_tile(ch, "red") for ch in grays))

    if not_used:
        parts.append("**Not used**: " + " ".join(render_tile(ch, "gray") for ch in not_used))

    return "\n".join(parts)






# -------------------- fail quips (smaller fallback; file preferred) --------------------
# Trimmed fallback list; primary source is 'fail_quips.txt'
DEFAULT_FAIL_QUIPS = [
    "brutal. the tiles showed no mercy.",
    "the word juked you like a pro.",
    "close! …to five completely different letters.",
    "rng checked out, skill took a nap.",
    "your keyboard wants an apology.",
    "a flawless victory… for the dictionary.",
    "yellow tried to help. you ignored it.",
    "greens? haven’t heard of them.",
    "the tiles: undefeated.",
    "that was a speedrun of ‘incorrect’.",
]

def load_fail_quips() -> list[str]:
    """
    Load quips from fail_quips.txt (one per line).
    Does NOT write the file. If missing/empty, returns a small safe fallback.
    """
    p = pathlib.Path("fail_quips.txt")

    fallback = [
        "brutal. the tiles showed no mercy.",
        "close! …to five completely different letters.",
        "rng checked out, skill took a nap.",
        "a flawless victory… for the dictionary.",
        "today’s forecast: 100% gray with scattered cope.",
    ]

    try:
        if p.exists():
            lines = [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines()]
            out = [q for q in lines if q]
            if out:
                return out
            else:
                log.warning("[quips] fail_quips.txt is empty; using fallback.")
        else:
            log.info("[quips] fail_quips.txt not found; using fallback.")
    except Exception as e:
        log.warning(f"[quips] failed to read fail_quips.txt: {e}")

    return fallback


FAIL_QUIPS = load_fail_quips()

# -------------------- definitions (on fail) --------------------
_definition_cache: dict[str, str] = {}

def _fetch_definition_sync(word: str) -> str:
    """Fetch a short definition using the free dictionary API. Cached. Returns '' if unavailable."""
    w = word.lower()
    if w in _definition_cache:
        return _definition_cache[w]
    try:
        r = requests.get(f"https://api.dictionaryapi.dev/api/v2/entries/en/{w}", timeout=8)
        if r.status_code != 200:
            _definition_cache[w] = ""
            return ""
        data = r.json()
        # Walk to the first definition
        if isinstance(data, list) and data:
            meanings = data[0].get("meanings", [])
            for m in meanings:
                defs = m.get("definitions", [])
                if defs:
                    d = defs[0].get("definition", "")
                    if d:
                        # Clamp to a reasonable length
                        d = (d[:220] + "…") if len(d) > 220 else d
                        _definition_cache[w] = d
                        return d
    except Exception as e:
        log.warning(f"[defs] lookup failed for {w}: {e}")
    _definition_cache[w] = ""
    return ""

async def fetch_definition(word: str) -> str:
    return await asyncio.to_thread(_fetch_definition_sync, word)

# -------------------- guards --------------------
async def guard_worldler_inter(inter: discord.Interaction) -> bool:
    if inter.command and inter.command.name in {
        "immigrate","help","worldle_resync","worldle_bounty_setchannel",
        "worldle_set_category","worldle_set_announce","streaks","mystreak"
    }:
        return True
    if not inter.guild:
        await inter.response.send_message("Run this in a server.", ephemeral=True)
        return False
    if await is_worldler(inter.guild, inter.user):
        return True
    await inter.response.send_message(f"You need the **{WORLDLER_ROLE_NAME}** role. Use `/immigrate` to join Wordle World!", ephemeral=True)
    return False

async def guard_worldler_msg(msg: discord.Message) -> bool:
    if not msg.guild: return False
    if msg.author.bot: return False
    return await is_worldler(msg.guild, msg.author)

# -------------------- SOLO (private rooms + daily cap + announcements + streak touch) --------------------
def _key(gid, cid, uid): return (gid, cid, uid)

async def _make_private_solo_channel(invocation_channel: discord.TextChannel, member: discord.Member) -> Optional[discord.TextChannel]:
    guild = invocation_channel.guild
    me = guild.me
    if not me or not me.guild_permissions.manage_channels:
        await invocation_channel.send("I need **Manage Channels** to open your private Wordle room.", delete_after=20)
        return None

    cfg = await get_cfg(guild.id)
    rid = cfg["worldler_role_id"] or await ensure_worldler_role(guild)
    worldler_role = guild.get_role(rid) if rid else None

    category = guild.get_channel(cfg["solo_category_id"]) if cfg.get("solo_category_id") else None
    if category and not isinstance(category, discord.CategoryChannel):
        category = None

    base = re.sub(r"[^a-zA-Z0-9]+", "-", member.display_name).strip("-").lower() or f"user-{member.id}"
    base = f"{base}-worldle"
    name = base
    i = 2
    while discord.utils.get(guild.text_channels, name=name):
        name = f"{base}-{i}"; i += 1

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False, mention_everyone=False),
        member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, mention_everyone=False),
        me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True, mention_everyone=False),
    }
    if worldler_role:
        overwrites[worldler_role] = discord.PermissionOverwrite(view_channel=True, send_messages=False, read_message_history=True, mention_everyone=False)

    ch = await guild.create_text_channel(name=name, overwrites=overwrites, category=category, reason="Wordle World solo")
    return ch

async def _announce_result(
    guild: discord.Guild,
    origin_cid: Optional[int],
    content: str,
    board: Optional[str] = None,
    *,
    title: str | None = None,
    color: discord.Color = discord.Color.blurple(),
    embed: Optional[discord.Embed] = None,
):
    """
    Post to the configured announcements channel.
    If `embed` is provided, use it. Otherwise build a simple card from title/content/board.
    If nothing to post (no embed, no content, no board), do nothing.
    """
    if not guild:
        return

    cfg = await get_cfg(guild.id)
    ann_id = cfg.get("announcements_channel_id")
    if not ann_id:
        return

    ch = guild.get_channel(ann_id)
    if not isinstance(ch, discord.TextChannel):
        return
    if not ch.permissions_for(guild.me).send_messages:
        return

    # If no payload at all, don't send a blank card (lets you "probe" channel in callers)
    if embed is None and not (content or board or title):
        return

    if embed is None:
        emb = discord.Embed(
            title=title or "📣 Announcement",
            description=content or "",
            color=color
        )
        if board:
            emb.add_field(name="Board", value=board, inline=False)
    else:
        emb = embed

    # Add source footer if we can (and don’t overwrite an existing one)
    if origin_cid and not emb.footer:
        src = guild.get_channel(origin_cid)
        if isinstance(src, discord.TextChannel):
            emb.set_footer(text=f"from {src.name}")

    await safe_send(
        ch,
        embed=emb,
        allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
    )








async def solo_start(invocation_channel: discord.TextChannel, user: discord.Member) -> Optional[discord.TextChannel]:
    gid, uid = invocation_channel.guild.id, user.id
    today = uk_today_str()  # UK-local reset

    plays = await get_solo_plays_today(gid, uid, today)
    if plays >= 5:
        await send_boxed(
            invocation_channel,
            "Solo Wordle",
            f"{user.mention} you've reached your **5 solo games** for today. Resets at **00:00 UK time**.",
            icon="🧩",
        )
        return None

    existing_cid = solo_channels.get((gid, uid))
    if existing_cid and _key(gid, existing_cid, uid) in solo_games:
        ch = invocation_channel.guild.get_channel(existing_cid)
        if isinstance(ch, discord.TextChannel):
            await send_boxed(
                invocation_channel,
                "Solo Wordle",
                f"{user.mention} you already have a game running: {ch.mention}",
                icon="🧩",
            )
            return ch
        else:
            solo_channels.pop((gid, uid), None)

    ch = await _make_private_solo_channel(invocation_channel, user)
    if not ch:
        return None

    solo_games[_key(gid, ch.id, uid)] = {
        "answer": random.choice(ANSWERS),
        "guesses": [],
        "max": 5,
        "legend": {},
        "origin_cid": invocation_channel.id,
        "start_date": uk_today_str(),  # record which UK day this slot was consumed
        "snipers_tried": set(),        # shooters who already took a shot at THIS game
    }
    solo_channels[(gid, uid)] = ch.id
    await inc_solo_plays_today(gid, uid, today)
    # Streak touch only once per UK day
    if plays == 0:
        await update_streak_on_play(gid, uid, today)

    left = 5 - (plays + 1)
    await send_boxed(
        ch,
        "Solo — Your Wordle is ready",
        (
            f"{user.mention} You have **5 tries**.\n"
            "Payouts if you solve: 1st=5, 2nd=4, 3rd=3, 4th=2, 5th=1.\n"
            f"Today’s uses left **after this**: **{left}**."
        ),
        icon="🧩",
    )
    board = render_board(solo_games[_key(gid, ch.id, uid)]["guesses"])
    await ch.send(board)  # board stays unboxed/plain
    return ch




async def solo_guess(channel: discord.TextChannel, user: discord.Member, word: str):
    gid, cid, uid = channel.guild.id, channel.id, user.id
    game = solo_games.get(_key(gid, cid, uid))
    if not game:
        await send_boxed(channel, "Solo Wordle", f"{user.mention} no game here. Start with `w` or `/worldle`.", icon="🧩")
        return

    cleaned = "".join(ch for ch in word.lower().strip() if ch.isalpha())
    if len(cleaned) != 5:
        await send_boxed(channel, "Invalid Guess", "Guess must be **exactly 5 letters**.", icon="❗")
        return
    if not is_valid_guess(cleaned):
        await send_boxed(channel, "Invalid Guess", "That’s not in the Wordle dictionary (UK variants supported).", icon="📚")
        return
    if len(game["guesses"]) >= game["max"]:
        await send_boxed(channel, "Solo Wordle", "Out of tries! Start a new one with `w`.", icon="🧩")
        return

    colors = score_guess(cleaned, game["answer"])
    game["guesses"].append({"word": cleaned, "colors": colors})
    update_legend(game["legend"], cleaned, colors)

    board = render_board(game["guesses"])
    await channel.send(board)  # keep the board as plain text

    attempt = len(game["guesses"])

    def _cleanup():
        solo_games.pop(_key(gid, cid, uid), None)
        if solo_channels.get((gid, uid)) == cid:
            solo_channels.pop((gid, uid), None)

    # WIN
    if cleaned == game["answer"]:
        payout = payout_for_attempt(attempt)
        if payout:
            await change_balance(gid, uid, payout, announce_channel_id=cid)
        bal_new = await get_balance(gid, uid)
        origin_cid = game.get("origin_cid")
        ans = game["answer"].upper()
        _cleanup()

        await send_boxed(
            channel,
            "🏁 Solo — Solved!",
            f"{user.mention} solved **{ans}** on attempt **{attempt}** and earned **{payout} {EMO_SHEKEL()}**.\nBalance **{bal_new}**",
            icon="🎉",
        )

        emb = make_card(
            title="🏁 Solo — Finished",
            description=f"{user.mention} solved **{ans}** in **{attempt}** tries and earned **{payout} {EMO_SHEKEL()}**.",
            fields=[("Board", board, False)],
            color=CARD_COLOR_SUCCESS,
        )
        await _announce_result(channel.guild, origin_cid, content="", embed=emb)

        try:
            await channel.delete(reason="Wordle World solo finished (win)")
        except Exception:
            pass
        return

    # FAIL (out of tries)
    if attempt == game["max"]:
        ans_raw = game["answer"]
        ans = ans_raw.upper()
        origin_cid = game.get("origin_cid")
        quip = random.choice(FAIL_QUIPS)
        definition = await fetch_definition(ans_raw)
        _cleanup()
        await inc_stat(gid, uid, "solo_fails", 1)
        bal_now = await get_balance(gid, uid)

        desc = f"❌ Out of tries. The word was **{ans}** — {quip}\nBalance **{bal_now}**."
        fields = [("Board", board, False)]
        if definition:
            fields.append(("Definition", definition, False))
        await send_boxed(channel, "💀 Solo — Failed", desc, icon="💀", fields=fields)

        emb = make_card(
            title="💀 Solo — Failed",
            description=f"{user.mention} failed their Worldle. The word was **{ans}** — {quip}",
            fields=fields,
            color=CARD_COLOR_FAIL,
        )
        await _announce_result(channel.guild, origin_cid, content="", embed=emb)

        try:
            await channel.delete(reason="Wordle World solo finished (out of tries)")
        except Exception:
            pass
        return

    # MID-GAME STATUS (box the legend)
    next_attempt = attempt + 1
    legend = legend_overview(game["legend"], game["guesses"])
    payout = payout_for_attempt(next_attempt)
    status = f"Attempt **{attempt}/{game['max']}** — If you solve on attempt **{next_attempt}**, payout will be **{payout}**."
    flds = [("Next", status, False)]
    if legend:
        flds.append(("Legend", legend, False))
    await send_boxed(channel, "Solo — Status", "", icon="🧩", fields=flds)





# -------------------- CASINO: Word Pot (new) --------------------
async def casino_start_word_pot(invocation_channel: discord.TextChannel, user: discord.Member) -> Optional[discord.TextChannel]:
    gid, uid = invocation_channel.guild.id, user.id

    bal = await get_balance(gid, uid)
    if bal < 1:
        await send_boxed(invocation_channel, "Word Pot", f"{user.mention} you need **1 {EMO_SHEKEL()}** to play.", icon="🎰")
        return None

    existing_cid = casino_channels.get((gid, uid))
    if existing_cid and _key(gid, existing_cid, uid) in casino_games:
        ch = invocation_channel.guild.get_channel(existing_cid)
        if isinstance(ch, discord.TextChannel):
            await send_boxed(invocation_channel, "Word Pot", f"{user.mention} you already have a game running: {ch.mention}", icon="🎰")
            return ch
        else:
            casino_channels.pop((gid, uid), None)

    ch = await _make_private_solo_channel(invocation_channel, user)
    if not ch:
        return None

    # charge entry
    await change_balance(gid, uid, -1, announce_channel_id=ch.id)

    casino_games[_key(gid, ch.id, uid)] = {
        "answer": random.choice(ANSWERS), "guesses": [], "max": 3, "legend": {}, "origin_cid": invocation_channel.id, "staked": 1
    }
    casino_channels[(gid, uid)] = ch.id

    pot = await get_casino_pot(gid)
    await send_boxed(
        ch,
        "🎰 Word Pot",
        (
            f"{user.mention} • Entry: **1 {EMO_SHEKEL()}** (paid)\n"
            f"• Current Pot: **{pot} {EMO_SHEKEL()}** (resets to {CASINO_BASE_POT} on win)\n"
            "• You have **3 tries** — solve within 3 to **win the pot**.\n"
            "If you fail, your entry adds **+1** to the pot."
        ),
        icon="🎰",
    )
    board = render_board(casino_games[_key(gid, ch.id, uid)]["guesses"], total_rows=3)
    await ch.send(board)  # board as plain text
    return ch


async def casino_guess(channel: discord.TextChannel, user: discord.Member, word: str):
    gid, cid, uid = channel.guild.id, channel.id, user.id
    game = casino_games.get(_key(gid, cid, uid))
    if not game:
        await send_boxed(channel, "Word Pot", f"{user.mention} no Word Pot game here. Start with `/worldle_casino`.", icon="🎰")
        return

    cleaned = "".join(ch for ch in word.lower().strip() if ch.isalpha())
    if len(cleaned) != 5:
        await send_boxed(channel, "Invalid Guess", "Guess must be **exactly 5 letters**.", icon="❗")
        return
    if not is_valid_guess(cleaned):
        await send_boxed(channel, "Invalid Guess", "That’s not in the Wordle dictionary (UK variants supported).", icon="📚")
        return
    if len(game["guesses"]) >= game["max"]:
        await send_boxed(channel, "Word Pot", "Out of tries! Start a new one with `/worldle_casino`.", icon="🎰")
        return

    colors = score_guess(cleaned, game["answer"])
    game["guesses"].append({"word": cleaned, "colors": colors})
    update_legend(game["legend"], cleaned, colors)
    attempt = len(game["guesses"])

    board = render_board(game["guesses"], total_rows=3)
    await safe_send(channel, board)  # board stays plain

    def _cleanup():
        casino_games.pop(_key(gid, cid, uid), None)
        if casino_channels.get((gid, uid)) == cid:
            casino_channels.pop((gid, uid), None)

    # WIN
    if cleaned == game["answer"]:
        pot = await get_casino_pot(gid)
        await change_balance(gid, uid, pot, announce_channel_id=cid)
        bal_new = await get_balance(gid, uid)
        ans = game["answer"].upper()
        origin_cid = game.get("origin_cid")
        _cleanup()
        await set_casino_pot(gid, CASINO_BASE_POT)

        await send_boxed(
            channel,
            "🏆 Word Pot — WIN",
            f"{user.mention} solved **{ans}** on attempt **{attempt}** and **WON {pot} {EMO_SHEKEL()}**!\nPot resets to **{CASINO_BASE_POT}**. (Balance: {bal_new})",
            icon="🎰",
        )

        emb = make_card(
            title="🎰 Word Pot — WIN",
            description=f"{user.mention} won **{pot} {EMO_SHEKEL()}** by solving **{ans}** on attempt **{attempt}**.",
            fields=[
                ("Board", board, False),
                ("Next Pot", f"Resets to **{CASINO_BASE_POT}**", True),
            ],
            color=CARD_COLOR_SUCCESS,
        )
        await _announce_result(channel.guild, origin_cid, content="", embed=emb)

        try:
            await channel.delete(reason="Word Pot finished (win)")
        except Exception:
            pass
        return

    # FAIL (out of tries)
    if attempt == game["max"]:
        cur_pot = await get_casino_pot(gid)
        add_amt = (game.get("staked", 0) or 0)
        new_pot = cur_pot + add_amt
        await set_casino_pot(gid, new_pot)
        ans_raw = game["answer"]
        ans = ans_raw.upper()
        quip = random.choice(FAIL_QUIPS)
        definition = await fetch_definition(ans_raw)
        origin_cid = game.get("origin_cid")
        _cleanup()

        fields = [("Board", board, False), ("Pot", f"Now **{new_pot} {EMO_SHEKEL()}**", True)]
        if definition:
            fields.append(("Definition", definition, False))

        await send_boxed(
            channel,
            "🎰 Word Pot — Failed",
            f"❌ The word was **{ans}** — {quip}",
            icon="🎰",
            fields=fields,
        )

        emb = make_card(
            title="🎰 Word Pot — Failed",
            description=f"{user.mention} failed **Word Pot** — the word was **{ans}**. {quip}",
            fields=fields,
            color=CARD_COLOR_FAIL,
        )
        await _announce_result(channel.guild, origin_cid, content="", embed=emb)

        try:
            await channel.delete(reason="Word Pot finished (fail)")
        except Exception:
            pass
        return

    # MID-GAME STATUS (box the legend)
    legend = legend_overview(game["legend"], game["guesses"])
    msg = f"Attempt **{attempt}/3** — solve within **3** to win the pot."
    flds = [("Next", msg, False)]
    if legend:
        flds.append(("Legend", legend, False))
    await send_boxed(channel, "Word Pot — Status", "", icon="🎰", fields=flds)


# --- Quantity modal: defer first, then buy; success is PUBLIC ---
class QuantityModal(discord.ui.Modal):
    def __init__(
        self,
        key: str,
        label: str,
        owner_id: int,
        panel_channel_id: int,
        panel_message_id: int,
    ):
        super().__init__(title=f"Buy {label}")
        self.key = key
        self.label = label
        self.owner_id = int(owner_id)
        self.panel_channel_id = int(panel_channel_id)
        self.panel_message_id = int(panel_message_id)

        self.amount = discord.ui.TextInput(
            label="Quantity to buy",
            placeholder="Enter a positive whole number",
            required=True,
            min_length=1,
            max_length=6,
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner_id:
            return await send_boxed(
                interaction, "Shop", "This shop view isn’t yours. Use **/shop** to open your own.", icon="🛒"
            )

        # validate
        try:
            qty = int(str(self.amount.value).strip())
        except ValueError:
            return await send_boxed(interaction, "Shop", "Enter a whole number.", icon="🛒")
        if qty <= 0:
            return await send_boxed(interaction, "Shop", "Enter a quantity greater than **0**.", icon="🛒")

        # don’t block UI
        try:
            await interaction.response.defer(thinking=False)
        except Exception:
            pass

        # do the buy (public confirmation)
        # NOTE: keep this call name the same as in your codebase if different.
        await _shop_perform_buy(interaction, self.key, qty)

        # refresh the original Shop message so the balance updates
        try:
            gid, uid = interaction.guild.id, interaction.user.id
            emb = await _build_shop_embed(gid, uid)

            channel = interaction.client.get_channel(self.panel_channel_id) or await interaction.client.fetch_channel(self.panel_channel_id)
            msg = await channel.fetch_message(self.panel_message_id)

            view = ShopView(interaction)
            view.message = msg
            await msg.edit(embed=emb, view=view)
        except Exception:
            # swallow refresh errors to avoid breaking the purchase flow
            pass



class SellQuantityModal(discord.ui.Modal):
    def __init__(
        self,
        key: str,
        label: str,
        max_qty: int,
        price_each: int,
        owner_id: int | None,
        panel_channel_id: int,
        panel_message_id: int,
    ):
        super().__init__(title=f"Sell {label}")
        self.key = key
        self.label = label
        self.max_qty = int(max_qty)
        self.price_each = int(price_each)
        self.owner_id = owner_id
        self.panel_channel_id = int(panel_channel_id)
        self.panel_message_id = int(panel_message_id)

        hint = f"You own {self.max_qty}. Price each: {self.price_each} {EMO_SHEKEL()}."
        self.amount = discord.ui.TextInput(
            label="Quantity to sell",
            placeholder=f"Enter 1–{self.max_qty}  ({hint})",
            required=True,
            min_length=1,
            max_length=6,
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if self.owner_id and interaction.user.id != int(self.owner_id):
            return await send_boxed(interaction, "Shop — Sell", "This sell panel isn’t yours. Use **/shop** to open your own.", icon="🛍️")

        try:
            qty = int(str(self.amount.value).strip())
        except ValueError:
            return await send_boxed(interaction, "Shop — Sell", "Enter a whole number.", icon="🛍️")
        if qty <= 0 or qty > self.max_qty:
            return await send_boxed(interaction, "Shop — Sell", f"Enter **1–{self.max_qty}**.", icon="🛍️")

        try:
            await interaction.response.defer(thinking=False)
        except Exception:
            pass

        # Perform the sale (public confirmation message)
        await _shop_perform_sell(interaction, self.key, qty)

        # Refresh the Sell panel (balance + counts)
        try:
            gid, uid = interaction.guild.id, interaction.user.id
            owned = await _get_owned_sellables(gid, uid)
            emb = await _build_sell_embed(gid, uid, owned)

            channel = interaction.client.get_channel(self.panel_channel_id) or await interaction.client.fetch_channel(self.panel_channel_id)
            msg = await channel.fetch_message(self.panel_message_id)

            view = SellMenuView(interaction, owned)
            view.message = msg
            await msg.edit(embed=emb, view=view)
        except Exception:
            pass






class ShopView(discord.ui.View):
    """Buttons open a quantity modal (buy) or switch to the Sell panel. Success messages are PUBLIC."""
    def __init__(self, inter: discord.Interaction, *, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.owner_id = inter.user.id
        self.guild_id = inter.guild.id
        self.message: Optional[discord.Message] = None  # set by /shop after send

        def add_buy_btn(key: str, label: str, emoji: str, style: discord.ButtonStyle):
            btn = discord.ui.Button(label=label, emoji=emoji, style=style)

            async def _cb(i: discord.Interaction, _key=key, _label=label):
                if i.user.id != self.owner_id:
                    return await send_boxed(i, "Shop", "This shop view isn’t yours. Use **/shop** to open your own.", icon="🛒")
                # pass panel ids so the modal can refresh the same message after purchase
                await i.response.send_modal(
                    QuantityModal(
                        _key,
                        _label,
                        self.owner_id,
                        panel_channel_id=i.channel.id,
                        panel_message_id=self.message.id if self.message else i.message.id,
                    )
                )

            btn.callback = _cb
            self.add_item(btn)

        # Buy buttons
        add_buy_btn("stone",     "Stone",     EMO_STONE(),   discord.ButtonStyle.primary)
        add_buy_btn("badge",     "Badge",     EMO_BADGE(),   discord.ButtonStyle.secondary)
        add_buy_btn("chicken",   "Chicken",   EMO_CHICKEN(), discord.ButtonStyle.secondary)
        add_buy_btn("sniper",    "Sniper",    EMO_SNIPER(),  discord.ButtonStyle.secondary)
        add_buy_btn("ticket_t3", "T3 Ticket", EMO_DUNGEON(), discord.ButtonStyle.success)

        # SELL (red) — replaces this message with the Sell menu
        sell_btn = discord.ui.Button(label="Sell", emoji="💸", style=discord.ButtonStyle.danger)

        async def _sell_cb(i: discord.Interaction):
            if i.user.id != self.owner_id:
                return await send_boxed(i, "Shop — Sell", "This sell panel isn’t yours. Use **/shop** to open your own.", icon="🛍️")
            owned = await _get_owned_sellables(i.guild.id, i.user.id)
            emb = await _build_sell_embed(i.guild.id, i.user.id, owned)
            view = SellMenuView(i, owned)
            view.message = self.message
            await i.response.edit_message(embed=emb, view=view)

        sell_btn.callback = _sell_cb
        self.add_item(sell_btn)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        try:
            if self.message:
                await self.message.edit(view=self)
        except Exception:
            pass





class SellMenuView(discord.ui.View):
    def __init__(self, inter: discord.Interaction, owned: list[dict], *, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.owner_id = inter.user.id
        self.guild_id = inter.guild.id
        self.channel_id = inter.channel.id
        self.message: Optional[discord.Message] = None
        self.owned = owned

        # Select with owned items
        options = []
        for item in owned:
            label = f"{item['emoji']} {item['label']}"
            desc = f"Owned: {item['count']} • Sell price: {item['price_each']} each"
            options.append(discord.SelectOption(label=label, value=item["key"], description=desc))

        select = discord.ui.Select(placeholder="Pick an item to sell…", options=options)

        async def _on_pick(i: discord.Interaction):
            if i.user.id != self.owner_id:
                return await send_boxed(i, "Shop — Sell", "This sell panel isn’t yours. Use **/shop** to open your own.", icon="🛍️")

            await i.response.send_modal(
                SellQuantityModal(
                    key=select.values[0],
                    label=next(x["label"] for x in self.owned if x["key"] == select.values[0]),
                    max_qty=next(x["count"] for x in self.owned if x["key"] == select.values[0]),
                    price_each=next(x["price_each"] for x in self.owned if x["key"] == select.values[0]),
                    owner_id=self.owner_id,
                    panel_channel_id=self.channel_id,
                    panel_message_id=self.message.id if self.message else i.message.id,
                )
            )

        select.callback = _on_pick
        self.add_item(select)

        # Back to Shop
        back_btn = discord.ui.Button(label="Shop", emoji="🛒", style=discord.ButtonStyle.primary)

        async def _back(i: discord.Interaction):
            if i.user.id != self.owner_id:
                return await send_boxed(i, "Shop", "This shop view isn’t yours. Use **/shop** to open your own.", icon="🛒")
            emb = await _build_shop_embed(self.guild_id, self.owner_id)
            view = ShopView(i)
            view.message = self.message
            await i.response.edit_message(embed=emb, view=view)

        back_btn.callback = _back
        self.add_item(back_btn)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        try:
            if self.message:
                await self.message.edit(view=self)
        except Exception:
            pass




# ---------- HELP PAGER UI ----------
class HelpBook(discord.ui.View):
    def __init__(self, pages: list[discord.Embed], start_index: int = 0, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.pages = pages
        self.index = max(0, min(start_index, len(pages)-1))
        # Build select options from page titles
        self.jump_select.options = [
            discord.SelectOption(label=emb.title[:100] if emb.title else f"Page {i+1}", value=str(i))
            for i, emb in enumerate(self.pages)
        ]
        self._sync_buttons()

    def _sync_buttons(self):
        at_first = self.index <= 0
        at_last = self.index >= len(self.pages) - 1
        self.first_btn.disabled = at_first
        self.prev_btn.disabled = at_first
        self.next_btn.disabled = at_last
        self.last_btn.disabled = at_last

    async def _show(self, interaction: discord.Interaction):
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    @discord.ui.button(label="⏮ First", style=discord.ButtonStyle.secondary)
    async def first_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = 0
        await self._show(interaction)

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.index > 0:
            self.index -= 1
        await self._show(interaction)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.index < len(self.pages) - 1:
            self.index += 1
        await self._show(interaction)

    @discord.ui.button(label="Last ⏭", style=discord.ButtonStyle.secondary)
    async def last_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = len(self.pages) - 1
        await self._show(interaction)

    @discord.ui.select(placeholder="Jump to section…")
    async def jump_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        try:
            self.index = int(select.values[0])
        except Exception:
            pass
        await self._show(interaction)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger)
    async def close_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Keep the message public, just remove the controls
        await interaction.response.edit_message(view=None)

def build_help_pages(guild_name: str | None = None) -> list[discord.Embed]:
    g = guild_name or "this server"
    shek = EMO_SHEKEL()
    stone = EMO_STONE()
    chick = EMO_CHICKEN()
    badge = EMO_BADGE()
    sniper = EMO_SNIPER()
    bounty = EMO_BOUNTY()

    pages: list[discord.Embed] = []

    # 1) Overview
    emb = discord.Embed(title="Wordle World — Overview")
    emb.description = (
        "Play Wordle-style games, earn coins (**Shekels**), and unlock roles. "
        "Daily limits and resets are based on **00:00 UK time** (London).\n\n"
        "Flip through these pages for a quick tour and exact commands."
    )
    emb.add_field(name="Quick Start", value="• Type **`/immigrate`** to join.\n• Then type **`w`** to start a solo game.", inline=False)
    emb.add_field(name="Currencies & Items", value=f"• {shek} **Shekels** — used in shop and fees.\n• Items: {stone}, {chick}, {badge}, {sniper}.", inline=False)
    emb.set_footer(text="Pages: Overview, Join & Roles, Solo, Word Pot, Bounty, Duels, Economy, Stones, Streaks, Admin, Shortcuts")
    pages.append(emb)

    # 2) Join & Roles
    emb = discord.Embed(title="Join & Roles")
    emb.add_field(name="Join the game", value=f"• **/immigrate** — grants the **{WORLDLER_ROLE_NAME}** role and a small bonus.", inline=False)
    emb.add_field(name="Tier Roles", value="• Admins can bind balance thresholds to roles.\n• Your roles auto-update as your balance changes.", inline=False)
    emb.add_field(name="Helpful Admin Commands", value="• **/role_maketier**, **/role_addtier**, **/role_removetier**, **/role_sync**", inline=False)
    pages.append(emb)

    # 3) Solo Mode
    emb = discord.Embed(title="Solo Wordle (private room)")
    emb.description = (
        "Five tries to guess a 5-letter word. UK dictionary variants are allowed. "
        "You get **5 solo games per day**."
    )
    emb.add_field(name="Start", value="• **`w`** or **/worldle**", inline=False)
    emb.add_field(name="Guess", value="• **`g APPLE`** or **/worldle_guess word:APPLE**", inline=False)
    emb.add_field(
        name="Payouts",
        value=f"• Solve on 1st→5 {shek} · 2nd→4 · 3rd→3 · 4th→2 · 5th→1",
        inline=False,
    )
    emb.add_field(name="End Early", value="• **/worldle_end** (counts as a fail).", inline=False)
    pages.append(emb)

    # 4) Word Pot (Casino)
    emb = discord.Embed(title="Word Pot (Casino)")
    emb.description = (
        "A shared prize pool across the server.\n"
        "• Costs **1** shekel to play.\n"
        "• You have **3 tries**. If you solve within 3, you **win the whole pot**.\n"
        f"• Pot **resets to {CASINO_BASE_POT}** after a win."
    )
    emb.add_field(name="Start / Guess / End", value="• **/worldle_casino** to start.\n• Guess with **`g WORD`** (same as solo) or **/worldle_guess** in your room.\n• **/worldle_end** to end early (fail).", inline=False)
    emb.add_field(name="Announcements & Quips", value="• Wins/fails are announced publicly with spicy fail quips. 🎤", inline=False)
    pages.append(emb)

    # 5) Bounty
    emb = discord.Embed(title="Hourly Bounty")
    emb.description = (
        f"A server-wide race. When the prompt drops, **react with {bounty}** to arm it (needs 2 players), "
        "then guess in the bounty channel."
    )
    emb.add_field(name="Admin Setup", value="• **/worldle_bounty_setchannel** to choose the channel.", inline=False)
    emb.add_field(name="Manual Drop", value="• **/worldle_bounty_now** posts a prompt immediately.", inline=False)
    emb.add_field(name="Play", value="• **`bg WORD`** or **/worldle_bounty_guess word:WORD**", inline=False)
    emb.add_field(name="Reward", value=f"• First solver wins **{BOUNTY_PAYOUT} {shek}**.", inline=False)
    pages.append(emb)

    # 6) Duels
    emb = discord.Embed(title="Duels")
    emb.description = "Challenge a player. Stake goes into a pot; first to solve wins the lot."
    emb.add_field(name="Create", value="• **/worldle_challenge user:@Name amount:10**", inline=False)
    emb.add_field(name="Accept / Cancel", value="• **/worldle_accept id:**, **/worldle_cancel id:**", inline=False)
    emb.add_field(name="Guess", value="• **/worldle_duel_guess id:123 word:APPLE** or just **`g APPLE`** when it’s your turn.", inline=False)
    pages.append(emb)

    # 7) Economy & Shop
    emb = discord.Embed(title="Economy & Shop")
    emb.add_field(name="Daily", value=f"• **/pray** → +5 {shek} (once per day)\n• **/beg** → +5 {stone} (once per day)", inline=False)
    emb.add_field(
        name="Shop",
        value=(
            f"• **/shop**, **/buy**, **/sell**\n"
            f"• Items: {stone} **Stone** (49% drop chance), {chick} **Fried Chicken** (+1h protection), "
            f"{badge} **Bounty Hunter Badge** (pings), {sniper} **Sniper** (guess into others’ solo)."
        ),
        inline=False,
    )
    emb.add_field(name="Wallet / Inventory / Badges / LB", value="• **/balance**, **/inventory**, **/badges**, **/leaderboard**", inline=False)
    pages.append(emb)

    # 8) Stones & Protection
    emb = discord.Embed(title="Stones & Protection")
    emb.description = (
        f"Throw {stone} at players to make them drop shekels into the ground pot.\n"
        "• Each throw has a **49%** chance to hit.\n"
        "• If they’re protected, stones are wasted (no drop).\n"
        "• You can only stone the **same player up to 15 times per day**. You can still stone others."
    )
    emb.add_field(name="Commands", value=f"• **/stone user:@Name times:5**\n• **/collect** to pick up all ground shekels.\n• **/eat amount:1** to gain protection (1 hour each).", inline=False)
    pages.append(emb)

    # 9) Streaks
    emb = discord.Embed(title="Streaks (UK days)")
    emb.description = "Play at least one solo game per day to keep your streak alive."
    emb.add_field(name="See Streaks", value="• **/streaks** (server top)\n• **/mystreak** (yours)", inline=False)
    pages.append(emb)

    # 10) Admin / Setup
    emb = discord.Embed(title="Admin / Setup")
    emb.add_field(name="Solo Rooms Category", value="• **/worldle_set_category**", inline=False)
    emb.add_field(name="Announcements Channel", value="• **/worldle_set_announce**", inline=False)
    emb.add_field(name="Bounty Channel", value="• **/worldle_bounty_setchannel**", inline=False)
    emb.add_field(name="Resync Commands", value="• **/worldle_resync**", inline=False)
    emb.add_field(name="Set Balance", value=f"• **/set_balance user:@Name amount:123** ({shek})", inline=False)
    pages.append(emb)

    # 11) Text Shortcuts (recap)
    emb = discord.Embed(title="Text Shortcuts (Anywhere)")
    emb.add_field(name="Start Solo", value="• **`w`**", inline=False)
    emb.add_field(name="Guess", value="• **`g WORD`** (smart: solo, duel turn, or bounty if in the bounty channel)", inline=False)
    emb.add_field(name="Bounty Guess", value="• **`bg WORD`**", inline=False)
    pages.append(emb)

    return pages

# -------------------- GLOBALS --------------------

# ---- Dailies panel state (place near other globals) ----
dailies_msg_ids: set[int] = set()   # message IDs of active /dailies panels


# -------------------- BOUNTY (hourly GMT + reaction gate; manual now uses gate too) --------------------
BOUNTY_PAYOUT = 5
BOUNTY_EXPIRE_MIN = 59
BOUNTY_EXPIRE_S = BOUNTY_EXPIRE_MIN * 60

# NEW: arm delay and per-user guess cooldown
BOUNTY_ARM_DELAY_S = 60          # wait 60s after 2 reactions before arming
BOUNTY_GUESS_COOLDOWN_S = 5      # 5s per-user cooldown between guesses

# Track last guess time per (guild_id, user_id) for the bounty
last_bounty_guess_ts: dict[tuple[int, int], int] = {}



async def _build_dailies_embed(guild_id: int, user_id: int) -> discord.Embed:
    solo_left   = await get_solo_wordles_left(guild_id, user_id)
    prayed_done = await has_prayed_today(guild_id, user_id)
    begged_done = await has_begged_today(guild_id, user_id)
    pot_amount  = await get_word_pot_total(guild_id)

    em = discord.Embed(
        title="📅 Daily Actions (UK reset)",
        color=discord.Color.blurple(),
        description=(
            "All daily limits reset at **00:00 UK time** (Europe/London).\n"
            "Use the buttons below.\n\n"
            "**Solo Worldles**\n"
            "Play up to **5/day**.\n"
            f"You have **{solo_left}** left today.\n"
            "Start with 🎲.\n\n"
            "**Pray**\n"
            "+5 ẅ once per day.\n"
            f"Status: {'✅ done today.' if prayed_done else '❌ not done yet.'}\n"
            "Use: 🙏\n\n"
            "**Beg**\n"
            "+5 🧱 stones once per day.\n"
            f"Status: {'✅ done today.' if begged_done else '❌ not done yet.'}\n"
            "Use: 🤲\n\n"
            "**Extras**\n"
            "💰 **Word Pot** — not a daily, but fun!\n"
            f"Current pot: {EMO_SHEKEL()} **{pot_amount}** available to win.\n"
            "Use the 💰 Word Pot button below."
        )
    )
    em.set_footer(text=f"Status shown for user ID {user_id}")
    return em






# --- DailiesView (drop-in replacement) ---
import weakref
from collections import defaultdict
from typing import Optional

class DailiesView(discord.ui.View):
    """
    Dailies panel buttons.

    - Word Pot button shows: 'Word Pot (X shekels)' with a 💰 icon.
    - The panel can be refreshed from *outside* (e.g., when the pot changes)
      via: await DailiesView.refresh_pot_label_for_guild(guild_id)
    """

    # registry of active views by guild (weak refs so they clean up automatically)
    _registry: dict[int, "weakref.WeakSet[DailiesView]"] = defaultdict(weakref.WeakSet)

    def __init__(self, interaction: discord.Interaction, *, pot_amount: int, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.owner_id = interaction.user.id
        self.guild_id: Optional[int] = interaction.guild.id if interaction.guild else None
        self.message: Optional[discord.Message] = None  # set right after /dailies send
        self.btn_pot: Optional[discord.ui.Button] = None

        # --- Start Solo ---
        btn_start = discord.ui.Button(
            label="Start Solo (w)",
            emoji="🎲",
            style=discord.ButtonStyle.primary,
        )

        async def _start_cb(i: discord.Interaction):
            if i.user.id != self.owner_id:
                return await i.response.send_message("Open your own dailies with **/dailies**.", ephemeral=True)
            if not i.guild or not i.channel:
                return
            ch = await solo_start(i.channel, i.user)
            if isinstance(ch, discord.TextChannel):
                await send_boxed(i, "Solo Room Opened", f"{i.user.mention} your room is {ch.mention}.", icon="🧩", ephemeral=False)
            await self._refresh_panel(i)

        btn_start.callback = _start_cb
        self.add_item(btn_start)

        # --- Pray (+5) ---
        btn_pray = discord.ui.Button(
            label="Pray (+5)",
            emoji="🙏",
            style=discord.ButtonStyle.success,
        )

        async def _pray_cb(i: discord.Interaction):
            if i.user.id != self.owner_id:
                return await i.response.send_message("Open your own dailies with **/dailies**.", ephemeral=True)
            if not i.guild:
                return
            gid, uid, cid = i.guild.id, i.user.id, getattr(i.channel, "id", None)
            today = uk_today_str()
            last_pray, _ = await _get_cd(gid, uid)
            if last_pray == today:
                await send_boxed(i, "Daily — Pray", "You already prayed today. Resets at **00:00 UK time**.", icon="🛐", ephemeral=True)
            else:
                await change_balance(gid, uid, 5, announce_channel_id=cid)
                await _set_cd(gid, uid, "last_pray", today)
                bal = await get_balance(gid, uid)
                await send_boxed(i, "Daily — Pray", f"+5 {EMO_SHEKEL()}  · Balance **{bal}**", icon="🛐", ephemeral=False)
            await self._refresh_panel(i)

        btn_pray.callback = _pray_cb
        self.add_item(btn_pray)

        # --- Beg (+5 stones) ---
        btn_beg = discord.ui.Button(
            label="Beg (+5 stones)",
            emoji="🤲",
            style=discord.ButtonStyle.secondary,
        )

        async def _beg_cb(i: discord.Interaction):
            if i.user.id != self.owner_id:
                return await i.response.send_message("Open your own dailies with **/dailies**.", ephemeral=True)
            if not i.guild:
                return
            gid, uid = i.guild.id, i.user.id
            today = uk_today_str()
            _, last_beg = await _get_cd(gid, uid)
            if last_beg == today:
                await send_boxed(i, "Daily — Beg", "You already begged today. Resets at **00:00 UK time**.", icon="🙇", ephemeral=True)
            else:
                await change_stones(gid, uid, 5)
                await _set_cd(gid, uid, "last_beg", today)
                stones = await get_stones(gid, uid)
                await send_boxed(i, "Daily — Beg", f"{EMO_STONE()} +5 Stones. You now have **{stones}**.", icon="🙇", ephemeral=False)
            await self._refresh_panel(i)

        btn_beg.callback = _beg_cb
        self.add_item(btn_beg)

        # --- Word Pot (label: 'Word Pot (X shekels)') ---
        btn_pot = discord.ui.Button(
            label=f"Word Pot ({pot_amount} shekels)",
            emoji="💰",
            style=discord.ButtonStyle.secondary,
        )

        async def _pot_cb(i: discord.Interaction):
            if i.user.id != self.owner_id:
                return await i.response.send_message("Open your own dailies with **/dailies**.", ephemeral=True)
            if not i.guild or not i.channel:
                return
            ch = await casino_start_word_pot(i.channel, i.user)
            if isinstance(ch, discord.TextChannel):
                await send_boxed(i, "Word Pot", f"{i.user.mention} Word Pot room: {ch.mention}", icon="💰", ephemeral=False)
            # initial refresh right after starting
            await self._refresh_panel(i)

        btn_pot.callback = _pot_cb
        self.add_item(btn_pot)
        self.btn_pot = btn_pot

    # called by /dailies after sending the message
    def attach_message(self, msg: discord.Message) -> None:
        self.message = msg
        if self.guild_id is not None:
            DailiesView._registry[self.guild_id].add(self)

    async def _refresh_panel(self, i: discord.Interaction):
        """Refresh the dailies embed + Word Pot button label from inside an interaction."""
        try:
            if not i.guild:
                return
            latest = await get_word_pot_total(i.guild.id)
            if self.btn_pot:
                self.btn_pot.label = f"Word Pot ({latest} shekels)"
            emb = await _build_dailies_embed(i.guild.id, self.owner_id)

            target_msg = self.message or (await i.original_response() if i.response.is_done() else None)
            if target_msg:
                await target_msg.edit(embed=emb, view=self)
            else:
                await i.edit_original_response(embed=emb, view=self)
        except Exception:
            pass  # never blow up the UX

    @classmethod
    async def refresh_pot_label_for_guild(cls, guild_id: int) -> None:
        """
        External refresh: call this AFTER the pot value changes anywhere.
        It updates the button label *and* the embed for all active dailies in that guild.
        """
        views = list(cls._registry.get(guild_id, ()))  # snapshot
        if not views:
            return

        latest = await get_word_pot_total(guild_id)
        to_prune = []
        for view in views:
            try:
                if view.btn_pot:
                    view.btn_pot.label = f"Word Pot ({latest} shekels)"
                emb = await _build_dailies_embed(guild_id, view.owner_id)
                if view.message:
                    await view.message.edit(embed=emb, view=view)
                else:
                    # no message yet (race) — skip, we'll catch it on next call
                    pass
            except Exception:
                to_prune.append(view)
        # prune broken/dead views
        for v in to_prune:
            try:
                cls._registry[guild_id].discard(v)
            except Exception:
                pass

    async def on_timeout(self) -> None:
        for c in self.children:
            c.disabled = True
        try:
            if self.message:
                await self.message.edit(view=self)
        except Exception:
            pass
        # unregister on timeout
        try:
            if self.guild_id is not None:
                self._registry[self.guild_id].discard(self)
        except Exception:
            pass












async def dailies_reaction_listener(payload: discord.RawReactionActionEvent):
    """Independent reaction handler for /dailies panels only."""
    try:
        if payload.user_id == (bot.user.id if bot.user else 0):
            return
        if payload.message_id not in dailies_msg_ids:
            return

        guild = discord.utils.get(bot.guilds, id=payload.guild_id)
        if not guild:
            return
        try:
            member = guild.get_member(payload.user_id) or await guild.fetch_member(payload.user_id)
        except Exception:
            return
        if not member or member.bot or not await is_worldler(guild, member):
            return

        channel = guild.get_channel(payload.channel_id) if hasattr(payload, "channel_id") else None
        if not isinstance(channel, discord.TextChannel):
            # Fallback: try fetching via the message
            try:
                channel = await guild.fetch_channel(payload.channel_id)
            except Exception:
                return

        emoji_name = payload.emoji.name

        # React: 🧩 = Start Solo
        if emoji_name == "🧩":
            ch = await solo_start(channel, member)
            if isinstance(ch, discord.TextChannel):
                await safe_send(
                    channel,
                    f"🧩 {member.mention} your solo room is {ch.mention}.",
                    allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False)
                )

        # React: 🛐 = Pray
        elif emoji_name == "🛐":
            gid, uid = guild.id, member.id
            today = uk_today_str()
            last_pray, _ = await _get_cd(gid, uid)
            if last_pray == today:
                await safe_send(channel, f"🛐 {member.mention} you already prayed today.", 
                                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
            else:
                await change_balance(gid, uid, 5, announce_channel_id=channel.id)
                await _set_cd(gid, uid, "last_pray", today)
                bal = await get_balance(gid, uid)
                await safe_send(channel, f"🛐 {member.mention} +5 {EMO_SHEKEL()} — Balance **{bal}**",
                                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))

        # React: 🙇 = Beg
        elif emoji_name == "🙇":
            gid, uid = guild.id, member.id
            today = uk_today_str()
            _, last_beg = await _get_cd(gid, uid)
            if last_beg == today:
                await safe_send(channel, f"🙇 {member.mention} you already begged today.",
                                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
            else:
                await change_stones(gid, uid, 5)
                await _set_cd(gid, uid, "last_beg", today)
                stones = await get_stones(gid, uid)
                await safe_send(channel, f"🙇 {member.mention} {EMO_STONE()} +5 Stones — You now have **{stones}**.",
                                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))

        # React: 🎰 = Word Pot
        elif emoji_name == "🎰":
            ch = await casino_start_word_pot(channel, member)
            if isinstance(ch, discord.TextChannel):
                await safe_send(
                    channel,
                    f"🎰 {member.mention} Word Pot room: {ch.mention}",
                    allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False)
                )

        # Tidy up: remove the user’s reaction so others can easily use it too
        try:
            msg = await channel.fetch_message(payload.message_id)
            await msg.remove_reaction(payload.emoji, member)
        except Exception:
            pass

    except Exception as e:
        log.warning(f"[dailies] reaction handler error: {e}")



async def dailies_raw_reaction_add(payload: discord.RawReactionActionEvent):
    """Independent reaction handler for /dailies panels only (and refresh the panel)."""
    try:
        # Only handle reactions on dailies panels we sent
        if payload.message_id not in dailies_msg_ids:
            return

        # Ignore the bot's own reactions
        if bot.user and payload.user_id == bot.user.id:
            return

        guild = discord.utils.get(bot.guilds, id=payload.guild_id)
        if not guild:
            return

        try:
            member = guild.get_member(payload.user_id) or await guild.fetch_member(payload.user_id)
        except Exception:
            member = None
        if not member or member.bot or not await is_worldler(guild, member):
            return

        # Resolve channel to reply in
        channel = guild.get_channel(getattr(payload, "channel_id", 0))
        if not isinstance(channel, discord.TextChannel):
            try:
                channel = await guild.fetch_channel(getattr(payload, "channel_id", 0))
            except Exception:
                return

        emoji_name = payload.emoji.name

        # 🧩 Start Solo
        if emoji_name == "🧩":
            ch = await solo_start(channel, member)
            if isinstance(ch, discord.TextChannel):
                await safe_send(
                    channel,
                    f"🧩 {member.mention} your solo room is {ch.mention}.",
                    allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False)
                )

        # 🛐 Pray
        elif emoji_name == "🛐":
            gid, uid = guild.id, member.id
            today = uk_today_str()
            last_pray, _ = await _get_cd(gid, uid)
            if last_pray == today:
                await safe_send(channel, f"🛐 {member.mention} you already prayed today (resets 00:00 UK).",
                                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
            else:
                await change_balance(gid, uid, 5, announce_channel_id=channel.id)
                await _set_cd(gid, uid, "last_pray", today)
                bal = await get_balance(gid, uid)
                await safe_send(channel, f"🛐 {member.mention} +5 {EMO_SHEKEL()} — Balance **{bal}**",
                                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))

        # 🙇 Beg
        elif emoji_name == "🙇":
            gid, uid = guild.id, member.id
            today = uk_today_str()
            _, last_beg = await _get_cd(gid, uid)
            if last_beg == today:
                await safe_send(channel, f"🙇 {member.mention} you already begged today (resets 00:00 UK).",
                                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
            else:
                await change_stones(gid, uid, 5)
                await _set_cd(gid, uid, "last_beg", today)
                stones = await get_stones(gid, uid)
                await safe_send(channel, f"🙇 {member.mention} {EMO_STONE()} +5 Stones — You now have **{stones}**.",
                                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))

        # 🎰 Word Pot (casino)
        elif emoji_name == "🎰":
            ch = await casino_start_word_pot(channel, member)
            if isinstance(ch, discord.TextChannel):
                await safe_send(
                    channel,
                    f"🎰 {member.mention} Word Pot room: {ch.mention}",
                    allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False)
                )

        # Tidy up: remove the user's reaction so others can easily click too
        try:
            msg = await channel.fetch_message(payload.message_id)
            await msg.remove_reaction(payload.emoji, member)
        except Exception:
            pass

        # 🔄 Refresh the panel embed (leave existing buttons/view intact)
        try:
            msg = await channel.fetch_message(payload.message_id)
            new_emb = await _build_dailies_embed(guild.id, member.id)
            await msg.edit(embed=new_emb)
        except Exception:
            pass

    except Exception as e:
        log.warning(f"[dailies] reaction handler error: {e}")


      

@tree.command(name="dailies", description="Show your daily actions.")
async def dailies(interaction: discord.Interaction):
    emb = await _build_dailies_embed(interaction.guild.id, interaction.user.id)
    pot_amount = await get_word_pot_total(interaction.guild.id)

    view = DailiesView(interaction, pot_amount=pot_amount)
    await interaction.response.send_message(embed=emb, view=view)

    try:
        msg = await interaction.original_response()
        view.attach_message(msg)
        dailies_msg_ids.add(msg.id)  # 👈 add this
    except Exception:
        pass









async def _find_bounty_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    cfg = await get_cfg(guild.id)
    if cfg["bounty_channel_id"]:
        ch = guild.get_channel(cfg["bounty_channel_id"])
        if isinstance(ch, discord.TextChannel) and ch.permissions_for(guild.me).send_messages:
            return ch
    if guild.system_channel and guild.system_channel.permissions_for(guild.me).send_messages:
        return guild.system_channel
    for ch in guild.text_channels:
        if ch.permissions_for(guild.me).send_messages:
            return ch
    return None

def _bounty_emoji_matches(emoji: discord.PartialEmoji) -> bool:
    target_name = (EMO_BOUNTY_NAME or "ww_bounty").lower()
    if emoji.is_unicode_emoji():
        return emoji.name == "🎯"
    return (emoji.name or "").lower() == target_name

async def _post_bounty_prompt(guild: discord.Guild, channel: discord.TextChannel, hour_idx: int):
    if guild.id in pending_bounties or guild.id in bounty_games:
        return False

    cfg = await get_cfg(guild.id)
    suppress_ping = int(cfg.get("suppress_bounty_ping", 0)) == 1

    rid = await ensure_bounty_role(guild)
    em = EMO_BOUNTY()
    role_mention = "" if suppress_ping else (f"<@&{rid}>" if rid else "")

    desc = (
      f"React with {em} to **arm** this bounty — need **2** players.\n"
      f"**After 2 react, the bounty arms in {BOUNTY_ARM_DELAY_S//60} minute.**\n"
      f"**Prize:** {BOUNTY_PAYOUT} {EMO_SHEKEL()}\n"
      "Use `bg APPLE` or `/worldle_bounty_guess` when armed.\n\n"
      f"⏲️ *This prompt expires in {BOUNTY_EXPIRE_MIN} minutes.*"
  )

    emb = make_panel(title=f"{em} Hourly Bounty (GMT)", description=desc)

    # Use content for the role ping so it actually notifies
    msg = await safe_send(
        channel,
        content=role_mention or None,
        embed=emb,
        allowed_mentions=discord.AllowedMentions(users=False, roles=(not suppress_ping), everyone=False),
    )

    try:
        await msg.add_reaction(em)
    except Exception:
        try: await msg.add_reaction("🎯")
        except Exception: pass

    pending_bounties[guild.id] = {
        "message_id": msg.id,
        "channel_id": channel.id,
        "users": set(),
        "hour_idx": hour_idx,
        "expires_at": gmt_now_s() + BOUNTY_EXPIRE_S,
    }
    await set_cfg(guild.id, last_bounty_hour=hour_idx)
    return True






async def _start_bounty_after_gate(guild: discord.Guild, channel_id: int):
    if guild.id in bounty_games:
        return
    answer = random.choice(ANSWERS)
    bounty_games[guild.id] = {
        "answer": answer,
        "channel_id": channel_id,
        "started_at": gmt_now_s(),
        "expires_at": gmt_now_s() + BOUNTY_EXPIRE_S,
    }
    await set_cfg(guild.id, last_bounty_ts=gmt_now_s(), suppress_bounty_ping=0)  # re-enable pings
    ch = guild.get_channel(channel_id)
    if isinstance(ch, discord.TextChannel):
        emb = make_panel(
            title="🎯 Bounty armed!",
            description=(
                f"First to solve in **{BOUNTY_EXPIRE_MIN} minutes** wins **{BOUNTY_PAYOUT} {EMO_SHEKEL()}**.\n"
                "Use `bg WORD` or `/worldle_bounty_guess`."
            ),
        )
        await safe_send(ch, embed=emb)








@tree.command(name="worldle_bounty_setchannel", description="(Admin) Set this channel for bounty drops.")
@app_commands.default_permissions(administrator=True)
async def worldle_bounty_setchannel(inter: discord.Interaction):
    if not inter.guild or not inter.channel: return await inter.response.send_message("Server only.", ephemeral=True)
    await set_cfg(inter.guild.id, bounty_channel_id=inter.channel.id)
    await inter.response.send_message(f"✅ Set bounty channel to {inter.channel.mention}.")

@tree.command(name="worldle_bounty_now", description="(Admin) Post a bounty prompt **now** (requires emoji arm).")
@app_commands.default_permissions(administrator=True)
async def worldle_bounty_now(inter: discord.Interaction):
    if not inter.guild or not inter.channel: return await inter.response.send_message("Server only.", ephemeral=True)
    await inter.response.defer(thinking=False)
    if inter.guild.id in bounty_games:
        return await inter.followup.send("There is already an active bounty.")
    if inter.guild.id in pending_bounties:
        return await inter.followup.send("There is already a pending bounty prompt.")
    ch = await _find_bounty_channel(inter.guild)
    if not ch:
        return await inter.followup.send("I can't find a channel I can speak in.")
    ok = await _post_bounty_prompt(inter.guild, ch, current_hour_index_gmt())
    await inter.followup.send("🎯 Bounty prompt posted — needs 2 reactions to arm." if ok else "Couldn't post a bounty prompt.")

@tree.command(name="worldle_bounty_guess", description="Guess the active bounty word.")
@app_commands.describe(word="Your 5-letter guess")
async def worldle_bounty_guess(inter: discord.Interaction, word: str):
    if not await guard_worldler_inter(inter): return
    if not inter.guild or not inter.channel: return
    game = bounty_games.get(inter.guild.id)
    if not game:
        return await inter.response.send_message("No active bounty right now.", ephemeral=True)
    if inter.channel.id != game["channel_id"]:
        ch = inter.guild.get_channel(game["channel_id"])
        return await inter.response.send_message(f"Use this in {ch.mention if ch else 'the bounty channel'}.", ephemeral=True)

    # NEW: per-user cooldown
    now_s = gmt_now_s()
    key = (inter.guild.id, inter.user.id)
    last = last_bounty_guess_ts.get(key, 0)
    delta = now_s - last
    if delta < BOUNTY_GUESS_COOLDOWN_S:
        wait = int(BOUNTY_GUESS_COOLDOWN_S - delta)
        return await inter.response.send_message(
            f"Slow down — **{wait}s** cooldown between guesses.", ephemeral=True
        )

    cleaned = "".join(ch for ch in word.lower().strip() if ch.isalpha())
    if len(cleaned) != 5:
        return await inter.response.send_message("Guess must be exactly 5 letters.", ephemeral=True)
    if not is_valid_guess(cleaned):
        return await inter.response.send_message("That’s not in the Wordle dictionary (UK variants supported).", ephemeral=True)

    # Start cooldown now that we accepted a valid guess
    last_bounty_guess_ts[key] = now_s

    colors = score_guess(cleaned, game["answer"])
    row = render_row(cleaned, colors)

    # live feedback in the bounty channel
    await inter.response.send_message(row)

    if cleaned == game["answer"]:
        gid, uid = inter.guild.id, inter.user.id
        await change_balance(gid, uid, BOUNTY_PAYOUT, announce_channel_id=game["channel_id"])
        await inc_stat(gid, uid, "bounties_won", 1)
        bal = await get_balance(gid, uid)

        # capture & clear
        ans_raw = game["answer"]
        ans_up = ans_raw.upper()
        del bounty_games[gid]

        # small confirmation in-channel
        await inter.followup.send(
            f"🏆 {inter.user.mention} solved the Bounty Wordle (**{ans_up}**) and wins **{BOUNTY_PAYOUT} {EMO_SHEKEL()}**! (Balance: {bal})"
        )

        # definition + neat card in announcements
        definition = await fetch_definition(ans_raw)
        fields = []
        if definition:
            fields.append(("Definition", definition, False))
        fields.append(("Result", row, False))  # emojis render

        emb = make_card(
            title="🎯 Hourly Bounty — Solved",
            description=f"{inter.user.mention} wins **{BOUNTY_PAYOUT} {EMO_SHEKEL()}** by solving **{ans_up}**.",
            fields=fields,
            color=CARD_COLOR_SUCCESS,
        )
        await _announce_result(inter.guild, origin_cid=None, content="", embed=emb)
    else:
        await inter.followup.send("(Keep trying! Unlimited guesses.)")






@tasks.loop(seconds=20)
async def bounty_loop():
    now = gmt_now_s()
    hour_idx = current_hour_index_gmt()
    within_window = (now % 3600) < 40

    # 1) Expire pending (not armed) prompts
    for gid, pend in list(pending_bounties.items()):
        try:
            if now >= pend.get("expires_at", 0):
                pending_bounties.pop(gid, None)
                guild = discord.utils.get(bot.guilds, id=gid)
                if not guild:
                    continue
                ch = guild.get_channel(pend["channel_id"])

                # Suppress next-hour bounty ping
                try:
                    await set_cfg(guild.id, suppress_bounty_ping=1)
                except Exception:
                    pass

                # +1 to Word Pot
                pot = await get_casino_pot(gid)
                new_pot = pot + 1
                await set_casino_pot(gid, new_pot)

                if isinstance(ch, discord.TextChannel):
                    emb = make_panel(
                        title="⏲️ Bounty prompt expired",
                        description=f"+1 {EMO_SHEKEL()} to **Word Pot** (now **{new_pot}**).",
                    )
                    try:
                        msg = await ch.fetch_message(pend["message_id"])
                        await msg.reply(embed=emb)
                    except Exception:
                        await safe_send(ch, embed=emb)
        except Exception as e:
            log.warning(f"bounty_loop pending expiry error (guild {gid}): {e}")

    # 1.5) NEW: Arm any prompts whose countdown finished
    for gid, pend in list(pending_bounties.items()):
        try:
            arm_at = pend.get("arming_at")
            if arm_at and now >= arm_at and len(pend.get("users", set())) >= 2:
                guild = discord.utils.get(bot.guilds, id=gid)
                if not guild:
                    continue
                ch = guild.get_channel(pend["channel_id"])
                if isinstance(ch, discord.TextChannel):
                    try:
                        msg = await ch.fetch_message(pend["message_id"])
                        await msg.reply("🔔 **Arming now!**")
                    except Exception:
                        await safe_send(ch, "🔔 **Arming now!**")

                channel_id = pend["channel_id"]
                pending_bounties.pop(gid, None)
                await _start_bounty_after_gate(guild, channel_id)
        except Exception as e:
            log.warning(f"bounty_loop arming error (guild {gid}): {e}")

    # 2) Expire ARMED bounties
    for gid, game in list(bounty_games.items()):
        try:
            if now >= game.get("expires_at", 0):
                bounty_games.pop(gid, None)
                guild = discord.utils.get(bot.guilds, id=gid)
                if not guild:
                    continue
                ch = guild.get_channel(game["channel_id"])

                # Suppress next-hour ping
                try:
                    await set_cfg(guild.id, suppress_bounty_ping=1)
                except Exception:
                    pass

                # +1 to Word Pot
                pot = await get_casino_pot(gid)
                new_pot = pot + 1
                await set_casino_pot(gid, new_pot)

                if isinstance(ch, discord.TextChannel):
                    emb = make_panel(
                        title="⏲️ Bounty expired",
                        description=(
                            f"No solve in **{BOUNTY_EXPIRE_MIN} minutes**.\n"
                            f"+1 {EMO_SHEKEL()} to **Word Pot** (now **{new_pot}**)."
                        ),
                    )
                    await safe_send(ch, embed=emb)
        except Exception as e:
            log.warning(f"bounty_loop active expiry error (guild {gid}): {e}")

    # 3) Drop a NEW bounty prompt this hour
    for guild in bot.guilds:
        try:
            if guild.id in bounty_games or guild.id in pending_bounties:
                continue
            cfg = await get_cfg(guild.id)
            if cfg.get("last_bounty_hour", 0) == hour_idx:
                continue
            if not within_window:
                continue
            ch = await _find_bounty_channel(guild)
            if not ch:
                continue
            await _post_bounty_prompt(guild, ch, hour_idx)
        except Exception as e:
            log.warning(f"bounty loop error {guild.id}: {e}")






@bounty_loop.before_loop
async def _before_bounty_loop():
    await bot.wait_until_ready()

# -------------------- Reactions: bounty + dungeon (FULL) --------------------
@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    gid = payload.guild_id
    if gid is None or (bot.user and payload.user_id == bot.user.id):
        return
    guild = discord.utils.get(bot.guilds, id=gid)
    if not guild:
        return

    # --- DAILIES PANEL HOOK (keep) ---
    try:
        await dailies_raw_reaction_add(payload)
    except Exception as e:
        log.warning(f"[dailies] reaction proxy error: {e}")
    # ---------------------------------

    # ---------- BOUNTY: gate (boxed + edits) ----------
    pend = pending_bounties.get(gid)
    if pend and payload.message_id == pend["message_id"] and _bounty_emoji_matches(payload.emoji):
        try:
            member = guild.get_member(payload.user_id) or await guild.fetch_member(payload.user_id)
        except Exception:
            member = None
        if member and not member.bot and await is_worldler(guild, member):
            pend["users"].add(member.id)

            # Build a neat player list
            names = []
            for uid in sorted(pend["users"]):
                try:
                    m = guild.get_member(uid) or await guild.fetch_member(uid)
                    names.append(m.mention if m else f"<@{uid}>")
                except Exception:
                    names.append(f"<@{uid}>")
            players_txt = ", ".join(names) if names else "—"

            # Edit the original gate card to show who’s in
            try:
                ch = guild.get_channel(pend["channel_id"])
                if isinstance(ch, discord.TextChannel):
                    msg = await ch.fetch_message(pend["message_id"])
                    desc = (
                        f"React with {EMO_BOUNTY()} to **arm** this bounty — need **2** players.\n"
                        f"After 2 react, the bounty **arms in {BOUNTY_ARM_DELAY_S//60} minute**.\n"
                        f"**Prize:** {BOUNTY_PAYOUT} {EMO_SHEKEL()}\n"
                        "Use `bg APPLE` or `/worldle_bounty_guess` when armed.\n\n"
                        f"⏲️ This prompt expires in {BOUNTY_EXPIRE_MIN} minutes."
                    )
                    emb = make_panel(
                        title=f"{EMO_BOUNTY()} Hourly Bounty (GMT)",
                        description=desc,
                        fields=[("Players ready", players_txt, False)],
                    )
                    await msg.edit(embed=emb)
            except Exception:
                pass

            # If we just reached 2 players, start the arming countdown (and box the notice)
            if len(pend["users"]) >= 2 and not pend.get("arming_at"):
                pend["arming_at"] = gmt_now_s() + BOUNTY_ARM_DELAY_S
                try:
                    ch = guild.get_channel(pend["channel_id"])
                    await send_boxed(
                        ch, "Bounty", f"✅ Armed by {', '.join(names[:2])}. **Arming in {BOUNTY_ARM_DELAY_S//60} minute…**",
                        icon="🎯"
                    )
                except Exception:
                    pass
        return

    # ---------- DUNGEON: join gate (boxed + edits only) ----------
    gate = pending_dungeon_gates_by_msg.get(payload.message_id)
    if gate and _dungeon_join_emoji_matches(payload.emoji):
        try:
            member = guild.get_member(payload.user_id) or await guild.fetch_member(payload.user_id)
        except Exception:
            member = None
        if member and (not member.bot) and await is_worldler(guild, member):
            # Track participant
            gate["participants"].add(member.id)

            # Grant write access to the dungeon channel
            dch = guild.get_channel(gate["dungeon_channel_id"])
            if isinstance(dch, discord.TextChannel):
                try:
                    await dch.set_permissions(member, view_channel=True, send_messages=True, read_message_history=True)
                except Exception:
                    pass
                # Mirror into the dungeon game object
                g = dungeon_games.get(dch.id)
                if g:
                    g["participants"].add(member.id)
                gmsg_id = g.get("welcome_msg_id") if g else None
                if gmsg_id:
                    # Edit the in-room welcome to show all participants (no new lines)
                    try:
                        msg = await dch.fetch_message(gmsg_id)
                        names = []
                        for uid in sorted(g["participants"]):
                            try:
                                mm = guild.get_member(uid) or await guild.fetch_member(uid)
                                names.append(mm.mention if mm else f"<@{uid}>")
                            except Exception:
                                names.append(f"<@{uid}>")
                        await msg.edit(content=(
                            f"🌀 **Dungeon — Tier {g['tier']}**\n"
                            f"Participants: {', '.join(names)}\n\n"
                            "When ready, the **owner** clicks 🔒 to start."
                        ))
                    except Exception:
                        pass

            # Edit the original *gate* message (where players click) to include the live roster
            try:
                gate_ch = guild.get_channel(gate["gate_channel_id"])
                if isinstance(gate_ch, discord.TextChannel):
                    jmsg = await gate_ch.fetch_message(payload.message_id)
                    names = []
                    for uid in sorted(gate["participants"]):
                        try:
                            mm = guild.get_member(uid) or await guild.fetch_member(uid)
                            names.append(mm.mention if mm else f"<@{uid}>")
                        except Exception:
                            names.append(f"<@{uid}>")
                    emb = make_panel(
                        title=f"{EMO_DUNGEON()} Dungeon Gate — Tier {gate['tier']}",
                        description=(
                            "Click the swirl below to **join**. "
                            "When everyone’s in, the **owner** will lock the dungeon from inside to begin."
                        ),
                        fields=[("Participants", ", ".join(names) if names else "—", False)],
                        icon="🌀",
                    )
                    # Switch to an embed (boxed); keep the reaction on the same message
                    await jmsg.edit(content=None, embed=emb)
            except Exception:
                pass
        return

    # ---------- DUNGEON: owner locks 🔒 to start ----------
    for ch_id, game in list(dungeon_games.items()):
        if payload.message_id == game.get("welcome_msg_id") and _lock_emoji_matches(payload.emoji):
            if payload.user_id != game.get("owner_id"):
                return
            mid = game.get("gate_msg_id")
            if mid in pending_dungeon_gates_by_msg:
                pending_dungeon_gates_by_msg.pop(mid, None)
            ch = guild.get_channel(ch_id)
            if isinstance(ch, discord.TextChannel):
                await send_boxed(ch, "Dungeon", "🔒 **Gate closed.** No further joins. The dungeon begins!", icon="🌀")
                # public announcement
                try:
                    await _announce_result(
                        guild,
                        game.get("origin_cid"),
                        f"{EMO_DUNGEON()} **Dungeon gate closed** — Tier {game.get('tier')} has **started** in {ch.mention}. Good luck, adventurers!"
                    )
                except Exception:
                    pass
            await _dungeon_start_round(game)
            return

    # ---------- DUNGEON: owner decision (⏩ continue / 💰 cash out) ----------
    for ch_id, game in list(dungeon_games.items()):
        if payload.message_id == game.get("decision_msg_id") and game.get("state") == "await_decision":
            if payload.user_id != game.get("owner_id"):
                return
            ch = guild.get_channel(ch_id)
            if _continue_emoji_matches(payload.emoji):
                game["decision_msg_id"] = None
                if isinstance(ch, discord.TextChannel):
                    await send_boxed(ch, "Dungeon", "⏩ **Continuing…**", icon="🌀")
                await _dungeon_start_round(game)
                return
            if _cashout_emoji_matches(payload.emoji):
                pool = max(0, game.get("pool", 0))
                await _dungeon_settle_and_close(game, pool, note="💰 **Cashed out in time.**")
                return




# -------------------- /dungeon (UI entry) --------------------

@tree.command(name="dungeon", description="Dungeon book: tiers, lore, loot, and Enter button.")
async def dungeon_ui(inter: discord.Interaction):
    if not await guard_worldler_inter(inter):
        return
    view = DungeonView(inter, start_tier=3)
    emb = await _build_dungeon_embed(inter.guild.id, inter.user.id, "t3")
    await inter.response.send_message(embed=emb, view=view)
    try:
        view.message = await inter.original_response()
    except Exception:
        pass
    await view.refresh_page()  # <-- set button label/state now




@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    """Bounty opt-out: update the gate card (boxed) and cancel arming if we fall below 2."""
    gid = payload.guild_id
    if gid is None:
        return

    pend = pending_bounties.get(gid)
    if not pend or payload.message_id != pend.get("message_id") or not _bounty_emoji_matches(payload.emoji):
        return

    # Remove the user from the pending set (if present)
    uid = payload.user_id
    if uid and uid in pend.get("users", set()):
        pend["users"].discard(uid)

        guild = discord.utils.get(bot.guilds, id=gid)
        if not guild:
            return

        # If countdown was running but we dropped below 2, cancel it (boxed)
        if pend.get("arming_at") and len(pend["users"]) < 2:
            pend["arming_at"] = None
            try:
                ch = guild.get_channel(pend["channel_id"])
                await send_boxed(ch, "Bounty", "⏹️ Arming **cancelled** — need 2 players again.", icon="🎯")
            except Exception:
                pass

        # Edit the original gate embed to reflect the new roster
        try:
            ch = guild.get_channel(pend["channel_id"])
            if isinstance(ch, discord.TextChannel):
                msg = await ch.fetch_message(pend["message_id"])
                names = []
                for id_ in sorted(pend["users"]):
                    try:
                        m = guild.get_member(id_) or await guild.fetch_member(id_)
                        names.append(m.mention if m else f"<@{id_}>")
                    except Exception:
                        names.append(f"<@{id_}>")
                players_txt = ", ".join(names) if names else "—"
                desc = (
                    f"React with {EMO_BOUNTY()} to **arm** this bounty — need **2** players.\n"
                    f"After 2 react, the bounty **arms in {BOUNTY_ARM_DELAY_S//60} minute**.\n"
                    f"**Prize:** {BOUNTY_PAYOUT} {EMO_SHEKEL()}\n"
                    "Use `bg APPLE` or `/worldle_bounty_guess` when armed.\n\n"
                    f"⏲️ This prompt expires in {BOUNTY_EXPIRE_MIN} minutes."
                )
                emb = make_panel(
                    title=f"{EMO_BOUNTY()} Hourly Bounty (GMT)",
                    description=desc,
                    fields=[("Players ready", players_txt, False)],
                )
                await msg.edit(embed=emb)
        except Exception:
            pass






# -------------------- DUNGEON tickets (T1/T2/T3) --------------------
async def get_dungeon_tickets_t1(gid: int, uid: int) -> int:
    async with bot.db.execute("SELECT dungeon_tickets_t1 FROM inv WHERE guild_id=? AND user_id=?", (gid, uid)) as cur:
        row = await cur.fetchone()
    return row[0] if row else 0

async def change_dungeon_tickets_t1(gid: int, uid: int, delta: int):
    await bot.db.execute("""
      INSERT INTO inv(guild_id,user_id,dungeon_tickets_t1) VALUES(?,?,?)
      ON CONFLICT(guild_id,user_id) DO UPDATE SET dungeon_tickets_t1=inv.dungeon_tickets_t1+excluded.dungeon_tickets_t1
    """, (gid, uid, delta))
    await bot.db.commit()

async def get_dungeon_tickets_t2(gid: int, uid: int) -> int:
    async with bot.db.execute("SELECT dungeon_tickets_t2 FROM inv WHERE guild_id=? AND user_id=?", (gid, uid)) as cur:
        row = await cur.fetchone()
    return row[0] if row else 0

async def change_dungeon_tickets_t2(gid: int, uid: int, delta: int):
    await bot.db.execute("""
      INSERT INTO inv(guild_id,user_id,dungeon_tickets_t2) VALUES(?,?,?)
      ON CONFLICT(guild_id,user_id) DO UPDATE SET dungeon_tickets_t2=inv.dungeon_tickets_t2+excluded.dungeon_tickets_t2
    """, (gid, uid, delta))
    await bot.db.commit()

async def get_dungeon_tickets_t3(gid: int, uid: int) -> int:
    async with bot.db.execute("SELECT dungeon_tickets_t3 FROM inv WHERE guild_id=? AND user_id=?", (gid, uid)) as cur:
        row = await cur.fetchone()
    return row[0] if row else 0

async def change_dungeon_tickets_t3(gid: int, uid: int, delta: int):
    await bot.db.execute("""
      INSERT INTO inv(guild_id,user_id,dungeon_tickets_t3) VALUES(?,?,?)
      ON CONFLICT(guild_id,user_id) DO UPDATE SET dungeon_tickets_t3=inv.dungeon_tickets_t3+excluded.dungeon_tickets_t3
    """, (gid, uid, delta))
    await bot.db.commit()

# -------------------- DUNGEON channel factory --------------------
async def _make_dungeon_channel(invocation_channel: discord.TextChannel, owner: discord.Member) -> Optional[discord.TextChannel]:
    guild = invocation_channel.guild
    me = guild.me
    if not me or not me.guild_permissions.manage_channels:
        await invocation_channel.send("I need **Manage Channels** to open the dungeon.", delete_after=20)
        return None

    cfg = await get_cfg(guild.id)
    rid = cfg["worldler_role_id"] or await ensure_worldler_role(guild)
    worldler_role = guild.get_role(rid) if rid else None

    category = guild.get_channel(cfg["solo_category_id"]) if cfg.get("solo_category_id") else None
    if category and not isinstance(category, discord.CategoryChannel):
        category = None

    base = re.sub(r"[^a-zA-Z0-9]+", "-", owner.display_name).strip("-").lower() or f"user-{owner.id}"
    base = f"{base}-dungeon"
    name = base
    i = 2
    while discord.utils.get(guild.text_channels, name=name):
        name = f"{base}-{i}"; i += 1

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False, mention_everyone=False),
        owner: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, mention_everyone=False),
        me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True, mention_everyone=False),
    }
    if worldler_role:
        overwrites[worldler_role] = discord.PermissionOverwrite(view_channel=True, send_messages=False, read_message_history=True, mention_everyone=False)

    ch = await guild.create_text_channel(name=name, overwrites=overwrites, category=category, reason="Wordle Dungeon")
    return ch


# -------------------- DUNGEON logic --------------------
def _dungeon_max_for_tier(tier: int) -> int:
    return 5 if tier == 3 else 4 if tier == 2 else 3  # T3=5, T2=4, T1=3

def _dungeon_mult_for_tier(tier: int) -> int:
    return 1 if tier == 3 else 2 if tier == 2 else 3  # T3 base, T2 double, T1 triple

def _dungeon_new_answer() -> str:
    return random.choice(ANSWERS)

async def _dungeon_settle_and_close(game: dict, payout_each: int, note: str):
    gid = game["guild_id"]
    ch_id = game["channel_id"]
    tier = game.get("tier", "?")
    origin_cid = game.get("origin_cid")
    part_ids = sorted(game.get("participants", set()))
    num_parts = len(part_ids)

    ch = discord.utils.get(bot.get_all_channels(), id=ch_id)
    guild = ch.guild if isinstance(ch, discord.TextChannel) else discord.utils.get(bot.guilds, id=gid)

    # pay
    for uid in part_ids:
        try:
            await change_balance(gid, uid, payout_each, announce_channel_id=ch_id)
        except Exception:
            pass

    # participants (mentions)
    names = []
    if guild:
        for uid in part_ids:
            try:
                m = guild.get_member(uid) or await guild.fetch_member(uid)
                names.append(m.mention if m else f"<@{uid}>")
            except Exception:
                names.append(f"<@{uid}>")
    names_txt = ", ".join(names) if names else f"{num_parts} adventurer(s)"

    solved_list = game.get("solved_rounds", [])
    solved_cnt = len(solved_list)
    solved_block = "—" if not solved_list else "\n".join(f"• **{w}**" for w in solved_list)

    # In-channel wrap-up
    if isinstance(ch, discord.TextChannel):
        emb = make_panel(
            title=f"Dungeon Finished — Tier {tier}",
            description=note,
            icon="🧱",
            fields=[
                ("Participants", names_txt, False),
                ("Rewards", f"**{payout_each}** {EMO_SHEKEL()} each · Pool: **{max(0, game.get('pool',0))}**", False),
                (f"Rounds solved ({solved_cnt})", solved_block, False),
            ]
        )
        try:
            await ch.send(embed=emb)
        except Exception:
            pass

    # public announcement (same panel) to announcements channel
    if guild:
        summary_emb = make_panel(
            title=f"Dungeon Finished — Tier {tier}",
            description=note,
            icon="🧱",
            fields=[
                ("Participants", names_txt, False),
                ("Rewards", f"**{payout_each}** {EMO_SHEKEL()} each · Pool: **{max(0, game.get('pool',0))}**", False),
                (f"Rounds solved ({solved_cnt})", solved_block, False),
            ]
        )
        try:
            await _announce_result(guild, origin_cid, "",)  # ensure channel chosen
            # send directly to announcements channel using _announce_result's channel picking
            # (empty content makes _announce_result post only the board/body; we will send panel ourselves)
            cfg = await get_cfg(guild.id)
            ann_id = cfg.get("announcements_channel_id")
            if ann_id:
                ann_ch = guild.get_channel(ann_id)
                if isinstance(ann_ch, discord.TextChannel):
                    await safe_send(ann_ch, embed=summary_emb)
        except Exception:
            pass

    dungeon_games.pop(ch_id, None)
    for mid, g in list(pending_dungeon_gates_by_msg.items()):
        if g.get("dungeon_channel_id") == ch_id:
            pending_dungeon_gates_by_msg.pop(mid, None)

    if isinstance(ch, discord.TextChannel):
        try:
            await ch.delete(reason="Dungeon closed")
        except Exception:
            pass







async def _dungeon_start_round(game: dict):
    # keep cumulative list across rounds
    game.setdefault("solved_rounds", [])
    game["answer"] = _dungeon_new_answer()
    game["guesses"] = []
    game["legend"] = {}
    game["max"] = _dungeon_max_for_tier(game["tier"])
    game["state"] = "active"

    ch = discord.utils.get(bot.get_all_channels(), id=game["channel_id"])
    if isinstance(ch, discord.TextChannel):
        await send_boxed(
            ch,
            "Dungeon — New Wordle",
            f"Tier **{game['tier']}** — you have **{game['max']} tries**.\nGuess with `g APPLE` here.",
            icon="🌀",
        )
        blank_board = render_board([], total_rows=game["max"])
        await ch.send(blank_board)  # board plain






async def dungeon_guess(channel: discord.TextChannel, author: discord.Member, word: str):
    ch_id = channel.id
    game = dungeon_games.get(ch_id)
    if not game or game.get("state") not in ("active",):
        await send_boxed(channel, "Dungeon", "No active dungeon round right now.", icon="🌀")
        return
    if author.id not in game["participants"]:
        await send_boxed(channel, "Dungeon", f"{author.mention} you're not registered for this dungeon.", icon="🌀")
        return

    cleaned = "".join(ch for ch in word.lower().strip() if ch.isalpha())
    if len(cleaned) != 5:
        await send_boxed(channel, "Invalid Guess", "Guess must be **exactly 5 letters**.", icon="❗")
        return
    if not is_valid_guess(cleaned):
        await send_boxed(channel, "Invalid Guess", "That’s not in the Wordle dictionary (UK variants supported).", icon="📚")
        return
    if len(game["guesses"]) >= game["max"]:
        await send_boxed(channel, "Dungeon", "Out of tries for this round.", icon="🌀")
        return

    colors = score_guess(cleaned, game["answer"])
    game["guesses"].append({"word": cleaned, "colors": colors})
    update_legend(game["legend"], cleaned, colors)

    board = render_board(game["guesses"], total_rows=game["max"])
    await safe_send(channel, board)  # board plain

    attempt = len(game["guesses"])
    if cleaned == game["answer"]:
        base = payout_for_attempt(attempt)
        gained = base * _dungeon_mult_for_tier(game["tier"])
        game["pool"] = game.get("pool", 0) + gained

        # record solved word (UPPER)
        try:
            game.setdefault("solved_rounds", []).append(game["answer"].upper())
        except Exception:
            pass

        # Loot: chances remain the same
        loot_msgs = []
        if random.random() < 0.40:
            await change_stones(game["guild_id"], author.id, 1)
            loot_msgs.append(f"+1 {EMO_STONE()}")
        if game["tier"] == 3 and random.random() < 0.10:
            await change_dungeon_tickets_t2(game["guild_id"], author.id, 1)
            loot_msgs.append("+1 Ticket (Tier 2)")
        elif game["tier"] == 2 and random.random() < 0.10:
            await change_dungeon_tickets_t1(game["guild_id"], author.id, 1)
            loot_msgs.append("+1 Ticket (Tier 1)")

        legend = legend_overview(game["legend"])
        extra = f" 🎁 Loot: {' · '.join(loot_msgs)}" if loot_msgs else ""
        fields = [("Pool", f"Added **+{gained} {EMO_SHEKEL()}** (now **{game['pool']}**).", True)]
        if legend:
            fields.append(("Legend", legend, False))
        await send_boxed(
            channel,
            f"✅ Solved on attempt {attempt}!",
            f"**Owner**: react **⏩** to **Continue** or **💰** to **Cash Out** for everyone.{extra}",
            icon="🌀",
            fields=fields,
        )
        msg = await safe_send(channel, "⏩ Continue or 💰 Cash Out?")
        try:
            await msg.add_reaction("⏩")
            await msg.add_reaction("💰")
        except Exception:
            pass
        game["decision_msg_id"] = msg.id
        game["state"] = "await_decision"
        return

    if attempt == game["max"]:
        from math import ceil
        half_each = ceil(max(0, game.get("pool", 0)) / 2)
        await _dungeon_settle_and_close(game, half_each, note="❌ Round failed; reward halved (rounded up).")
        return

    next_attempt = attempt + 1
    payout = payout_for_attempt(next_attempt) * _dungeon_mult_for_tier(game["tier"])
    hint = legend_overview(game["legend"])
    flds = [("Next", f"Attempt **{attempt}/{game['max']}** — Solve on attempt **{next_attempt}** to add **+{payout}** to the pool.", False)]
    if hint:
        flds.append(("Legend", hint, False))
    await send_boxed(channel, "Dungeon — Status", "", icon="🌀", fields=flds)







@tree.command(name="worldle_dungeon", description="Open a Worldle Dungeon (Tier 1/2/3).")
@app_commands.describe(tier="Dungeon tier")
@app_commands.choices(tier=[
    app_commands.Choice(name="Tier 1 (triple rewards · 3 tries)", value=1),
    app_commands.Choice(name="Tier 2 (double rewards · 4 tries)", value=2),
    app_commands.Choice(name="Tier 3 (base rewards · 5 tries)",   value=3),
])
async def worldle_dungeon_open(inter: discord.Interaction, tier: app_commands.Choice[int]):
    if not await guard_worldler_inter(inter): return
    if not inter.guild or not inter.channel: return
    gid, uid = inter.guild.id, inter.user.id
    t = tier.value

    # Check ticket ownership
    if t == 3:
        if await get_dungeon_tickets_t3(gid, uid) < 1:
            return await inter.response.send_message(
                f"You need a **{EMO_DUNGEON()} Dungeon Ticket (Tier 3)**. Buy it in `/shop`.", ephemeral=True
            )
    elif t == 2:
        if await get_dungeon_tickets_t2(gid, uid) < 1:
            return await inter.response.send_message("You need a **Tier 2 Dungeon Ticket** (loot from Tier 3).", ephemeral=True)
    else:
        if await get_dungeon_tickets_t1(gid, uid) < 1:
            return await inter.response.send_message("You need a **Tier 1 Dungeon Ticket** (loot from Tier 2).", ephemeral=True)

    await inter.response.defer(thinking=False)

    # Consume ticket
    if t == 3:   await change_dungeon_tickets_t3(gid, uid, -1)
    elif t == 2: await change_dungeon_tickets_t2(gid, uid, -1)
    else:        await change_dungeon_tickets_t1(gid, uid, -1)

    # Create dungeon channel
    ch = await _make_dungeon_channel(inter.channel, inter.user)
    if not ch:
        # refund on failure to create the channel
        if t == 3:   await change_dungeon_tickets_t3(gid, uid, +1)
        elif t == 2: await change_dungeon_tickets_t2(gid, uid, +1)
        else:        await change_dungeon_tickets_t1(gid, uid, +1)
        return await inter.followup.send("Couldn't create the dungeon channel (ticket refunded).")

    # Register game
    dungeon_games[ch.id] = {
        "guild_id": gid,
        "channel_id": ch.id,
        "owner_id": uid,
        "tier": t,
        "participants": {uid},
        "state": "await_start",
        "answer": None, "guesses": [], "max": _dungeon_max_for_tier(t), "legend": {}, "pool": 0,
        "gate_msg_id": None, "welcome_msg_id": None, "decision_msg_id": None,
        "origin_cid": inter.channel.id,
        "solved_words": [],
    }

    # Gate message in current channel (boxed) with dynamic participants list
    part_txt = f"<@{uid}> (owner)"
    gate_embed = make_panel(
        title=f"{EMO_DUNGEON()} Dungeon Gate (Tier {t})",
        description=(
            f"Click {EMO_DUNGEON()} below **to join**. You’ll gain write access in {ch.mention}.\n"
            f"When ready, the **owner** will **lock** the dungeon from inside to start the game."
        ),
        fields=[("Participants", part_txt, False)],
        icon="🌀",
    )
    join_msg = await inter.channel.send(embed=gate_embed)
    try:
        await join_msg.add_reaction(EMO_DUNGEON())
    except Exception:
        try: await join_msg.add_reaction("🌀")
        except Exception: pass

    pending_dungeon_gates_by_msg[join_msg.id] = {
        "guild_id": gid,
        "gate_channel_id": inter.channel.id,
        "dungeon_channel_id": ch.id,
        "owner_id": uid,
        "participants": {uid},
        "tier": t,
        "state": "gate_open",
    }

    # Spooky welcome in dungeon channel (boxed) with lock control
    welcome_txt = (
        "🕯️ **Welcome, adventurers…**\n"
        "The air is cold and the walls whisper letters you cannot see.\n"
        "Solve quickly or **lose half your spoils** to the shadows.\n\n"
        f"**Tier {t}**: rewards multiplier ×{_dungeon_mult_for_tier(t)}, tries **{_dungeon_max_for_tier(t)}** per Wordle.\n"
        "When everyone has joined, the **owner** must click **🔒** below to seal the gate and begin."
    )
    welcome = await send_boxed(
        ch,
        f"Dungeon — Tier {t}",
        f"Participants: <@{uid}> (owner)\n\n{welcome_txt}",
        icon="🌀",
    )
    # welcome is a Message returned by send_boxed via safe_send path; get id:
    try:
        # Retrieve the actual message to add reaction
        if isinstance(welcome, discord.Message):
            welcome_msg = welcome
        else:
            welcome_msg = await ch.fetch_message(ch.last_message_id)
    except Exception:
        welcome_msg = None

    if welcome_msg:
        try:
            await welcome_msg.add_reaction("🔒")
        except Exception:
            pass

        dungeon_games[ch.id]["welcome_msg_id"] = welcome_msg.id

    dungeon_games[ch.id]["gate_msg_id"] = join_msg.id

    await inter.followup.send(f"Opened {ch.mention} and posted a **join gate** here. Players must react {EMO_DUNGEON()} to join.")




# -------------------- Duels --------------------
def _new_duel_id() -> int:
    global _next_duel_id
    did = _next_duel_id; _next_duel_id += 1; return did

def _duel_in_channel(ch_id: int) -> Optional[int]:
    for k, d in duels.items():
        if d["state"]=="active" and d["channel_id"] == ch_id:
            return k
    return None

@tree.command(name="worldle_challenge", description="Challenge a player to a Wordle duel for a stake.")
@app_commands.describe(user="Opponent", amount="Stake (shekels)")
async def worldle_challenge(inter: discord.Interaction, user: discord.Member, amount: int):
    if not await guard_worldler_inter(inter): return
    if not inter.guild or not inter.channel: return
    if user.bot or user.id == inter.user.id:
        return await inter.response.send_message("Pick a real opponent (not yourself/bots).", ephemeral=True)
    if amount <= 0:
        return await inter.response.send_message("Stake must be positive.", ephemeral=True)

    gid, cid = inter.guild.id, inter.channel.id
    for d in duels.values():
        if d["state"] in ("pending","active") and (d["challenger_id"] in (inter.user.id,user.id) or d["target_id"] in (inter.user.id,user.id)):
            return await inter.response.send_message("Either you or they are already in a pending/active duel.", ephemeral=True)
    if await get_balance(gid, inter.user.id) < amount:
        return await inter.response.send_message("You don't have enough shekels.", ephemeral=True)

    did = _new_duel_id()
    duels[did] = {
        "id": did, "guild_id": gid, "channel_id": cid,
        "challenger_id": inter.user.id, "target_id": user.id,
        "stake": amount, "pot": 0, "state": "pending", "created": time.time(),
        "answer": None, "turn": None,
        "guesses": {inter.user.id: [], user.id: []},
    }
    await inter.response.send_message(
        f"⚔️ Duel **#{did}** created: {inter.user.mention} challenges {user.mention} for **{amount} {EMO_SHEKEL()}**.\n"
        f"{user.mention}, accept with `/worldle_accept id:{did}` or decline with `/worldle_cancel id:{did}`.",
        allowed_mentions=discord.AllowedMentions(users=[inter.user, user])
    )

@tree.command(name="worldle_accept", description="Accept a Wordle duel by ID.")
@app_commands.describe(id="Duel ID")
async def worldle_accept(inter: discord.Interaction, id: int):
    if not await guard_worldler_inter(inter): return
    d = duels.get(id)
    if not d or d["state"] != "pending":
        return await inter.response.send_message("No such pending duel.", ephemeral=True)
    if inter.channel.id != d["channel_id"]:
        ch = inter.guild.get_channel(d["channel_id"])
        return await inter.response.send_message(f"Use this in {ch.mention if ch else 'the duel channel'}.", ephemeral=True)
    if inter.user.id != d["target_id"]:
        return await inter.response.send_message("Only the challenged player can accept.", ephemeral=True)
    if time.time() - d["created"] > 10*60:
        d["state"] = "cancelled"
        return await inter.response.send_message("That duel expired.", ephemeral=True)

    gid, cid = d["guild_id"], d["channel_id"]
    a, b, stake = d["challenger_id"], d["target_id"], d["stake"]
    if await get_balance(gid, a) < stake or await get_balance(gid, b) < stake:
        d["state"] = "cancelled"
        return await inter.response.send_message("One of you no longer has enough shekels. Duel cancelled.", ephemeral=True)

    await change_balance(gid, a, -stake, announce_channel_id=cid)
    await change_balance(gid, b, -stake, announce_channel_id=cid)
    d["pot"] = stake * 2
    d["answer"] = random.choice(ANSWERS)
    d["turn"] = random.choice([a, b])
    d["state"] = "active"

    ch = inter.channel
    starter = f"<@{d['turn']}>"
    await ch.send(
        f"⚔️ Duel **#{id}** started between <@{a}> and <@{b}> for **{stake}** each (**pot {d['pot']} {EMO_SHEKEL()}**).\n"
        f"Starting player chosen at random: {starter} goes first.\n"
        f"Guess with `g APPLE` here or `/worldle_duel_guess id:{id} word:APPLE`."
    )
    await inter.response.send_message("Accepted. Good luck!", ephemeral=True)

@tree.command(name="worldle_duel_guess", description="Play your turn in a Wordle duel.")
@app_commands.describe(id="Duel ID", word="Your 5-letter guess")
async def worldle_duel_guess(inter: discord.Interaction, id: int, word: str):
    if not await guard_worldler_inter(inter): return
    d = duels.get(id)
    if not d or d["state"] != "active":
        return await inter.response.send_message("No such active duel.", ephemeral=True)
    if inter.channel.id != d["channel_id"]:
        ch = inter.guild.get_channel(d["channel_id"])
        return await inter.response.send_message(f"Use this in {ch.mention if ch else 'the duel channel'}.", ephemeral=True)

    uid = inter.user.id
    if uid not in (d["challenger_id"], d["target_id"]):
        return await inter.response.send_message("You're not in that duel.", ephemeral=True)
    if uid != d["turn"]:
        return await inter.response.send_message("It's not your turn.", ephemeral=True)

    cleaned = "".join(ch for ch in word.lower().strip() if ch.isalpha())
    if len(cleaned) != 5:
        return await inter.response.send_message("Guess must be exactly 5 letters.", ephemeral=True)
    if not is_valid_guess(cleaned):
        return await inter.response.send_message("That’s not in the Wordle dictionary (UK variants supported).", ephemeral=True)

    colors = score_guess(cleaned, d["answer"])
    d["guesses"][uid].append({"word": cleaned, "colors": colors})
    row = render_row(cleaned, colors)

    ch = inter.channel
    if cleaned == d["answer"]:
        await ch.send(row)
        await change_balance(d["guild_id"], uid, d["pot"], announce_channel_id=d["channel_id"])
        bal = await get_balance(d["guild_id"], uid)
        await ch.send(f"🏁 Duel **#{id}**: {inter.user.mention} guessed **{d['answer'].upper()}** and wins the pot **{d['pot']} {EMO_SHEKEL()}**! (Balance: {bal})")
        d["state"] = "finished"
        return await inter.response.send_message("You win!", ephemeral=True)

    other = d["challenger_id"] if uid == d["target_id"] else d["target_id"]
    d["turn"] = other
    await ch.send(row)
    await ch.send(f"**Duel #{id}** — It’s now <@{other}>'s turn.")
    await inter.response.send_message("Move submitted.", ephemeral=True)

@tree.command(name="worldle_cancel", description="Cancel your pending duel by ID.")
@app_commands.describe(id="Duel ID")
async def worldle_cancel(inter: discord.Interaction, id: int):
    if not await guard_worldler_inter(inter): return
    d = duels.get(id)
    if not d or d["state"] != "pending":
        return await inter.response.send_message("No such pending duel.", ephemeral=True)
    if inter.channel.id != d["channel_id"]:
        ch = inter.guild.get_channel(d["channel_id"])
        return await inter.response.send_message(f"Use this in {ch.mention if ch else 'the duel channel'}.", ephemeral=True)
    if inter.user.id not in (d["challenger_id"], d["target_id"]):
        return await inter.response.send_message("Only participants can cancel.", ephemeral=True)
    d["state"] = "cancelled"
    await inter.response.send_message("Duel cancelled.", ephemeral=True)

# -------------------- Economy / items --------------------
PRICE_STONE = 1
PRICE_BADGE = 5
PRICE_CHICK = 2
PRICE_SNIPER = 100
SNIPER_SNIPE_COST = 1  # price per snipe shot

# NEW: Tier 3 Dungeon Ticket is purchasable (T2/T1 are loot-only)
SHOP_ITEMS = {
    "stone": {
        "label": f"{EMO_STONE()} Stone",
        "price": PRICE_STONE,
        "desc": "Throw with /stone. 49% drop chance per stone (bulk supported).",
    },
    "badge": {
        "label": f"{EMO_BADGE()} Bounty Hunter Badge",
        "price": PRICE_BADGE,
        "desc": "Grants the Bounty Hunter role. Bounties ping that role.",
    },
    "chicken": {
        "label": f"{EMO_CHICKEN()} Fried Chicken",
        "price": PRICE_CHICK,
        "desc": "Use /eat to gain 1h immunity from stones.",
    },
    "sniper": {
        "label": f"{EMO_SNIPER()} Sniper",
        "price": PRICE_SNIPER,
        "desc": f"Lets you `/snipe` other players' solo Wordle (costs {SNIPER_SNIPE_COST} shekel per shot). One-time purchase.",
    },
    # NEW ITEM
    "ticket_t3": {
        "label": f"{EMO_DUNGEON()} Dungeon Ticket (Tier 3)",
        "price": 5,
        "desc": "Opens a Tier 3 Worldle Dungeon. Use `/worldle_dungeon tier:Tier 3`.",
    },
}

# Controls shop item order & /buy choices
SHOP_ORDER = ["stone", "badge", "chicken", "sniper", "ticket_t3"]



@tree.command(name="shop", description="Show the shop.")
async def shop(inter: discord.Interaction):
    if not await guard_worldler_inter(inter):
        return

    emb = await _build_shop_embed(inter.guild.id, inter.user.id)
    view = ShopView(inter)
    await inter.response.send_message(embed=emb, view=view)

    try:
        msg = await inter.original_response()
        view.message = msg
    except Exception:
        pass








@tree.command(name="buy", description="Buy from the shop.")
@app_commands.describe(item="Item", amount="How many")
@app_commands.choices(item=[app_commands.Choice(name=SHOP_ITEMS[k]["label"], value=k) for k in SHOP_ORDER])
async def buy(inter: discord.Interaction, item: app_commands.Choice[str], amount: int):
    if not await guard_worldler_inter(inter):
        return
    await _shop_perform_buy(inter, item.value, amount)







@tree.command(name="sell", description="Sell items back to the shop for the same price.")
@app_commands.describe(item="Item", amount="How many")
@app_commands.choices(item=[
    app_commands.Choice(name="Stone", value="stone"),
    app_commands.Choice(name="Bounty Hunter Badge", value="badge"),
    app_commands.Choice(name="Fried Chicken", value="chicken"),
    app_commands.Choice(name="Sniper", value="sniper"),
    app_commands.Choice(name="Dungeon Ticket (Tier 3)", value="ticket_t3"),
])
async def sell(inter: discord.Interaction, item: app_commands.Choice[str], amount: int = 1):
    if not await guard_worldler_inter(inter):
        return
    if not inter.guild:
        return await send_boxed(inter, "Shop — Sell", "Server only.", icon="🛍️", ephemeral=False)
    if amount <= 0:
        return await send_boxed(inter, "Shop — Sell", "Amount must be positive.", icon="🛍️", ephemeral=False)

    # Public reply (not ephemeral)
    try:
        await inter.response.defer(thinking=False)
    except Exception:
        pass

    await _shop_perform_sell(inter, item.value, amount)



@tree.command(name="eat", description="Eat a Fried Chicken to gain 1 hour stone immunity.")
@app_commands.describe(amount="How many to eat (each adds 1 hour)")
async def eat(inter: discord.Interaction, amount: int=1):
    if not await guard_worldler_inter(inter): return
    if amount <= 0: return await inter.response.send_message("Amount must be positive.", ephemeral=True)
    gid, uid = inter.guild.id, inter.user.id
    have = await get_chickens(gid, uid)
    if have < amount: return await inter.response.send_message("You don't have that many fried chicken.", ephemeral=True)
    await change_chickens(gid, uid, -amount)
    now = gmt_now_s()
    current = await get_protection_until(gid, uid)
    base = current if current > now else now
    new_until = base + 3600 * amount
    await set_protection_until(gid, uid, new_until)
    mins = (new_until - now) // 60
    await inter.response.send_message(f"{EMO_CHICKEN()} You are protected from stones for **~{mins} minutes**.")

@tree.command(name="inventory", description="See your items.")
async def inventory(inter: discord.Interaction):
    if not await guard_worldler_inter(inter):
        return
    gid, uid = inter.guild.id, inter.user.id
    stones = await get_stones(gid, uid)
    chickens = await get_chickens(gid, uid)
    prot = await get_protection_until(gid, uid)
    sniper = await get_sniper(gid, uid)
    t1 = await get_dungeon_tickets_t1(gid, uid)
    t2 = await get_dungeon_tickets_t2(gid, uid)
    t3 = await get_dungeon_tickets_t3(gid, uid)

    left = max(0, prot - gmt_now_s())
    prot_txt = f" · 🛡️ {left//60}m left" if left>0 else ""
    sniper_owned = "Yes" if sniper else "No"

    body = "\n".join([
        f"• {EMO_STONE()} Stones: **{stones}**",
        f"• {EMO_CHICKEN()} Fried Chicken: **{chickens}**{prot_txt}",
        f"• {EMO_SNIPER()} Sniper: **{sniper_owned}**",
        f"• {EMO_DUNGEON()} Tickets — T1: **{t1}**, T2: **{t2}**, T3: **{t3}**",
    ])
    await send_boxed(inter, "Inventory", body, icon="🎒")



@tree.command(name="badges", description="Show your badges.")
async def badges_cmd(inter: discord.Interaction):
    if not await guard_worldler_inter(inter): return
    gid, uid = inter.guild.id, inter.user.id
    badges = []
    if await get_badge(gid, uid) >= 1:
        badges.append(f"{EMO_BADGE()} **Bounty Hunter Badge** — receive bounty pings")
    body = "You don't have any badges yet." if not badges else "• " + "\n• ".join(badges)
    await send_boxed(inter, "Badges", body, icon="🏅")


@tree.command(name="balance", description="See your shekels.")
async def balance_cmd(inter: discord.Interaction):
    if not await guard_worldler_inter(inter): return
    bal = await get_balance(inter.guild.id, inter.user.id)
    await send_boxed(inter, "Balance", f"**{bal} {EMO_SHEKEL()}**", icon="💰")


@tree.command(name="pray", description="Receive 5 shekels (once per UK day).")
async def pray(inter: discord.Interaction):
    if not await guard_worldler_inter(inter): return
    gid, uid, cid = inter.guild.id, inter.user.id, inter.channel_id
    today = uk_today_str()
    last_pray, _ = await _get_cd(gid, uid)
    if last_pray == today:
        return await send_boxed(inter, "Daily — Pray", "You already prayed today. Resets at **00:00 UK time**.", icon="🛐", ephemeral=True)
    await change_balance(gid, uid, 5, announce_channel_id=cid)
    await _set_cd(gid, uid, "last_pray", today)
    await send_boxed(inter, "Daily — Pray", f"+5 {EMO_SHEKEL()}  · Balance **{await get_balance(gid, uid)}**", icon="🛐")

@tree.command(name="beg", description="Beg for 5 stones (once per UK day).")
async def beg(inter: discord.Interaction):
    if not await guard_worldler_inter(inter): return
    gid, uid = inter.guild.id, inter.user.id
    today = uk_today_str()
    _, last_beg = await _get_cd(gid, uid)
    if last_beg == today:
        return await send_boxed(inter, "Daily — Beg", "You already begged today. Resets at **00:00 UK time**.", icon="🙇", ephemeral=True)
    await change_stones(gid, uid, 5)
    await _set_cd(gid, uid, "last_beg", today)
    stones = await get_stones(gid, uid)
    await send_boxed(inter, "Daily — Beg", f"{EMO_STONE()} +5 Stones. You now have **{stones}**.", icon="🙇")


@tree.command(name="stone", description="Throw stones at someone (bulk). 49% drop chance per stone. Max 15 per target per UK day.")
@app_commands.describe(user="Target", times="How many stones to throw (you must own them)")
async def stone_cmd(inter: discord.Interaction, user: discord.Member, times: int=1):
    if not await guard_worldler_inter(inter): return
    if user.bot:
        return await inter.response.send_message("Let's not stone bots.", ephemeral=True)
    if times <= 0:
        return await inter.response.send_message("Times must be positive.", ephemeral=True)

    await inter.response.defer(thinking=False)

    gid, uid, cid = inter.guild.id, inter.user.id, inter.channel_id

    have = await get_stones(gid, uid)
    if have < 1:
        return await inter.followup.send("You don't have any stones. Buy more with `/buy`.")

    today = uk_today_str()
    used_today = await get_stone_count_today(gid, uid, user.id, today)
    remaining_cap = max(0, 15 - used_today)
    if remaining_cap <= 0:
        return await inter.followup.send(f"🛑 You've reached your daily limit of **15** stones against {user.mention}. Resets at **00:00 UK time**.")

    # Only allow up to remaining cap and what they own
    allowed = min(times, remaining_cap, have)
    if allowed < times:
        await inter.followup.send(f"⚠️ You can only throw **{allowed}** more at {user.mention} today (cap 15 per day). Proceeding with **{allowed}**.")
    await change_stones(gid, uid, -allowed)

    # Stats (attempts)
    await inc_stat(gid, uid, "stones_thrown", allowed)
    await inc_stat(gid, user.id, "stoned_received", allowed)

    target_prot = await get_protection_until(gid, user.id)
    now = gmt_now_s()
    protected = target_prot > now

    # Count attempts regardless of protection
    await inc_stone_count_today(gid, uid, user.id, today, allowed)

    if protected:
        left = (target_prot - now)//60
        stones_left = await get_stones(gid, uid)
        return await inter.followup.send(
            f"🛡️ {user.mention} is protected from stones for about **~{left}m**. "
            f"You used **{allowed}** {EMO_STONE()} (left: **{stones_left}**)."
        )

    hits = sum(1 for _ in range(allowed) if random.random() < 0.49)
    victim_bal = await get_balance(gid, user.id)
    drops = min(hits, max(0, victim_bal))

    if drops > 0:
        # Take from victim, add to ground pot
        await change_balance(gid, user.id, -drops, announce_channel_id=cid)
        await add_to_pot(gid, drops)

    stones_left = await get_stones(gid, uid)

    if drops:
        # Plain text status (like before)…
        await inter.followup.send(
            f"💥 {inter.user.mention} stoned {user.mention} with **{allowed}** {EMO_STONE()} — **{drops}** {EMO_SHEKEL()} dropped. Use `/collect`."
        )

        # …plus a boxed message with a one-shot Collect button for THIS bundle.
        # If you'd rather send to the drops channel, replace `target_ch = inter.channel`
        # with: `target_ch = await _get_drops_channel(inter.guild) or inter.channel`
        target_ch = inter.channel
        emb = make_panel(
            title="💥 Shekels Dropped!",
            description=(
                f"{inter.user.mention} hit {user.mention}.\n"
                f"**{drops} {EMO_SHEKEL()}** fell to the ground — press **Collect** to grab this bundle, "
                f"or use `/collect` to scoop **everything** on the ground."
            ),
            icon="🪙"
        )
        view = ShekelDropView(guild_id=gid, channel_id=target_ch.id, amount=drops)
        await safe_send(
            target_ch,
            embed=emb,
            view=view,
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False)
        )
    else:
        await inter.followup.send(
            f"🪨 {inter.user.mention} stoned {user.mention} with **{allowed}** {EMO_STONE()}… no shekels dropped. (You have **{stones_left}** left.)"
        )


@tree.command(name="collect", description="Pick up ALL shekels from the ground.")
async def collect(inter: discord.Interaction):
    if not await guard_worldler_inter(inter): return
    gid, uid, cid = inter.guild.id, inter.user.id, inter.channel_id
    amt = await pop_all_from_pot(gid)
    if amt <= 0: return await inter.response.send_message("Nothing on the ground right now.")
    await change_balance(gid, uid, amt, announce_channel_id=cid)
    bal = await get_balance(gid, uid)
    s = "" if amt == 1 else "s"
    await inter.response.send_message(f"{EMO_SHEKEL()} {inter.user.mention} collected **{amt} shekel{s}**. Balance: **{bal}**")

@tree.command(name="leaderboard", description="Leaderboards you can flip through.")
async def leaderboard(inter: discord.Interaction):
    if not await guard_worldler_inter(inter): 
        return

    # ACK immediately to avoid "Unknown interaction" if DB/user fetches take >3s
    await inter.response.defer(thinking=False)

    guild = inter.guild

    async def _top_balances_page():
        gid = guild.id
        async with bot.db.execute(
            "SELECT user_id,balance FROM wallet WHERE guild_id=? ORDER BY balance DESC LIMIT 10",
            (gid,)
        ) as cur:
            rows = await cur.fetchall()

        emb = discord.Embed(title="🏆 Leaderboard — Most Shekels")
        if not rows:
            emb.description = "Nobody has any shekels yet."
            return emb

        # Gather tiers for suffix
        async with bot.db.execute(
            "SELECT role_id,min_balance FROM role_tier WHERE guild_id=? ORDER BY min_balance ASC",
            (gid,)
        ) as cur:
            tier_rows = await cur.fetchall()
        tiers = [(guild.get_role(rid), min_bal) for (rid, min_bal) in tier_rows if guild.get_role(rid)]

        lines=[]
        you_in_list = False
        for i,(uid,bal) in enumerate(rows,1):
            try:
                member = guild.get_member(uid) or await guild.fetch_member(uid)
                name = member.display_name
            except Exception:
                name = f"User {uid}"
            tier_name = ""
            for role, min_bal in tiers:
                if bal >= min_bal:
                    tier_name = role.name
            tier_suffix = f" ({tier_name})" if tier_name else ""
            marker = " ← you" if uid == inter.user.id else ""
            if marker: 
                you_in_list = True
            lines.append(f"{i}. **{name}** — {bal} {EMO_SHEKEL()}{tier_suffix}{marker}")

        if not you_in_list:
            async with bot.db.execute(
                "SELECT balance FROM wallet WHERE guild_id=? AND user_id=?", 
                (gid, inter.user.id)
            ) as cur:
                me = await cur.fetchone()
            if me:
                bal = me[0]
                tier_name = ""
                for role, min_bal in tiers:
                    if bal >= min_bal:
                        tier_name = role.name
                tier_suffix = f" ({tier_name})" if tier_name else ""
                lines.append(f"— **{inter.user.display_name}** — {bal} {EMO_SHEKEL()}{tier_suffix} ← you")

        emb.description = "\n".join(lines)
        return emb

    async def _make_stat_page(title: str, field: str, icon: str = "", note: str = ""):
        rows = await get_top_stats(guild.id, field)
        emb = discord.Embed(title=title)
        if note:
            emb.set_footer(text=note)
        if not rows:
            emb.description = "No data yet."
            return emb

        icon_sfx = f" {icon}" if icon else ""

        lines=[]
        you_in_list = False
        for i,(uid,val) in enumerate(rows,1):
            try:
                member = guild.get_member(uid) or await guild.fetch_member(uid)
                name = member.display_name
            except Exception:
                name = f"User {uid}"
            marker = " ← you" if uid == inter.user.id else ""
            if marker: 
                you_in_list = True
            lines.append(f"{i}. **{name}** — {val}{icon_sfx}{marker}")

        my_val = await get_my_stat(guild.id, inter.user.id, field)
        if not you_in_list and my_val > 0:
            lines.append(f"— **{inter.user.display_name}** — {my_val}{icon_sfx} ← you")

        emb.description = "\n".join(lines)
        return emb

    pages = [
        await _top_balances_page(),
        await _make_stat_page("🎯 Leaderboard — Most Bounties Won", "bounties_won", EMO_BOUNTY()),
        await _make_stat_page("🪨 Leaderboard — Most Stones Thrown", "stones_thrown", EMO_STONE()),
        await _make_stat_page("💥 Leaderboard — Most Stoned (Received)", "stoned_received", "💥",
                              note="Counts attempts against you, even if you were protected."),
        await _make_stat_page("💀 Leaderboard — Most Failed Solo Worldles", "solo_fails", "💀",
                              note="Only counts solo Worldles (including ending early)."),
        await _make_stat_page("🥷 Leaderboard — Most Snipes", "snipes", EMO_SNIPER()),
        await _make_stat_page("🥶 Leaderboard — Most Sniped", "sniped", "🥶",
                      note="Times your solo was finished by a sniper."),

    ]

    view = HelpBook(pages)
    # Use followup after defer
    await inter.followup.send(embed=pages[0], view=view)




# NEW: streak leaderboard
@tree.command(name="streaks", description="Show current solo play streaks (UK days).")
async def streaks_cmd(inter: discord.Interaction):
    if not await guard_worldler_inter(inter): return
    gid = inter.guild.id
    async with bot.db.execute("""
        SELECT user_id,cur,best
        FROM solo_streak
        WHERE guild_id=?
        ORDER BY cur DESC, best DESC
        LIMIT 10
    """, (gid,)) as cur:
        top = await cur.fetchall()

    lines = []
    if not top:
        my_last, my_cur, my_best = await _get_streak(gid, inter.user.id)
        if my_cur > 0:
            lines.append(f"1. **{inter.user.display_name}** — {my_cur} (best {my_best}) ← you")
        else:
            lines.append("No streaks yet. Play a solo Wordle to start your streak!")
    else:
        you_in_list = False
        for i, (uid, cur_s, best_s) in enumerate(top, 1):
            try:
                member = inter.guild.get_member(uid) or await inter.guild.fetch_member(uid)
                name = member.display_name
            except Exception:
                name = f"User {uid}"
            marker = " ← you" if uid == inter.user.id else ""
            if marker: you_in_list = True
            lines.append(f"{i}. **{name}** — {cur_s} (best {best_s}){marker}")
        if not you_in_list:
            my_last, my_cur, my_best = await _get_streak(gid, inter.user.id)
            if my_cur > 0:
                lines.append(f"— **{inter.user.display_name}** — {my_cur} (best {my_best}) ← you")

    await send_boxed(inter, "Streaks (UK days)", "\n".join(lines), icon="🔥")

@tree.command(name="mystreak", description="Show your solo streak (UK time).")
async def my_streak_cmd(inter: discord.Interaction):
    if not await guard_worldler_inter(inter): return
    gid, uid = inter.guild.id, inter.user.id
    last_date, cur_s, best_s = await _get_streak(gid, uid)
    if cur_s <= 0:
        return await send_boxed(inter, "Your Streak", "You don't have a streak yet. Start a solo Wordle to begin!", icon="🔥", ephemeral=True)
    await send_boxed(inter, "Your Streak", f"**{cur_s}** (best **{best_s}**) — last counted day: {last_date or '—'} (UK time).", icon="🔥")



@tree.command(name="set_balance", description="(Admin) Set a user's balance exactly.")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(user="Member", amount="New balance")
async def set_balance(inter: discord.Interaction, user: discord.Member, amount: int):
    if not inter.guild: return await inter.response.send_message("Server only.", ephemeral=True)
    if amount < 0: return await inter.response.send_message("Amount must be ≥ 0.", ephemeral=True)
    gid, cid = inter.guild.id, inter.channel_id
    current = await get_balance(gid, user.id)
    await change_balance(gid, user.id, amount - current, announce_channel_id=cid)
    await inter.response.send_message(f"✅ Set {user.mention}'s balance to **{amount} {EMO_SHEKEL()}**.")

@tree.command(name="snipe", description="Snipe another player's active Worldle (costs 1 shekel per shot).")
@app_commands.describe(target="Player you're sniping", word="Your 5-letter guess")
async def snipe_cmd(inter: discord.Interaction, target: discord.Member, word: str):
    # Check permissions/role first (don't defer yet so guard can reply ephemerally if needed)
    if not await guard_worldler_inter(inter):
        return
    if target.bot or target.id == inter.user.id:
        return await inter.response.send_message("Pick a real target (not yourself/bots).", ephemeral=True)

    gid, uid = inter.guild.id, inter.user.id

    # Must own the Sniper
    if await get_sniper(gid, uid) < 1:
        return await inter.response.send_message("You need to **buy the Sniper** first in `/shop`.", ephemeral=True)

    # Must have enough shekels to fire
    bal = await get_balance(gid, uid)
    if bal < SNIPER_SNIPE_COST:
        return await inter.response.send_message(f"You need **{SNIPER_SNIPE_COST} {EMO_SHEKEL()}** to fire a shot.", ephemeral=True)

    # Validate guess
    cleaned = "".join(ch for ch in word.lower().strip() if ch.isalpha())
    if len(cleaned) != 5:
        return await inter.response.send_message("Guess must be exactly 5 letters.", ephemeral=True)
    if not is_valid_guess(cleaned):
        return await inter.response.send_message("That’s not in the Wordle dictionary (UK variants supported).", ephemeral=True)

    # Target must have an active SOLO game
    target_cid = solo_channels.get((gid, target.id))
    if not target_cid or _key(gid, target_cid, target.id) not in solo_games:
        return await inter.response.send_message("That player has no active Worldle right now.", ephemeral=True)

    game = solo_games[_key(gid, target_cid, target.id)]

    # --- NEW: one shot per shooter per target game ---
    tried = game.setdefault("snipers_tried", set())
    if uid in tried:
        return await inter.response.send_message(
            "You’ve already taken your one shot at this Worldle. You can’t snipe it again.",
            ephemeral=True
        )
    # --------------------------------------------------

    # Defer now so we can safely use followups regardless of channel deletions later
    if not inter.response.is_done():
        try:
            await inter.response.defer(ephemeral=True, thinking=False)
        except Exception:
            pass

    # Pay to fire (charge into the victim's room for audit trail)
    await change_balance(gid, uid, -SNIPER_SNIPE_COST, announce_channel_id=target_cid)

    # Lock in that this shooter has used their shot for THIS game
    tried.add(uid)

    colors = score_guess(cleaned, game["answer"])
    row = render_row(cleaned, colors)

    # Post the snipe shot in the victim's room (ignore if channel vanished)
    try:
        ch = inter.guild.get_channel(target_cid)
        if isinstance(ch, discord.TextChannel):
            await ch.send(f"{EMO_SNIPER()} **{inter.user.display_name}** sniped with `{cleaned.upper()}`:\n{row}")
    except Exception:
        ch = None  # if anything went wrong, treat as missing

    # MISS → tell sniper and exit
    if cleaned != game["answer"]:
        try:
            await inter.followup.send("Missed shot. (Doesn't consume their tries.)", ephemeral=True)
        except Exception:
            pass
        return

    # HIT
    next_attempt = len(game["guesses"]) + 1  # snipe shot doesn't consume victim's tries
    payout = payout_for_attempt(next_attempt)
    if payout:
        await change_balance(gid, uid, payout, announce_channel_id=target_cid)

    # Count stats: shooter made a snipe; victim got sniped
    try:
        await inc_stat(gid, uid, "snipes", 1)
        await inc_stat(gid, target.id, "sniped", 1)
    except Exception:
        pass

    # Roll back the victim's daily solo slot (sniped games shouldn't count)
    try:
        start_day = game.get("start_date") or uk_today_str()
        await dec_solo_plays_on_date(gid, target.id, start_day)
    except Exception:
        pass

    origin_cid = game.get("origin_cid")
    ans = game["answer"].upper()

    # Build a final board for the announcement: victim guesses + this snipe shot
    try:
        guesses_for_board = list(game["guesses"]) + [{"word": cleaned, "colors": colors}]
        board_str = render_board(guesses_for_board)
    except Exception:
        board_str = None

    # Tell the sniper first (ephemeral is safe)
    try:
        await inter.followup.send(
            f"Hit confirmed. **Word: {ans}** · You earned **{payout} {EMO_SHEKEL()}**.",
            ephemeral=True
        )
    except Exception:
        pass

    # Public announcement (include board if we built it)
    ann = (
        f"🎯 {inter.user.mention} **sniped** {target.mention}'s Worldle (**{ans}**) on attempt **{next_attempt}** "
        f"and stole **{payout} {EMO_SHEKEL()}**!"
    )
    if board_str:
        ann += f"\n{board_str}"
    try:
        await _announce_result(inter.guild, origin_cid, ann)
    except Exception:
        pass

    # Cleanup the victim's game state
    try:
        solo_games.pop(_key(gid, target_cid, target.id), None)
        if solo_channels.get((gid, target.id)) == target_cid:
            solo_channels.pop((gid, target.id), None)
    except Exception:
        pass

    # Delete the victim's channel last (ignore errors if already gone)
    try:
        if isinstance(ch, discord.TextChannel):
            await ch.delete(reason="Worldle sniped (finished)")
    except Exception:
        pass





# -------------------- Setup (category + announcements) --------------------
@tree.command(name="worldle_set_category", description="(Admin) Set THIS channel's category for solo Wordle rooms.")
@app_commands.default_permissions(administrator=True)
async def worldle_set_category(inter: discord.Interaction):
    if not inter.guild or not inter.channel:
        return await send_boxed(inter, "Solo Category", "Server only.", icon="🛠", ephemeral=True)
    cat = getattr(inter.channel, "category", None)
    if not isinstance(cat, discord.CategoryChannel):
        return await send_boxed(inter, "Solo Category", "This channel isn’t inside a category. Move it, then run again.", icon="🛠", ephemeral=True)
    await set_cfg(inter.guild.id, solo_category_id=cat.id)
    await send_boxed(inter, "Solo Category", f"Solo rooms will be created under **{cat.name}**.", icon="🛠")

@tree.command(name="worldle_set_announce", description="(Admin) Set THIS channel for solo Wordle result announcements.")
@app_commands.default_permissions(administrator=True)
async def worldle_set_announce(inter: discord.Interaction):
    if not inter.guild or not inter.channel:
        return await send_boxed(inter, "Announcements", "Server only.", icon="📣", ephemeral=True)
    await set_cfg(inter.guild.id, announcements_channel_id=inter.channel.id)
    await send_boxed(inter, "Announcements", f"All announcements will be posted in {inter.channel.mention}.", icon="📣")


# -------------------- Roles admin --------------------
@tree.command(name="role_maketier", description="(Admin) Create a role and bind it to a Shekel minimum.")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(name="Role name", min="Minimum Shekels")
async def role_maketier(inter: discord.Interaction, name: str, min: int):
    if not inter.guild: return await inter.response.send_message("Server only.", ephemeral=True)
    guild = inter.guild
    if min < 0: return await inter.response.send_message("Min must be ≥0.", ephemeral=True)
    if not guild.me or not guild.me.guild_permissions.manage_roles:
        return await inter.response.send_message("I need **Manage Roles**.", ephemeral=True)
    role = discord.utils.find(lambda r: r.name.lower()==name.lower(), guild.roles)
    if role is None:
        role = await guild.create_role(name=name, reason="Create Wordle World tier")
    await bot.db.execute("""
      INSERT INTO role_tier(guild_id,role_id,min_balance) VALUES(?,?,?)
      ON CONFLICT(guild_id,role_id) DO UPDATE SET min_balance=excluded.min_balance
    """, (guild.id, role.id, min))
    await bot.db.commit()
    await inter.response.send_message(f"✅ Created/bound tier: {role.mention} at **{min}**.")

@tree.command(name="role_addtier", description="(Admin) Bind an existing role to a Shekel minimum.")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(role="Role", min="Minimum Shekels")
async def role_addtier(inter: discord.Interaction, role: discord.Role, min: int):
    if not inter.guild: return await inter.response.send_message("Server only.", ephemeral=True)
    if min < 0: return await inter.response.send_message("Min must be ≥0.", ephemeral=True)
    if not bot_can_manage_role(inter.guild, role):
        return await inter.response.send_message("I can't manage that role. Move my role above it & grant **Manage Roles**.", ephemeral=True)
    await bot.db.execute("""
      INSERT INTO role_tier(guild_id,role_id,min_balance) VALUES(?,?,?)
      ON CONFLICT(guild_id,role_id) DO UPDATE SET min_balance=excluded.min_balance
    """, (inter.guild.id, role.id, min))
    await bot.db.commit()
    await inter.response.send_message(f"✅ Bound tier: **{role.name}** at **{min}**.")

@tree.command(name="role_removetier", description="(Admin) Remove a tier mapping.")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(role="Role")
async def role_removetier(inter: discord.Interaction, role: discord.Role):
    if not inter.guild: return await inter.response.send_message("Server only.", ephemeral=True)
    await bot.db.execute("DELETE FROM role_tier WHERE guild_id=? AND role_id=?", (inter.guild.id, role.id))
    await bot.db.commit()
    await inter.response.send_message(f"🗑️ Removed tier for **{role.name}**.")

@tree.command(name="role_tiers", description="List tier roles.")
async def role_tiers(inter: discord.Interaction):
    if not await guard_worldler_inter(inter): return
    async with bot.db.execute("SELECT role_id,min_balance FROM role_tier WHERE guild_id=? ORDER BY min_balance ASC",(inter.guild.id,)) as cur:
        rows = await cur.fetchall()
    if not rows: return await inter.response.send_message("No tiers configured.")
    lines = ["🏷️ **Role Tiers** (balance ≥ min):"]
    for rid, min_bal in rows:
        role = inter.guild.get_role(rid)
        lines.append(f"• {role.mention if role else '(missing role)'} — **{min_bal}**")
    await inter.response.send_message("\n".join(lines))

@tree.command(name="role_sync", description="(Admin) Resync tier roles for everyone I know.")
@app_commands.default_permissions(administrator=True)
async def role_sync(inter: discord.Interaction):
    if not inter.guild: return await inter.response.send_message("Server only.", ephemeral=True)
    await inter.response.send_message("⏳ Syncing…", ephemeral=True)
    async with bot.db.execute("SELECT user_id FROM wallet WHERE guild_id=?", (inter.guild.id,)) as cur:
        ids = [r[0] for r in await cur.fetchall()]
    for uid in ids:
        try:
            member = inter.guild.get_member(uid) or await inter.guild.fetch_member(uid)
            await sync_member_role_tiers(inter.guild, member)
        except Exception:
            continue
    await inter.followup.send("✅ Done.")

# -------------------- Join / Help / Resync --------------------
@tree.command(name="immigrate", description=f"Join Wordle World: get the {WORLDLER_ROLE_NAME} role and a welcome bonus.")
async def immigrate(inter: discord.Interaction):
    if not inter.guild:
        return await inter.response.send_message("Run this in a server.", ephemeral=True)
    guild = inter.guild
    member = inter.user

    rid = await ensure_worldler_role(guild)
    if not rid:
        return await inter.response.send_message("I need **Manage Roles** to create the role. Ask an admin.", ephemeral=True)
    role = guild.get_role(rid)
    if role in (guild.get_member(member.id) or await guild.fetch_member(member.id)).roles:
        bal = await get_balance(guild.id, member.id)
        return await inter.response.send_message(f"You're already a **{WORLDLER_ROLE_NAME}**! Balance: **{bal}**.", ephemeral=True)

    try:
        await member.add_roles(role, reason="Wordle World immigration")
    except Exception as e:
        return await inter.response.send_message(f"Couldn't add the role. Do I have **Manage Roles** and is my role above **{WORLDLER_ROLE_NAME}**? ({e})", ephemeral=True)

    await change_balance(guild.id, member.id, START_BONUS, announce_channel_id=inter.channel_id)
    bal = await get_balance(guild.id, member.id)
    await inter.response.send_message(
        f"🌍 Welcome to **Wordle World** {member.mention}!\n"
        f"• Granted **{WORLDLER_ROLE_NAME}** role\n"
        f"• Welcome bonus: **+{START_BONUS} {EMO_SHEKEL()}** — Balance **{bal}**",
        allowed_mentions=discord.AllowedMentions.none()
    )

@tree.command(name="help", description="Interactive help: learn the game and commands.")
async def help_cmd(inter: discord.Interaction):
    pages = build_help_pages(getattr(inter.guild, "name", None))
    view = HelpBook(pages)
    # Public (non-ephemeral) by default — keep it visible to everyone
    await inter.response.send_message(embed=pages[0], view=view)


@tree.command(name="worldle_resync", description="(Admin) Fix duplicate commands / force-refresh here.")
@app_commands.default_permissions(administrator=True)
async def worldle_resync(inter: discord.Interaction):
    if not inter.guild:
        return await inter.response.send_message("Run in a server.", ephemeral=True)
    await inter.response.defer(ephemeral=True)
    tree.clear_commands(guild=inter.guild)
    await tree.sync(guild=inter.guild)
    tree.copy_global_to(guild=inter.guild)
    await tree.sync(guild=inter.guild)
    await tree.sync()
    await inter.followup.send("✅ Commands refreshed here. If any still don’t appear, close/reopen the slash picker.")

# -------------------- Slash wrappers: Solo + Casino --------------------
@tree.command(name="worldle", description="Start your own Wordle in a private room (free, 5/day).")
async def worldle_start(inter: discord.Interaction):
    if not await guard_worldler_inter(inter): return
    if not inter.guild or not inter.channel: return
    await inter.response.defer(thinking=False)
    ch = await solo_start(inter.channel, inter.user)
    if isinstance(ch, discord.TextChannel):
        await send_boxed(inter, "Solo Room Opened", f"{inter.user.mention} your room is {ch.mention}.", icon="🧩")

@tree.command(name="worldle_casino", description="Play a casino Wordle. First game: Word Pot.")
@app_commands.describe(game="Pick a casino game")
@app_commands.choices(game=[app_commands.Choice(name="Word Pot", value="word_pot")])
async def worldle_casino(inter: discord.Interaction, game: Optional[app_commands.Choice[str]] = None):
    if not await guard_worldler_inter(inter): return
    if not inter.guild or not inter.channel: return
    await inter.response.defer(thinking=False)
    choice = (game.value if game else "word_pot")
    if choice != "word_pot":
        return await send_boxed(inter, "Casino", "Only **Word Pot** is available right now.", icon="🎰")
    ch = await casino_start_word_pot(inter.channel, inter.user)
    if isinstance(ch, discord.TextChannel):
        await send_boxed(inter, "Word Pot Room Opened", f"{inter.user.mention} your room is {ch.mention}.", icon="🎰")


@tree.command(name="worldle_guess", description="Guess your word in this channel.")
@app_commands.describe(word="5-letter guess")
async def worldle_guess(inter: discord.Interaction, word: str):
    if not await guard_worldler_inter(inter): return
    if not inter.guild or not inter.channel: return
    await inter.response.defer(thinking=False)
    gid, cid, uid = inter.guild.id, inter.channel.id, inter.user.id

    if _key(gid, cid, uid) in solo_games:
        await solo_guess(inter.channel, inter.user, word)
    elif _key(gid, cid, uid) in casino_games:
        await casino_guess(inter.channel, inter.user, word)
    elif cid in dungeon_games:
        await dungeon_guess(inter.channel, inter.user, word)
    else:
        # keeps the nice "no game here" message for solo if applicable
        await solo_guess(inter.channel, inter.user, word)


@tree.command(name="worldle_end", description="End your current Wordle here (counts as a fail).")
async def worldle_end(inter: discord.Interaction):
    if not await guard_worldler_inter(inter): 
        return
    if not inter.guild or not inter.channel: 
        return

    gid, cid, uid = inter.guild.id, inter.channel.id, inter.user.id

    # --- Word Pot first ---
    cgame = casino_games.get(_key(gid, cid, uid))
    if cgame:
        board = render_board(cgame["guesses"], total_rows=3)
        ans_raw = cgame["answer"]
        ans = ans_raw.upper()
        origin_cid = cgame.get("origin_cid")

        cur_pot = await get_casino_pot(gid)
        new_pot = cur_pot + (cgame.get("staked", 0) or 0)
        await set_casino_pot(gid, new_pot)

        casino_games.pop(_key(gid, cid, uid), None)
        if casino_channels.get((gid, uid)) == cid:
            casino_channels.pop((gid, uid), None)

        quip = random.choice(FAIL_QUIPS)
        definition = await fetch_definition(ans_raw)

        await inter.response.send_message(board)
        await inter.followup.send(
            f"🛑 Ended your **Word Pot** game. The word was **{ans}** — {quip}{f'\\n📖 Definition: {definition}' if definition else ''}\n"
            f"Pot is now **{new_pot} {EMO_SHEKEL()}**."
        )

        fields = [("Board", board, False), ("Pot", f"Now **{new_pot} {EMO_SHEKEL()}**", True)]
        if definition:
            fields.append(("Definition", definition, False))

        emb = make_card(
            title="🎰 Word Pot — Ended Early",
            description=f"{inter.user.mention} ended their Word Pot early. The word was **{ans}** — {quip}",
            fields=fields,
            color=CARD_COLOR_FAIL,
        )
        await _announce_result(inter.guild, origin_cid, content="", embed=emb)

        try:
            await inter.channel.delete(reason="Word Pot ended by user (fail)")
        except Exception:
            pass
        return

    # --- Solo fallback ---
    sgame = solo_games.get(_key(gid, cid, uid))
    if not sgame:
        return await inter.response.send_message("You don't have a game running here.")

    board = render_board(sgame["guesses"])
    ans_raw = sgame["answer"]
    ans = ans_raw.upper()
    origin_cid = sgame.get("origin_cid")

    solo_games.pop(_key(gid, cid, uid), None)
    if solo_channels.get((gid, uid)) == cid:
        solo_channels.pop((gid, uid), None)

    quip = random.choice(FAIL_QUIPS)
    definition = await fetch_definition(ans_raw)

    await inter.response.send_message(board)
    await inter.followup.send(f"🛑 Ended your game. The word was **{ans}** — {quip}{f'\\n📖 Definition: {definition}' if definition else ''}")

    fields = [("Board", board, False)]
    if definition:
        fields.append(("Definition", definition, False))

    emb = make_card(
        title="💀 Solo — Ended Early",
        description=f"{inter.user.mention} failed their Worldle (ended early). The word was **{ans}** — {quip}",
        fields=fields,
        color=CARD_COLOR_FAIL,
    )
    await _announce_result(inter.guild, origin_cid, content="", embed=emb)

    try:
        await inter.channel.delete(reason="Wordle World solo ended by user (fail)")
    except Exception:
        pass






# -------------------- Admin emoji debug tools --------------------
@tree.command(name="ww_emoji_test", description="(Admin) Show how my named emojis resolve right now.")
@app_commands.default_permissions(administrator=True)
async def ww_emoji_test(inter: discord.Interaction):
    txt = (
        f"badge: {EMO_BADGE()}  (expects name: `{EMO_BADGE_NAME}`)\n"
        f"chicken: {EMO_CHICKEN()}  (expects name: `{EMO_CHICKEN_NAME}`)\n"
        f"sniper: {EMO_SNIPER()}  (expects name: `{EMO_SNIPER_NAME}`)\n"
        f"bounty: {EMO_BOUNTY()}  (expects name: `{EMO_BOUNTY_NAME}`)\n"
        f"shekel: {EMO_SHEKEL()}  (expects name: `{EMO_SHEKEL_NAME}`)\n"
        f"stone:  {EMO_STONE()}  (expects name: `{EMO_STONE_NAME}`)\n"
    )
    await inter.response.send_message(txt)

@tree.command(name="ww_refresh_tiles", description="(Admin) Re-scan tile emojis (wl_*) without restarting.")
@app_commands.default_permissions(administrator=True)
async def ww_refresh_tiles(inter: discord.Interaction):
    build_emoji_lookup()
    await inter.response.send_message("✅ Tile emoji cache rebuilt.")



class ShekelDropView(discord.ui.View):
    def __init__(self, guild_id: int, channel_id: int, amount: int = 1, timeout: float = 600):
        super().__init__(timeout=timeout)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.amount = int(max(1, amount))  # set before using
        self.claimed = False

        btn = discord.ui.Button(
            label=f"Collect {self.amount}",
            style=discord.ButtonStyle.success,
            emoji=EMO_SHEKEL(),
        )
        btn.callback = self._on_collect
        self.add_item(btn)
        self._btn = btn

    async def _on_collect(self, interaction: discord.Interaction):
        if not interaction.guild or interaction.guild.id != self.guild_id:
            return await interaction.response.send_message("Wrong server for this drop.", ephemeral=True)
        if self.claimed:
            return await interaction.response.send_message("Too late — already collected.", ephemeral=True)

        taken = await take_from_pot(self.guild_id, self.amount)
        if taken <= 0:
            self.claimed = True
            self._btn.disabled = True
            self._btn.style = discord.ButtonStyle.secondary
            self._btn.label = "Already taken"
            try:
                await interaction.response.edit_message(view=self)
            except Exception:
                pass
            return await interaction.followup.send("Someone scooped it, or `/collect` emptied the ground.", ephemeral=True)

        # Award and update the button
        await change_balance(self.guild_id, interaction.user.id, taken, announce_channel_id=self.channel_id)
        self.claimed = True
        self._btn.disabled = True
        self._btn.style = discord.ButtonStyle.secondary
        self._btn.label = f"Collected by {interaction.user.display_name}"
        try:
            await interaction.response.edit_message(view=self)
        except Exception:
            pass

        # PUBLIC announcement so others see who collected it
        s = "" if taken == 1 else "s"
        try:
            # Prefer the interaction channel; fall back to the stored channel id
            ch = interaction.channel or interaction.guild.get_channel(self.channel_id)
            await safe_send(
                ch,
                f"{EMO_SHEKEL()} {interaction.user.mention} collected **{taken} shekel{s}**.",
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )
        except Exception:
            # If sending publicly fails for any reason, at least tell the collector
            await interaction.followup.send(f"You collected **{taken} shekel{s}**.", ephemeral=True)



async def maybe_drop_shekel_on_message(msg: discord.Message):
    """
    Ambient drop:
      • At most one RNG roll per 20-minute slot *per guild* (DB-coordinated).
      • 10% chance to mint a bundle of **1–5** shekels when the slot is claimed.
      • Posts to the configured Drops channel, else the current channel.
    """
    if not msg.guild or msg.author.bot:
        return

    gid = msg.guild.id
    slot = _current_20m_slot()

    # Try to claim (guild, slot). If another process already claimed it, this INSERT
    # is ignored and rowcount will be 0 — meaning we've already rolled for this slot.
    cur = await bot.db.execute(
        "INSERT OR IGNORE INTO ambient_rolls(guild_id, slot) VALUES(?, ?)",
        (gid, slot),
    )
    await bot.db.commit()
    if getattr(cur, "rowcount", 0) == 0:
        return  # someone already rolled this 20-minute window

    # Only the claimer attempts RNG
    if random.random() >= SHEKEL_DROP_CHANCE:  # 10% default
        return

    # Amount: uniform 1..5
    amount = random.randint(SHEKEL_DROP_MIN, SHEKEL_DROP_MAX)

    # Mint into the ground pot
    await add_to_pot(gid, amount)

    # Post in configured Drops Channel, else current channel
    target = await _get_drops_channel(msg.guild) or msg.channel

    emb = make_panel(
        title="💰 Shekel Drop!",
        description=(
            f"A shiny {EMO_SHEKEL()} hit the floor — **{amount}**!\n"
            "Press **Collect** to grab this bundle, or use `/collect` to scoop **everything** on the ground."
        ),
        icon="🪙",
    )
    view = ShekelDropView(guild_id=gid, channel_id=target.id, amount=amount)

    await safe_send(
        target,
        embed=emb,
        view=view,
        allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
    )




async def take_from_pot(gid: int, amount: int) -> int:
    async with bot.db.execute("SELECT pot FROM ground WHERE guild_id=?", (gid,)) as cur:
        row = await cur.fetchone()
    pot = row[0] if row else 0
    take = min(max(0, int(amount)), pot)
    if take > 0:
        await bot.db.execute("UPDATE ground SET pot = pot - ? WHERE guild_id=?", (take, gid))
        await bot.db.commit()
    return take


async def _get_drops_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    cfg = await get_cfg(guild.id)
    ch_id = cfg.get("drops_channel_id")
    if ch_id:
        ch = guild.get_channel(ch_id)
        if isinstance(ch, discord.TextChannel) and ch.permissions_for(guild.me).send_messages:
            return ch
    return None

@tree.command(name="set_drops_channel", description="(Admin) Set THIS channel for shekel drop announcements.")
@app_commands.default_permissions(administrator=True)
async def set_drops_channel_cmd(inter: discord.Interaction):
    if not inter.guild or not inter.channel:
        return await send_boxed(inter, "Drops Channel", "Server only.", icon="🪙", ephemeral=True)
    await set_cfg(inter.guild.id, drops_channel_id=inter.channel.id)
    await send_boxed(inter, "Drops Channel", f"Shekel drops will be announced in {inter.channel.mention}.", icon="🪙")




# -------------------- Text shortcuts --------------------
class Shim:
    def __init__(self, message: discord.Message):
        self.guild = message.guild
        self.channel = message.channel
        self.user = message.author
        class Resp:
            def __init__(self, ch): self._ch = ch
            async def send_message(self, content=None, **kwargs):
                kwargs.pop("ephemeral", None)
                return await self._ch.send(content, **kwargs)
        class Follow:
            def __init__(self, ch): self._ch = ch
            async def send(self, content=None, **kwargs):
                kwargs.pop("ephemeral", None)
                return await self._ch.send(content, **kwargs)
        self.response = Resp(self.channel)
        self.followup = Follow(self.channel)
        self.command = None

@bot.event
async def on_message(msg: discord.Message):
    if not msg.guild or msg.author.bot:
        return

    # 🔔 10% random shekel drop on EVERY user message (boxed with Collect button)
    # (requires the ShekelDropView + maybe_drop_shekel_on_message you added earlier)
    try:
        await maybe_drop_shekel_on_message(msg)
    except Exception as e:
        log.warning(f"shekel drop failed: {e}")

    content = msg.content.strip()
    if not content:
        return

    lower = content.lower()

    # --- SOLO shortcut ---
    if lower == "w":
        if not await guard_worldler_msg(msg):
            await send_boxed(msg.channel, "Access Required", f"{msg.author.mention} you need **{WORLDLER_ROLE_NAME}**. Use `/immigrate` to join.", icon="🔐")
            return
        ch = await solo_start(msg.channel, msg.author)
        if isinstance(ch, discord.TextChannel):
            await send_boxed(msg.channel, "Solo Room Opened", f"{msg.author.mention} your room is {ch.mention}.", icon="🧩")
        return

    # --- CASINO shortcut (Word Pot) ---
    if lower == "wc":
        if not await guard_worldler_msg(msg):
            await send_boxed(msg.channel, "Access Required", f"{msg.author.mention} you need **{WORLDLER_ROLE_NAME}**. Use `/immigrate`.", icon="🔐")
            return
        ch = await casino_start_word_pot(msg.channel, msg.author)
        if isinstance(ch, discord.TextChannel):
            await send_boxed(msg.channel, "Word Pot Room Opened", f"{msg.author.mention} your room is {ch.mention}.", icon="🎰")
        return

    # --- GUESS (smart: duel turn / bounty / casino / dungeon / solo) ---
    if lower.startswith("g "):
        if not await guard_worldler_msg(msg):
            await send_boxed(msg.channel, "Access Required", f"{msg.author.mention} you need **{WORLDLER_ROLE_NAME}**. Use `/immigrate`.", icon="🔐")
            return
        word = content.split(None, 1)[1]

        did = _duel_in_channel(msg.channel.id)
        if did:
            d = duels.get(did)
            if d and d["state"] == "active" and msg.author.id == d["turn"]:
                inter = Shim(msg)
                await worldle_duel_guess.callback(inter, did, word)
                return

        game = bounty_games.get(msg.guild.id)
        if game and game["channel_id"] == msg.channel.id:
            inter = Shim(msg)
            await worldle_bounty_guess.callback(inter, word)
            return

        if _key(msg.guild.id, msg.channel.id, msg.author.id) in casino_games:
            await casino_guess(msg.channel, msg.author, word)
            return

        if msg.channel.id in dungeon_games:
            await dungeon_guess(msg.channel, msg.author, word)
            return

        await solo_guess(msg.channel, msg.author, word)
        return

    # --- Bounty guess shortcut ---
    if lower.startswith("bg "):
        if not await guard_worldler_msg(msg):
            await send_boxed(msg.channel, "Access Required", f"{msg.author.mention} you need **{WORLDLER_ROLE_NAME}**. Use `/immigrate`.", icon="🔐")
            return
        word = content.split(None, 1)[1]
        inter = Shim(msg)
        await worldle_bounty_guess.callback(inter, word)
        return





# -------------------- lifecycle --------------------
@bot.event
async def on_ready():
    log_deps_health()
    await db_init()
    for g in bot.guilds:
        try:
            await ensure_worldler_role(g)
            await ensure_bounty_role(g)
            if DEFAULT_TIERS:
                await ensure_default_tiers(g)
            # ensure casino pot row exists
            await get_casino_pot(g.id)
        except Exception as e:
            log.warning(f"guild init {g.id} failed: {e}")
    build_emoji_lookup()
    try:
        await tree.sync()
        print("Global slash commands synced.")
    except Exception as e:
        log.warning(f"global sync failed: {e}")
    if not bounty_loop.is_running():
        bounty_loop.start()
    me = bot.user
    print(f"Logged in as {me} ({me.id})")

@bot.event
async def on_guild_join(guild: discord.Guild):
    await ensure_worldler_role(guild)
    await ensure_bounty_role(guild)
    if DEFAULT_TIERS:
        await ensure_default_tiers(guild)
    await get_casino_pot(guild.id)

# -------------------- run --------------------
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Missing DISCORD_TOKEN in environment.")
    bot.run(TOKEN)
