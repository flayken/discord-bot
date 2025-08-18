# worldle_bot/features/dailies.py
"""
Daily actions feature.
- /dailies command
- Reaction handlers for pray, beg, solo, word pot
"""

import discord
from discord import app_commands
from discord.ext import commands

from ..core.utils import safe_send
from ..core.db import (
    get_balance, change_balance, change_stones, get_stones,
    _get_cd, _set_cd, get_word_pot_total
)
from ..features.roles import is_worldler
from ..features.casino import casino_start_word_pot
from ..features.solo import solo_start, solo_guess
from ..core.config import EMO_SHEKEL, EMO_STONE, uk_today_str

bot: commands.Bot
tree: app_commands.CommandTree
log = None  # injected in main.py

# Track dailies messages for reaction listening
dailies_msg_ids: set[int] = set()


# -------------------------------------------------------------------
# Reaction Handlers
# -------------------------------------------------------------------
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
            try:
                channel = await guild.fetch_channel(payload.channel_id)
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
                await safe_send(channel, f"🛐 {member.mention} you already prayed today.", 
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
                await safe_send(channel, f"🙇 {member.mention} you already begged today.",
                                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
            else:
                await change_stones(gid, uid, 5)
                await _set_cd(gid, uid, "last_beg", today)
                stones = await get_stones(gid, uid)
                await safe_send(channel, f"🙇 {member.mention} {EMO_STONE()} +5 Stones — You now have **{stones}**.",
                                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))

        # 🎰 Word Pot
        elif emoji_name == "🎰":
            ch = await casino_start_word_pot(channel, member)
            if isinstance(ch, discord.TextChannel):
                await safe_send(
                    channel,
                    f"🎰 {member.mention} Word Pot room: {ch.mention}",
                    allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False)
                )

        # Tidy up: remove the user’s reaction
        try:
            msg = await channel.fetch_message(payload.message_id)
            await msg.remove_reaction(payload.emoji, member)
        except Exception:
            pass

    except Exception as e:
        log.warning(f"[dailies] reaction handler error: {e}")


async def dailies_raw_reaction_add(payload: discord.RawReactionActionEvent):
    """Reaction handler for /dailies panels (with refresh)."""
    try:
        if payload.message_id not in dailies_msg_ids:
            return
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

        channel = guild.get_channel(getattr(payload, "channel_id", 0))
        if not isinstance(channel, discord.TextChannel):
            try:
                channel = await guild.fetch_channel(getattr(payload, "channel_id", 0))
            except Exception:
                return

        emoji_name = payload.emoji.name

        if emoji_name == "🧩":
            ch = await solo_start(channel, member)
            if isinstance(ch, discord.TextChannel):
                await safe_send(channel, f"🧩 {member.mention} your solo room is {ch.mention}.",
                                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))

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

        elif emoji_name == "🎰":
            ch = await casino_start_word_pot(channel, member)
            if isinstance(ch, discord.TextChannel):
                await safe_send(channel, f"🎰 {member.mention} Word Pot room: {ch.mention}",
                                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))

        # Remove reaction
        try:
            msg = await channel.fetch_message(payload.message_id)
            await msg.remove_reaction(payload.emoji, member)
        except Exception:
            pass

        # 🔄 Refresh panel
        try:
            msg = await channel.fetch_message(payload.message_id)
            new_emb = await _build_dailies_embed(guild.id, member.id)
            await msg.edit(embed=new_emb)
        except Exception:
            pass

    except Exception as e:
        log.warning(f"[dailies] reaction handler error: {e}")


# -------------------------------------------------------------------
# Slash Command
# -------------------------------------------------------------------
@tree.command(name="dailies", description="Show your daily actions.")
async def dailies(interaction: discord.Interaction):
    emb = await _build_dailies_embed(interaction.guild.id, interaction.user.id)
    pot_amount = await get_word_pot_total(interaction.guild.id)

    view = DailiesView(interaction, pot_amount=pot_amount)
    await interaction.response.send_message(embed=emb, view=view)

    try:
        msg = await interaction.original_response()
        view.attach_message(msg)
        dailies_msg_ids.add(msg.id)
    except Exception:
        pass
