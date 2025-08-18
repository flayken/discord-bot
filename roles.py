# worldle_bot/features/roles.py
"""
Guild role management for Worldle bot.
 - Ensure the Worldler role (for access gating)
 - Ensure the Bounty role (for hourly bounty events)
"""

import logging
import discord

from ..core import config

log = logging.getLogger("worldle_bot")


# -------------------------------------------------------------------
# Ensure Worldler role
# -------------------------------------------------------------------
async def ensure_worldler_role(guild: discord.Guild) -> int | None:
    """
    Ensure the WORLDLER_ROLE_NAME exists.
    Returns role.id or None.
    """
    role = discord.utils.get(guild.roles, name=config.WORLDLER_ROLE_NAME)
    if not role:
        try:
            role = await guild.create_role(
                name=config.WORLDLER_ROLE_NAME,
                mentionable=True,
                reason="Worldle Bot setup: required role"
            )
            log.info(f"[roles] Created {config.WORLDLER_ROLE_NAME} in {guild.name}")
        except Exception as e:
            log.warning(f"[roles] Could not create {config.WORLDLER_ROLE_NAME} in {guild.name}: {e}")
            return None
    return role.id


# -------------------------------------------------------------------
# Ensure Bounty role
# -------------------------------------------------------------------
async def ensure_bounty_role(guild: discord.Guild) -> int | None:
    """
    Ensure the BOUNTY_ROLE_NAME exists.
    Returns role.id or None.
    """
    role = discord.utils.get(guild.roles, name=config.BOUNTY_ROLE_NAME)
    if not role:
        try:
            role = await guild.create_role(
                name=config.BOUNTY_ROLE_NAME,
                mentionable=True,
                reason="Worldle Bot setup: bounty participants"
            )
            log.info(f"[roles] Created {config.BOUNTY_ROLE_NAME} in {guild.name}")
        except Exception as e:
            log.warning(f"[roles] Could not create {config.BOUNTY_ROLE_NAME} in {guild.name}: {e}")
            return None
    return role.id


# -------------------------------------------------------------------
# Membership guard
# -------------------------------------------------------------------
async def is_worldler(guild: discord.Guild, member: discord.Member) -> bool:
    """Check if member has the Worldler role."""
    role = discord.utils.get(guild.roles, name=config.WORLDLER_ROLE_NAME)
    if not role:
        return False
    return role in member.roles


async def guard_worldler_msg(msg: discord.Message) -> bool:
    """Convenience guard for message-based shortcuts (returns True if allowed)."""
    if not msg.guild or not isinstance(msg.author, discord.Member):
        return False
    return await is_worldler(msg.guild, msg.author)
