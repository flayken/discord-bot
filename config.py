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


# -------------------- GLOBALS --------------------

# ---- Dailies panel state (place near other globals) ----
dailies_msg_ids: set[int] = set()   # message IDs of active /dailies panels
