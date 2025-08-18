# worldle_bot/features/dailies.py
"""
Daily actions (/dailies command, reactions, and panel embed).
"""

import logging
import discord
from discord import app_commands

from ..core.utils import safe_send, make_panel
from ..core.db import get_balance, change_balance, get_stones, change_stones
from ..core.config import EMO_SHEKEL, EMO_STONE
from .roles import is_worldler
from .casino import casino_start_word_pot
from .solo import solo_start

log = logging.getLogger("worldle_bot")

# Track active dailies panels
dailies_msg_ids: set[int] = set()


# -------------------------------------------------------------------
# Cooldowns
# -------------------------------------------------------------------
async def _get_cd(gid: int, uid: int) -> tuple[str | None, str | None]:
    """
    Stub for cooldown lookup.
    TODO: wire into your DB schema.
    Returns (last_pray_date, last_beg_date).
    """
    return None, None


async def _set_cd(gid: int, uid: int, field: str, value: str):
    """
    Stub for cooldown setting.
    TODO: wire into your DB schema.
    """
    return


def uk_today_str() -> str:
    """Return today's date string in UK timezone (YYYY-MM-DD)."""
    from datetime import datetime, timezone, timedelta
    uk = timezone(timedelta(hours=0))
    return datetime.now(uk).strftime("%Y-%m-%d")


# -------------------------------------------------------------------
# Reaction listeners
# -------------------------------------------------------------------
async def dailies_reaction_listener(payload: discord.RawReactionActionEvent):
    """Independent reaction handler for /dailies panels only."""
    try:
        if payload.message_id not in dailies_msg_ids:
            return

        guild = discord.utils.get(payload.guild_id and payload.cached_message.guilds or [])
        if not guild:
            return

        try:
            member = guild.get_member(payload.user_id) or await guild.fetch_member(payload.user_id)
        except Exception:
            return
        if not member or member.bot or not await is_worldler(guild, member):
            return

        channel = guild.get_channel(payload.channel_id)
        if not isinstance(channel, discord.TextChannel):
            try:
                channel = await guild.fetch_channel(payload.channel_id)
            except Exception:
                return

        emoji_name = payload.emoji.name

        if emoji_name == "🧩":
            # Start Solo
            ch = await solo_start(channel, member)
            if isinstance(ch, discord.TextChannel):
                await safe_send(channel, f"🧩 {member.mention} your solo room is {ch.mention}.")

        elif emoji_name == "🛐":
            # Pray
            gid, uid = guild.id, member.id
            today = uk_today_str()
            last_pray, _ = await _get_cd(gid, uid)
            if last_pray == today:
                await safe_send(channel, f"🛐 {member.mention} you already prayed today.")
            else:
                await change_balance(gid, uid, 5, announce_channel_id=channel.id)
                await _set_cd(gid, uid, "last_pray", today)
                bal = await get_balance(gid, uid)
                await safe_send(channel, f"🛐 {member.mention} +5 {EMO_SHEKEL()} — Balance **{bal}**")

        elif emoji_name == "🙇":
            # Beg
            gid, uid = guild.id, member.id
            today = uk_today_str()
            _, last_beg = await _get_cd(gid, uid)
            if last_beg == today:
                await safe_send(channel, f"🙇 {member.mention} you already begged today.")
            else:
                await change_stones(gid, uid, 5)
                await _set_cd(gid, uid, "last_beg", today)
                stones = await get_stones(gid, uid)
                await safe_send(channel, f"🙇 {member.mention} {EMO_STONE()} +5 Stones — You now have **{stones}**.")

        elif emoji_name == "🎰":
            # Word Pot (casino)
            ch = await casino_start_word_pot(channel, member)
            if isinstance(ch, discord.TextChannel):
                await safe_send(channel, f"🎰 {member.mention} Word Pot room: {ch.mention}")

        # Remove user’s reaction for reusability
        try:
            msg = await channel.fetch_message(payload.message_id)
            await msg.remove_reaction(payload.emoji, member)
        except Exception:
            pass

    except Exception as e:
        log.warning(f"[dailies] reaction handler error: {e}")


# -------------------------------------------------------------------
# Slash command
# -------------------------------------------------------------------
class DailiesView(discord.ui.View):
    """Buttons for /dailies panel."""

    def __init__(self, interaction: discord.Interaction, pot_amount: int = 0):
        super().__init__(timeout=None)
        self.interaction = interaction
        self.pot_amount = pot_amount
        self.message = None

    def attach_message(self, msg: discord.Message):
        self.message = msg


async def _build_dailies_embed(gid: int, uid: int) -> discord.Embed:
    """Build the daily actions embed."""
    bal = await get_balance(gid, uid)
    stones = await get_stones(gid, uid)
    emb = make_panel(
        title="Daily Actions",
        description=(
            f"🛐 Pray (+5 {EMO_SHEKEL()})\n"
            f"🙇 Beg (+5 {EMO_STONE()})\n"
            f"🧩 Start Solo Room\n"
            f"🎰 Word Pot Casino\n\n"
            f"**Balance:** {bal} {EMO_SHEKEL()} | **Stones:** {stones} {EMO_STONE()}"
        ),
        icon="📅",
    )
    return emb


@app_commands.command(name="dailies", description="Show your daily actions.")
async def dailies(interaction: discord.Interaction):
    emb = await _build_dailies_embed(interaction.guild.id, interaction.user.id)

    view = DailiesView(interaction)
    await interaction.response.send_message(embed=emb, view=view)

    try:
        msg = await interaction.original_response()
        view.attach_message(msg)
        dailies_msg_ids.add(msg.id)
    except Exception:
        pass
