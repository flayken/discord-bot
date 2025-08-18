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
def get_named_emoji(name: str) -> str:
    e = discord.utils.find(lambda em: em.name.lower() == name.lower(), bot.emojis)
    return str(e) if e else ""

EMO_BADGE   = lambda: (get_named_emoji(EMO_BADGE_NAME)   or "🎖️")
EMO_CHICKEN = lambda: (get_named_emoji(EMO_CHICKEN_NAME) or "🍗")
EMO_SHEKEL  = lambda: (get_named_emoji(EMO_SHEKEL_NAME)  or "🪙")
EMO_STONE   = lambda: (get_named_emoji(EMO_STONE_NAME)   or "🪨")
EMO_SNIPER  = lambda: (get_named_emoji(EMO_SNIPER_NAME)  or "🎯")
EMO_BOUNTY  = lambda: (get_named_emoji(EMO_BOUNTY_NAME)  or "🎯")


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
