# worldle_bot/core/config.py
"""
Global configuration and constants for the Worldle bot.
Central place for emojis, role names, cooldowns, and tokens.
"""

import os

# -------------------------------------------------------------------
# Discord Token
# -------------------------------------------------------------------
TOKEN = os.getenv("DISCORD_TOKEN")  # fallback handled in main.py

# -------------------------------------------------------------------
# Roles / Access
# -------------------------------------------------------------------
WORLDLER_ROLE_NAME = "Worldler"  # change if your bot uses a different role

# -------------------------------------------------------------------
# Emoji names (these get resolved at runtime via build_emoji_lookup)
# You had functions like EMO_SHEKEL(), EMO_STONE(), etc.
# Keep just the *names* here.
# -------------------------------------------------------------------
EMO_BADGE_NAME = "ww_badge"
EMO_CHICKEN_NAME = "ww_chicken"
EMO_SNIPER_NAME = "ww_sniper"
EMO_BOUNTY_NAME = "ww_bounty"
EMO_SHEKEL_NAME = "ww_shekel"
EMO_STONE_NAME = "ww_stone"

# -------------------------------------------------------------------
# Economy
# -------------------------------------------------------------------
SHEKEL_DROP_CHANCE = 0.10   # 10% chance per slot
SHEKEL_DROP_MIN = 1
SHEKEL_DROP_MAX = 5

# -------------------------------------------------------------------
# Bounty System
# -------------------------------------------------------------------
BOUNTY_PAYOUT = 50
BOUNTY_ARM_DELAY_S = 60     # 1 minute
BOUNTY_EXPIRE_MIN = 15
BOUNTY_EXPIRE_S = BOUNTY_EXPIRE_MIN * 60

# -------------------------------------------------------------------
# Dungeon
# -------------------------------------------------------------------
DUNGEON_TICKET_COST = 10
DUNGEON_MAX_ROUNDS = 5

# -------------------------------------------------------------------
# Cooldowns
# -------------------------------------------------------------------
DAILY_RESET_HOUR = 0  # UK time reset
PRAY_REWARD = 5
BEG_REWARD = 5

# -------------------------------------------------------------------
# Other toggles
# -------------------------------------------------------------------
DEFAULT_TIERS = True  # whether to auto-create tiers on guild join
