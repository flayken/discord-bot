# worldle_bot/core/utils.py
"""
Utility helpers for the Worldle bot:
 - safe_send (robust message sending)
 - embed panel builders
 - emoji resolvers
 - access guards
"""

import discord
from discord import app_commands
from discord.ext import commands
import logging

from . import config

log = logging.getLogger("worldle_bot")


# -------------------------------------------------------------------
# Safe send (prevents crashes if channel perms break)
# -------------------------------------------------------------------
async def safe_send(channel: discord.TextChannel, content=None, **kwargs):
    """Send a message safely without crashing on perms or API errors."""
    try:
        return await channel.send(content, **kwargs)
    except Exception as e:
        log.warning(f"safe_send failed in {channel}: {e}")
        return None


# -------------------------------------------------------------------
# Embeds
# -------------------------------------------------------------------
def make_panel(title: str, description: str, icon: str = None) -> discord.Embed:
    emb = discord.Embed(title=title, description=description, color=discord.Color.blurple())
    if icon:
        emb.set_author(name=icon)
    return emb


async def send_boxed(
    dest,
    title: str,
    description: str,
    icon: str = None,
    ephemeral: bool = False,
):
    """
    Send an embed with a title + description (like your boxed UI).
    Works for both Interactions and regular Channels.
    """
    emb = make_panel(title, description, icon)
    if isinstance(dest, discord.Interaction):
        return await dest.response.send_message(embed=emb, ephemeral=ephemeral)
    elif isinstance(dest, discord.abc.Messageable):
        return await dest.send(embed=emb)
    else:
        log.warning("send_boxed called with unknown destination")
        return None


# -------------------------------------------------------------------
# Emoji lookup
# -------------------------------------------------------------------
# These return the actual usable emoji string, resolved from guild cache.
# Right now just return the name — you can expand later to resolve custom IDs.

def EMO_BADGE():
    return f":{config.EMO_BADGE_NAME}:"

def EMO_CHICKEN():
    return f":{config.EMO_CHICKEN_NAME}:"

def EMO_SNIPER():
    return f":{config.EMO_SNIPER_NAME}:"

def EMO_BOUNTY():
    return f":{config.EMO_BOUNTY_NAME}:"

def EMO_SHEKEL():
    return f":{config.EMO_SHEKEL_NAME}:"

def EMO_STONE():
    return f":{config.EMO_STONE_NAME}:"


def build_emoji_lookup():
    """
    Placeholder for scanning guild emojis into a cache.
    In your big file you rebuilt lookup here — wire it up later if needed.
    """
    log.info("Emoji lookup rebuilt.")


# -------------------------------------------------------------------
# Role guards
# -------------------------------------------------------------------
async def guard_worldler_inter(inter: discord.Interaction) -> bool:
    """Check if user has the Worldler role (for slash commands)."""
    if not inter.guild or not inter.user:
        return False
    role = discord.utils.get(inter.guild.roles, name=config.WORLDLER_ROLE_NAME)
    return role in getattr(inter.user, "roles", [])


async def guard_worldler_msg(msg: discord.Message) -> bool:
    """Check if message author has the Worldler role (for text shortcuts)."""
    if not msg.guild or not msg.author:
        return False
    role = discord.utils.get(msg.guild.roles, name=config.WORLDLER_ROLE_NAME)
    return role in getattr(msg.author, "roles", [])


async def is_worldler(guild: discord.Guild, member: discord.Member) -> bool:
    """Check if a member is a Worldler."""
    role = discord.utils.get(guild.roles, name=config.WORLDLER_ROLE_NAME)
    return role in getattr(member, "roles", [])
