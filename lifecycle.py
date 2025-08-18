# worldle_bot/core/lifecycle.py
"""
Lifecycle events for the Worldle bot:
 - on_ready
 - on_guild_join
"""

import logging
import discord
from discord.ext import commands

from . import config
from .utils import build_emoji_lookup, log
from ..features.bounty import bounty_loop  # we'll define bounty later as a Cog

# These will be implemented in core/db.py and features/*
from .db import db_init
from ..features.roles import ensure_worldler_role, ensure_bounty_role
from ..features.casino import get_casino_pot
from ..features.tiers import ensure_default_tiers

log = logging.getLogger("worldle_bot")


async def on_ready(bot: commands.Bot, tree: discord.app_commands.CommandTree):
    """Runs when the bot comes online."""
    # Check dependency health (placeholder for your old log_deps_health)
    try:
        log.info("Checking dependencies… (db, redis, etc.)")
    except Exception as e:
        log.warning(f"Dependency check failed: {e}")

    # Init DB
    await db_init()

    # Ensure roles + casino pots
    for g in bot.guilds:
        try:
            await ensure_worldler_role(g)
            await ensure_bounty_role(g)
            if config.DEFAULT_TIERS:
                await ensure_default_tiers(g)
            await get_casino_pot(g.id)
        except Exception as e:
            log.warning(f"Guild init {g.id} failed: {e}")

    # Emoji cache
    build_emoji_lookup()

    # Sync global slash commands
    try:
        await tree.sync()
        log.info("Global slash commands synced.")
    except Exception as e:
        log.warning(f"Global sync failed: {e}")

    # Start bounty loop if not running
    if not bounty_loop.is_running():
        bounty_loop.start()

    me = bot.user
    print(f"✅ Logged in as {me} ({me.id})")


async def on_guild_join(guild: discord.Guild):
    """Ensure roles + casino setup when joining a new guild."""
    await ensure_worldler_role(guild)
    await ensure_bounty_role(guild)
    if config.DEFAULT_TIERS:
        await ensure_default_tiers(guild)
    await get_casino_pot(guild.id)
