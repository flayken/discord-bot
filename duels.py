# worldle_bot/features/duels.py
"""
Wordle Duels:
 - /worldle_challenge @user amount:10
 - /worldle_accept id:123
 - /worldle_duel_guess id:123 word:APPLE
 - /worldle_cancel id:123
Also exports duel_id_in_channel(ch_id) to integrate text shortcut "g WORD".
"""

from __future__ import annotations

import random
import time
from typing import Optional, Dict, Any

import discord
from discord import app_commands
from discord.ext import commands

from ..core.config import EMO_SHEKEL, ANSWERS
from ..core.utils import (
    safe_send,
    send_boxed,
    render_row,
    is_valid_guess,
    score_guess,
)

from ..core.db import (
    get_balance,
    change_balance,
)

# injected by register()
bot: commands.Bot | None = None
tree: app_commands.CommandTree | None = None

# -------------------- in-memory state --------------------
# Structure:
# duels[did] = {
#   "id": int, "guild_id": int, "channel_id": int,
#   "challenger_id": int, "target_id": int,
#   "stake": int, "pot": int, "state": "pending"|"active"|"finished"|"cancelled",
#   "created": float, "answer": str|None, "turn": int|None,
#   "guesses": {uid: [{"word": str, "colors": list[int]}], uid2: [...]}
# }
duels: Dict[int, Dict[str, Any]] = {}
_next_duel_id = 1


def _new_duel_id() -> int:
    global _next_duel_id
    did = _next_duel_id
    _next_duel_id += 1
    return did


def duel_id_in_channel(ch_id: int) -> Optional[int]:
    """Return an active duel ID for this channel, if exactly one is active."""
    for k, d in duels.items():
        if d["state"] == "active" and d["channel_id"] == ch_id:
            return k
    return None


