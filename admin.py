# admin.py
from __future__ import annotations

import discord
from discord import app_commands

# Import emoji helpers & lookup
from .emoji import (
    EMO_BADGE, EMO_BADGE_NAME,
    EMO_CHICKEN, EMO_CHICKEN_NAME,
    EMO_SNIPER, EMO_SNIPER_NAME,
    EMO_BOUNTY, EMO_BOUNTY_NAME,
    EMO_SHEKEL, EMO_SHEKEL_NAME,
    EMO_STONE, EMO_STONE_NAME,
    build_emoji_lookup,
)

# Replace this with your actual dev/test guild ID
DEV_GUILD = discord.Object(id=YOUR_GUILD_ID_HERE)


def setup(tree: app_commands.CommandTree, bot: discord.Client) -> None:
    """
    Register admin/debug commands (guild-scoped).
    Call this from main.py after the bot is constructed.
    """

    @tree.command(
        name="ww_emoji_test",
        description="(Admin) Show how my named emojis resolve right now.",
        guild=DEV_GUILD,
    )
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

    @tree.command(
        name="ww_refresh_tiles",
        description="(Admin) Re-scan tile emojis (wl_*) without restarting.",
        guild=DEV_GUILD,
    )
    @app_commands.default_permissions(administrator=True)
    async def ww_refresh_tiles(inter: discord.Interaction):
        build_emoji_lookup()
        await inter.response.send_message("✅ Tile emoji cache rebuilt.")
