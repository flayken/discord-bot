# -------------------- BOUNTY (hourly GMT + reaction gate; manual now uses gate too) --------------------
BOUNTY_PAYOUT = 5
BOUNTY_EXPIRE_MIN = 59
BOUNTY_EXPIRE_S = BOUNTY_EXPIRE_MIN * 60

# NEW: arm delay and per-user guess cooldown
BOUNTY_ARM_DELAY_S = 60          # wait 60s after 2 reactions before arming
BOUNTY_GUESS_COOLDOWN_S = 5      # 5s per-user cooldown between guesses

# Track last guess time per (guild_id, user_id) for the bounty
last_bounty_guess_ts: dict[tuple[int, int], int] = {}