# -------------------- commands --------------------
def _bind_commands(_tree: app_commands.CommandTree):

    @_tree.command(name="worldle_challenge", description="Challenge a player to a Wordle duel for a stake.")
    @app_commands.describe(user="Opponent", amount="Stake (shekels)")
    async def worldle_challenge(inter: discord.Interaction, user: discord.Member, amount: int):
        if not inter.guild or not inter.channel:
            return await send_boxed(inter, "Duel", "Run this in a server.", icon="⚔️", ephemeral=True)

        if user.bot or user.id == inter.user.id:
            return await send_boxed(inter, "Duel", "Pick a real opponent (not yourself/bots).", icon="⚔️", ephemeral=True)
        if amount <= 0:
            return await send_boxed(inter, "Duel", "Stake must be positive.", icon="⚔️", ephemeral=True)

        gid, cid = inter.guild.id, inter.channel.id

        # prevent either player being in a pending/active duel already
        for d in duels.values():
            if d["state"] in ("pending", "active") and (
                d["challenger_id"] in (inter.user.id, user.id)
                or d["target_id"] in (inter.user.id, user.id)
            ):
                return await send_boxed(
                    inter,
                    "Duel",
                    "Either you or they are already in a pending/active duel.",
                    icon="⚔️",
                    ephemeral=True,
                )

        if await get_balance(gid, inter.user.id) < amount:
            return await send_boxed(inter, "Duel", "You don't have enough shekels.", icon="⚔️", ephemeral=True)

        did = _new_duel_id()
        duels[did] = {
            "id": did,
            "guild_id": gid,
            "channel_id": cid,
            "challenger_id": inter.user.id,
            "target_id": user.id,
            "stake": amount,
            "pot": 0,
            "state": "pending",
            "created": time.time(),
            "answer": None,
            "turn": None,
            "guesses": {inter.user.id: [], user.id: []},
        }

        # public creation message
        await safe_send(
            inter.channel,
            f"⚔️ Duel **#{did}** created: {inter.user.mention} challenges {user.mention} for **{amount} {EMO_SHEKEL()}**.\n"
            f"{user.mention}, accept with **/worldle_accept id:{did}** or decline with **/worldle_cancel id:{did}**.",
            allowed_mentions=discord.AllowedMentions(users=[inter.user, user])
        )
        await send_boxed(inter, "Duel", "Challenge sent!", icon="⚔️", ephemeral=True)

    @_tree.command(name="worldle_accept", description="Accept a Wordle duel by ID.")
    @app_commands.describe(id="Duel ID")
    async def worldle_accept(inter: discord.Interaction, id: int):
        if not inter.guild or not inter.channel:
            return await send_boxed(inter, "Duel", "Run this in a server.", icon="⚔️", ephemeral=True)

        d = duels.get(id)
        if not d or d["state"] != "pending":
            return await send_boxed(inter, "Duel", "No such pending duel.", icon="⚔️", ephemeral=True)
        if inter.channel.id != d["channel_id"]:
            ch = inter.guild.get_channel(d["channel_id"])
            where = ch.mention if isinstance(ch, discord.TextChannel) else "the duel channel"
            return await send_boxed(inter, "Duel", f"Use this in {where}.", icon="⚔️", ephemeral=True)
        if inter.user.id != d["target_id"]:
            return await send_boxed(inter, "Duel", "Only the challenged player can accept.", icon="⚔️", ephemeral=True)
        if time.time() - d["created"] > 10 * 60:
            d["state"] = "cancelled"
            return await send_boxed(inter, "Duel", "That duel expired.", icon="⚔️", ephemeral=True)

        gid, cid = d["guild_id"], d["channel_id"]
        a, b, stake = d["challenger_id"], d["target_id"], d["stake"]

        if await get_balance(gid, a) < stake or await get_balance(gid, b) < stake:
            d["state"] = "cancelled"
            return await send_boxed(inter, "Duel", "One of you no longer has enough shekels. Duel cancelled.", icon="⚔️", ephemeral=True)

        # lock stakes
        await change_balance(gid, a, -stake, announce_channel_id=cid)
        await change_balance(gid, b, -stake, announce_channel_id=cid)

        d["pot"] = stake * 2
        d["answer"] = random.choice(ANSWERS)
        d["turn"] = random.choice([a, b])
        d["state"] = "active"

        starter = f"<@{d['turn']}>"
        await safe_send(
            inter.channel,
            f"⚔️ Duel **#{id}** started between <@{a}> and <@{b}> for **{stake}** each (**pot {d['pot']} {EMO_SHEKEL()}**).\n"
            f"Starting player chosen at random: {starter} goes first.\n"
            f"Guess with `g APPLE` here or `/worldle_duel_guess id:{id} word:APPLE`."
        )
        await send_boxed(inter, "Duel", "Accepted. Good luck!", icon="⚔️", ephemeral=True)

    @_tree.command(name="worldle_duel_guess", description="Play your turn in a Wordle duel.")
    @app_commands.describe(id="Duel ID", word="Your 5-letter guess")
    async def worldle_duel_guess(inter: discord.Interaction, id: int, word: str):
        if not inter.guild or not inter.channel:
            return await send_boxed(inter, "Duel", "Run this in a server.", icon="⚔️", ephemeral=True)

        d = duels.get(id)
        if not d or d["state"] != "active":
            return await send_boxed(inter, "Duel", "No such active duel.", icon="⚔️", ephemeral=True)
        if inter.channel.id != d["channel_id"]:
            ch = inter.guild.get_channel(d["channel_id"])
            where = ch.mention if isinstance(ch, discord.TextChannel) else "the duel channel"
            return await send_boxed(inter, "Duel", f"Use this in {where}.", icon="⚔️", ephemeral=True)

        uid = inter.user.id
        if uid not in (d["challenger_id"], d["target_id"]):
            return await send_boxed(inter, "Duel", "You're not in that duel.", icon="⚔️", ephemeral=True)
        if uid != d["turn"]:
            return await send_boxed(inter, "Duel", "It's not your turn.", icon="⚔️", ephemeral=True)

        cleaned = "".join(ch for ch in word.lower().strip() if ch.isalpha())
        if len(cleaned) != 5:
            return await send_boxed(inter, "Invalid Guess", "Guess must be exactly **5 letters**.", icon="❗", ephemeral=True)
        if not is_valid_guess(cleaned):
            return await send_boxed(inter, "Invalid Guess", "That’s not in the Wordle dictionary (UK variants supported).", icon="📚", ephemeral=True)

        colors = score_guess(cleaned, d["answer"])
        d["guesses"][uid].append({"word": cleaned, "colors": colors})
        row = render_row(cleaned, colors)

        ch = inter.channel

        # WIN?
        if cleaned == d["answer"]:
            await ch.send(row)
            await change_balance(d["guild_id"], uid, d["pot"], announce_channel_id=d["channel_id"])
            bal = await get_balance(d["guild_id"], uid)
            await safe_send(
                ch,
                f"🏁 Duel **#{id}**: {inter.user.mention} guessed **{d['answer'].upper()}** and wins the pot **{d['pot']} {EMO_SHEKEL()}**! (Balance: {bal})",
                allowed_mentions=discord.AllowedMentions(users=[inter.user])
            )
            d["state"] = "finished"
            return await send_boxed(inter, "Duel", "You win!", icon="🏁", ephemeral=True)

        # Handoff to the other player
        other = d["challenger_id"] if uid == d["target_id"] else d["target_id"]
        d["turn"] = other
        await ch.send(row)
        await ch.send(f"**Duel #{id}** — It’s now <@{other}>'s turn.")
        await send_boxed(inter, "Duel", "Move submitted.", icon="✅", ephemeral=True)

    @_tree.command(name="worldle_cancel", description="Cancel your pending duel by ID.")
    @app_commands.describe(id="Duel ID")
    async def worldle_cancel(inter: discord.Interaction, id: int):
        if not inter.guild or not inter.channel:
            return await send_boxed(inter, "Duel", "Run this in a server.", icon="⚔️", ephemeral=True)

        d = duels.get(id)
        if not d or d["state"] != "pending":
            return await send_boxed(inter, "Duel", "No such pending duel.", icon="⚔️", ephemeral=True)
        if inter.channel.id != d["channel_id"]:
            ch = inter.guild.get_channel(d["channel_id"])
            where = ch.mention if isinstance(ch, discord.TextChannel) else "the duel channel"
            return await send_boxed(inter, "Duel", f"Use this in {where}.", icon="⚔️", ephemeral=True)
        if inter.user.id not in (d["challenger_id"], d["target_id"]):
            return await send_boxed(inter, "Duel", "Only participants can cancel.", icon="⚔️", ephemeral=True)

        d["state"] = "cancelled"
        await send_boxed(inter, "Duel", "Duel cancelled.", icon="🛑", ephemeral=True)


# -------------------- Public API --------------------
def register(_bot: commands.Bot, _tree: app_commands.CommandTree) -> None:
    """Called from main.py to wire duels into the app."""
    global bot, tree
    bot = _bot
    tree = _tree
    _bind_commands(_tree)
