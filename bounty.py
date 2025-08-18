# worldle_bot/features/bounty.py
"""
Bounty feature: hourly bounty prompts, arming, and guess handling.
"""

import random
import logging
import discord

from ..core.utils import safe_send, make_panel
from ..core.config import (
    EMO_BOUNTY, EMO_BOUNTY_NAME,
    EMO_SHEKEL,
    BOUNTY_ARM_DELAY_S, BOUNTY_PAYOUT,
    BOUNTY_EXPIRE_MIN, BOUNTY_EXPIRE_S,
)
from ..core.db import get_cfg, set_cfg
from ..features.roles import ensure_bounty_role

log = logging.getLogger("worldle_bot")

# Track current bounties and games
pending_bounties: dict[int, dict] = {}
bounty_games: dict[int, dict] = {}

# Answers pool (imported from your original word list)
ANSWERS: list[str] = ["apple", "grape", "melon", "peach"]  # TODO: replace with full word list


# -------------------------------------------------------------------
# Utility
# -------------------------------------------------------------------
def _bounty_emoji_matches(emoji: discord.PartialEmoji) -> bool:
    """Check if emoji matches bounty emoji config."""
    target_name = (EMO_BOUNTY_NAME or "ww_bounty").lower()
    if emoji.is_unicode_emoji():
        return emoji.name == "🎯"
    return (emoji.name or "").lower() == target_name


async def _find_bounty_channel(guild: discord.Guild) -> discord.TextChannel | None:
    """Pick bounty channel: config → system → first writable channel."""
    cfg = await get_cfg(guild.id)
    if cfg.get("bounty_channel_id"):
        ch = guild.get_channel(cfg["bounty_channel_id"])
        if isinstance(ch, discord.TextChannel) and ch.permissions_for(guild.me).send_messages:
            return ch
    if guild.system_channel and guild.system_channel.permissions_for(guild.me).send_messages:
        return guild.system_channel
    for ch in guild.text_channels:
        if ch.permissions_for(guild.me).send_messages:
            return ch
    return None


# -------------------------------------------------------------------
# Posting bounty prompt
# -------------------------------------------------------------------
async def _post_bounty_prompt(guild: discord.Guild, channel: discord.TextChannel, hour_idx: int):
    """Post bounty prompt message."""
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

    msg = await safe_send(
        channel,
        content=role_mention or None,
        embed=emb,
        allowed_mentions=discord.AllowedMentions(users=False, roles=(not suppress_ping), everyone=False),
    )

    try:
        await msg.add_reaction(em)
    except Exception:
        try:
            await msg.add_reaction("🎯")
        except Exception:
            pass

    pending_bounties[guild.id] = {
        "message_id": msg.id,
        "channel_id": channel.id,
        "users": set(),
        "hour_idx": hour_idx,
        "expires_at": discord.utils.utcnow().timestamp() + BOUNTY_EXPIRE_S,
    }
    await set_cfg(guild.id, last_bounty_hour=hour_idx)
    return True


# -------------------------------------------------------------------
# Arming bounty
# -------------------------------------------------------------------
async def _start_bounty_after_gate(guild: discord.Guild, channel_id: int):
    """Start bounty game after 2 players react."""
    if guild.id in bounty_games:
        return
    answer = random.choice(ANSWERS)
    bounty_games[guild.id] = {
        "answer": answer,
        "channel_id": channel_id,
        "started_at": discord.utils.utcnow().timestamp(),
        "expires_at": discord.utils.utcnow().timestamp() + BOUNTY_EXPIRE_S,
    }
    await set_cfg(guild.id, last_bounty_ts=discord.utils.utcnow().timestamp(), suppress_bounty_ping=0)
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
