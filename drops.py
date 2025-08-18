# worldle_bot/features/drops.py
"""
Shekel drop system:
 - Random ambient drops on user messages (10% per claimed 20m slot per guild)
 - /set_drops_channel admin command
 - Collect button UI that atomically takes from the ground pot
"""

from __future__ import annotations

import random
import discord
from discord import app_commands
from discord.ext import commands

from ..core.config import (
    EMO_SHEKEL,
    SHEKEL_DROP_CHANCE,
    SHEKEL_DROP_MIN,
    SHEKEL_DROP_MAX,
)
from ..core.utils import safe_send, make_panel, send_boxed, gmt_now_s, log
from ..core.db import get_cfg, set_cfg, add_to_pot, pop_all_from_pot, get_balance, change_balance

# Bot and tree will be injected by main.py via register()
bot: commands.Bot | None = None
tree: app_commands.CommandTree | None = None


# -------------------- Collect UI --------------------
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
        btn.callback = self._on_collect  # type: ignore
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
        await change_balance(self.guild_id, interaction.user.id, taken, announce_channel_id=self.channel_id)
        self.claimed = True
        self._btn.disabled = True
        self._btn.style = discord.ButtonStyle.secondary
        self._btn.label = f"Collected by {interaction.user.display_name}"
        try:
            await interaction.response.edit_message(view=self)
        except Exception:
            pass

        # PUBLIC announcement so others see who collected it
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


# -------------------- Helpers --------------------
def _current_20m_slot() -> int:
    """Returns an integer slot id for the current 20-minute window (GMT)."""
    return int(gmt_now_s() // (20 * 60))


async def take_from_pot(gid: int, amount: int) -> int:
    """
    Atomically take up to `amount` from the ground pot for this guild.
    Mirrors your original implementation with the same table/column names.
    """
    assert bot is not None, "drops.take_from_pot called before register()"
    async with bot.db.execute("SELECT pot FROM ground WHERE guild_id=?", (gid,)) as cur:  # type: ignore[attr-defined]
        row = await cur.fetchone()
    pot = row[0] if row else 0
    take = min(max(0, int(amount)), pot)
    if take > 0:
        await bot.db.execute("UPDATE ground SET pot = pot - ? WHERE guild_id=?", (take, gid))  # type: ignore[attr-defined]
        await bot.db.commit()  # type: ignore[attr-defined]
    return take


async def _get_drops_channel(guild: discord.Guild) -> discord.TextChannel | None:
    cfg = await get_cfg(guild.id)
    ch_id = cfg.get("drops_channel_id")
    if ch_id:
        ch = guild.get_channel(ch_id)
        if isinstance(ch, discord.TextChannel) and ch.permissions_for(guild.me).send_messages:
            return ch
    return None


# -------------------- Ambient drop on messages --------------------
async def maybe_drop_shekel_on_message(msg: discord.Message):
    """
    Ambient drop:
      • At most one RNG roll per 20-minute slot *per guild* (DB-coordinated).
      • 10% chance to mint a bundle of **1–5** shekels when the slot is claimed.
      • Posts to the configured Drops channel, else the current channel.
    """
    if not msg.guild or msg.author.bot:
        return

    assert bot is not None, "drops.maybe_drop_shekel_on_message called before register()"

    gid = msg.guild.id
    slot = _current_20m_slot()

    # Claim the (guild, slot) once — others will early return
    cur = await bot.db.execute(  # type: ignore[attr-defined]
        "INSERT OR IGNORE INTO ambient_rolls(guild_id, slot) VALUES(?, ?)",
        (gid, slot),
    )
    await bot.db.commit()  # type: ignore[attr-defined]
    if getattr(cur, "rowcount", 0) == 0:
        return  # someone already rolled this 20-minute window

    # Only the claimer attempts RNG
    if random.random() >= SHEKEL_DROP_CHANCE:
        return

    # Amount: uniform 1..5
    amount = random.randint(SHEKEL_DROP_MIN, SHEKEL_DROP_MAX)

    # Mint into the ground pot
    await add_to_pot(gid, amount)

    # Post in configured Drops Channel, else current channel
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


# -------------------- Slash commands --------------------
def _bind_commands(_tree: app_commands.CommandTree):
    @_tree.command(name="set_drops_channel", description="(Admin) Set THIS channel for shekel drop announcements.")
    @app_commands.default_permissions(administrator=True)
    async def set_drops_channel_cmd(inter: discord.Interaction):
        if not inter.guild or not inter.channel:
            return await send_boxed(inter, "Drops Channel", "Server only.", icon="🪙", ephemeral=True)
        await set_cfg(inter.guild.id, drops_channel_id=inter.channel.id)
        await send_boxed(inter, "Drops Channel", f"Shekel drops will be announced in {inter.channel.mention}.", icon="🪙")

    # Optional: handy /collect (ALL) here, if you prefer it local to drops.
    @_tree.command(name="collect", description="Pick up ALL shekels from the ground.")
    async def collect(inter: discord.Interaction):
        # Assumes guard handled outside if needed; kept identical to your original behavior.
        if not inter.guild:
            return await inter.response.send_message("Server only.", ephemeral=True)
        gid, uid, cid = inter.guild.id, inter.user.id, inter.channel_id
        amt = await pop_all_from_pot(gid)
        if amt <= 0:
            return await inter.response.send_message("Nothing on the ground right now.")
        await change_balance(gid, uid, amt, announce_channel_id=cid)
        bal = await get_balance(gid, uid)
        s = "" if amt == 1 else "s"
        await inter.response.send_message(f"{EMO_SHEKEL()} {inter.user.mention} collected **{amt} shekel{s}**. Balance: **{bal}**")


# -------------------- Public API --------------------
def register(_bot: commands.Bot, _tree: app_commands.CommandTree) -> None:
    """
    Called from main.py to wire this feature up.
    - stores bot/tree for DB & command access
    - binds slash commands
    """
    global bot, tree
    bot = _bot
    tree = _tree
    _bind_commands(_tree)
