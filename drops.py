# worldle_bot/features/drops.py
"""
Shekel drop feature: ambient random drops + collect button.
"""

import random
import discord
from discord import app_commands
from discord.ext import commands

from ..core.utils import safe_send, make_panel, send_boxed
from ..core.config import (
    EMO_SHEKEL,
    SHEKEL_DROP_CHANCE,
    SHEKEL_DROP_MIN,
    SHEKEL_DROP_MAX,
)
from ..core.db import add_to_pot, take_from_pot, get_cfg, set_cfg

bot: commands.Bot  # injected in main.py


# --------------------------------------------------------
# Shekel drop View
# --------------------------------------------------------
class ShekelDropView(discord.ui.View):
    def __init__(self, guild_id: int, channel_id: int, amount: int = 1, timeout: float = 600):
        super().__init__(timeout=timeout)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.amount = int(max(1, amount))
        self.claimed = False

        btn = discord.ui.Button(
            label=f"Collect {self.amount}",
            style=discord.ButtonStyle.success,
            emoji=EMO_SHEKEL(),
        )
        btn.callback = self._on_collect
        self.add_item(btn)
        self._btn = btn

    async def _on_collect(self, interaction: discord.Interaction):
        if not interaction.guild or interaction.guild.id != self.guild_id:
            return await interaction.response.send_message("Wrong server for this drop.", ephemeral=True)
        if self.claimed:
            return await interaction.response.send_message("Too late — already collected.", ephemeral=True)

        taken = await take_from_pot(self.guild_id, self.amount)
        if taken <= 0:
            self.claimed = True
            self._btn.disabled = True
            self._btn.style = discord.ButtonStyle.secondary
            self._btn.label = "Already taken"
            try:
                await interaction.response.edit_message(view=self)
            except Exception:
                pass
            return await interaction.followup.send("Someone scooped it, or `/collect` emptied the ground.", ephemeral=True)

        # Award and update the button
        self.claimed = True
        self._btn.disabled = True
        self._btn.style = discord.ButtonStyle.secondary
        self._btn.label = f"Collected by {interaction.user.display_name}"
        try:
            await interaction.response.edit_message(view=self)
        except Exception:
            pass

        # PUBLIC announcement
        s = "" if taken == 1 else "s"
        try:
            ch = interaction.channel or interaction.guild.get_channel(self.channel_id)
            await safe_send(
                ch,
                f"{EMO_SHEKEL()} {interaction.user.mention} collected **{taken} shekel{s}**.",
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )
        except Exception:
            await interaction.followup.send(f"You collected **{taken} shekel{s}**.", ephemeral=True)


# --------------------------------------------------------
# Ambient RNG shekel drops
# --------------------------------------------------------
async def maybe_drop_shekel_on_message(msg: discord.Message):
    """
    Ambient drop:
      • At most one RNG roll per 20-minute slot per guild.
      • 10% chance to mint a bundle of 1–5 shekels.
    """
    if not msg.guild or msg.author.bot:
        return

    gid = msg.guild.id
    slot = _current_20m_slot()

    # Attempt to claim the slot
    cur = await bot.db.execute(
        "INSERT OR IGNORE INTO ambient_rolls(guild_id, slot) VALUES(?, ?)",
        (gid, slot),
    )
    await bot.db.commit()
    if getattr(cur, "rowcount", 0) == 0:
        return  # already rolled this slot

    if random.random() >= SHEKEL_DROP_CHANCE:  # e.g. 10%
        return

    amount = random.randint(SHEKEL_DROP_MIN, SHEKEL_DROP_MAX)
    await add_to_pot(gid, amount)

    target = await _get_drops_channel(msg.guild) or msg.channel
    emb = make_panel(
        title="💰 Shekel Drop!",
        description=(
            f"A shiny {EMO_SHEKEL()} hit the floor — **{amount}**!\n"
            "Press **Collect** to grab this bundle, or use `/collect` to scoop **everything** on the ground."
        ),
        icon="🪙",
    )
    view = ShekelDropView(guild_id=gid, channel_id=target.id, amount=amount)

    await safe_send(
        target,
        embed=emb,
        view=view,
        allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
    )


# --------------------------------------------------------
# DB + helper
# --------------------------------------------------------
def _current_20m_slot() -> int:
    return int(discord.utils.utcnow().timestamp() // (20 * 60))


async def _get_drops_channel(guild: discord.Guild) -> discord.TextChannel | None:
    cfg = await get_cfg(guild.id)
    ch_id = cfg.get("drops_channel_id")
    if ch_id:
        ch = guild.get_channel(ch_id)
        if isinstance(ch, discord.TextChannel) and ch.permissions_for(guild.me).send_messages:
            return ch
    return None


# --------------------------------------------------------
# Admin command
# --------------------------------------------------------
@bot.tree.command(name="set_drops_channel", description="(Admin) Set THIS channel for shekel drop announcements.")
@app_commands.default_permissions(administrator=True)
async def set_drops_channel_cmd(inter: discord.Interaction):
    if not inter.guild or not inter.channel:
        return await send_boxed(inter, "Drops Channel", "Server only.", icon="🪙", ephemeral=True)
    await set_cfg(inter.guild.id, drops_channel_id=inter.channel.id)
    await send_boxed(inter, "Drops Channel", f"Shekel drops will be announced in {inter.channel.mention}.", icon="🪙")
