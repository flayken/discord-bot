# worldle_bot/features/duels.py
from __future__ import annotations

import random
import time
from typing import Dict, Any, Optional

import discord
from discord import app_commands
from discord.ext import commands

from ..core.config import EMO_SHEKEL
from ..core.utils import (
    guard_worldler_inter,
    send_boxed,
    score_guess,
    is_valid_guess,
    render_row,
    safe_send,
)
from ..core.db import (
    get_balance,
    change_balance,
)

# -------------------- module state --------------------
bot: commands.Bot | None = None
tree: app_commands.CommandTree | None = None

# duel_id -> duel dict
duels: Dict[int, Dict[str, Any]] = {}
_next_duel_id: int = 1


# -------------------- helpers --------------------
def _new_duel_id() -> int:
    global _next_duel_id
    did = _next_duel_id
    _next_duel_id += 1
    return did


def _duel_in_channel(ch_id: int) -> Optional[int]:
    for k, d in duels.items():
        if d["state"] == "active" and d["channel_id"] == ch_id:
            return k
    return None


# -------------------- slash commands --------------------
def _bind_commands(_tree: app_commands.CommandTree):

    @_tree.command(name="worldle_challenge", description="Challenge a player to a Wordle duel for a stake.")
    @app_commands.describe(user="Opponent", amount="Stake (shekels)")
    async def worldle_challenge(inter: discord.Interaction, user: discord.Member, amount: int):
        if not await guard_worldler_inter(inter):
            return
        if not inter.guild or not inter.channel:
            return
        if user.bot or user.id == inter.user.id:
            return await inter.response.send_message("Pick a real opponent (not yourself/bots).", ephemeral=True)
        if amount <= 0:
            return await inter.response.send_message("Stake must be positive.", ephemeral=True)

        gid, cid = inter.guild.id, inter.channel.id

        # no overlapping duels for either participant
        for d in duels.values():
            if d["state"] in ("pending", "active") and (
                d["challenger_id"] in (inter.user.id, user.id)
                or d["target_id"] in (inter.user.id, user.id)
            ):
                return await inter.response.send_message(
                    "Either you or they are already in a pending/active duel.", ephemeral=True
                )

        if await get_balance(gid, inter.user.id) < amount:
            return await inter.response.send_message("You don't have enough shekels.", ephemeral=True)

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

        await inter.response.send_message(
            f"⚔️ Duel **#{did}** created: {inter.user.mention} challenges {user.mention} for **{amount} {EMO_SHEKEL()}**.\n"
            f"{user.mention}, accept with **/worldle_accept id:{did}** or decline with **/worldle_cancel id:{did}**.",
            allowed_mentions=discord.AllowedMentions(users=[inter.user, user])
        )

    @_tree.command(name="worldle_accept", description="Accept a Wordle duel by ID.")
    @app_commands.describe(id="Duel ID")
    async def worldle_accept(inter: discord.Interaction, id: int):
        if not await guard_worldler_inter(inter):
            return
        d = duels.get(id)
        if not d or d["state"] != "pending":
            return await inter.response.send_message("No such pending duel.", ephemeral=True)
        if inter.channel.id != d["channel_id"]:
            ch = inter.guild.get_channel(d["channel_id"])
            return await inter.response.send_message(
                f"Use this in {ch.mention if ch else 'the duel channel'}.", ephemeral=True
            )
        if inter.user.id != d["target_id"]:
            return await inter.response.send_message("Only the challenged player can accept.", ephemeral=True)
        if time.time() - d["created"] > 10 * 60:
            d["state"] = "cancelled"
            return await inter.response.send_message("That duel expired.", ephemeral=True)

        gid, cid = d["guild_id"], d["channel_id"]
        a, b, stake = d["challenger_id"], d["target_id"], d["stake"]
        if await get_balance(gid, a) < stake or await get_balance(gid, b) < stake:
            d["state"] = "cancelled"
            return await inter.response.send_message(
                "One of you no longer has enough shekels. Duel cancelled.", ephemeral=True
            )

        # take stakes into pot
        await change_balance(gid, a, -stake, announce_channel_id=cid)
        await change_balance(gid, b, -stake, announce_channel_id=cid)
        d["pot"] = stake * 2

        # pick answer + starting player
        from ..core.config import ANSWERS
        d["answer"] = random.choice(ANSWERS)
        d["turn"] = random.choice([a, b])
        d["state"] = "active"

        ch = inter.channel
        starter = f"<@{d['turn']}>"
        await ch.send(
            f"⚔️ Duel **#{id}** started between <@{a}> and <@{b}> for **{stake}** each "
            f"(**pot {d['pot']} {EMO_SHEKEL()}**).\n"
            f"Starting player chosen at random: {starter} goes first.\n"
            f"Guess with `g APPLE` here or `/worldle_duel_guess id:{id} word:APPLE`."
        )
        await inter.response.send_message("Accepted. Good luck!", ephemeral=True)

    @_tree.command(name="worldle_duel_guess", description="Play your turn in a Wordle duel.")
    @app_commands.describe(id="Duel ID", word="Your 5-letter guess")
    async def worldle_duel_guess(inter: discord.Interaction, id: int, word: str):
        if not await guard_worldler_inter(inter):
            return
        d = duels.get(id)
        if not d or d["state"] != "active":
            return await inter.response.send_message("No such active duel.", ephemeral=True)
        if inter.channel.id != d["channel_id"]:
            ch = inter.guild.get_channel(d["channel_id"])
            return await inter.response.send_message(
                f"Use this in {ch.mention if ch else 'the duel channel'}.", ephemeral=True
            )

        uid = inter.user.id
        if uid not in (d["challenger_id"], d["target_id"]):
            return await inter.response.send_message("You're not in that duel.", ephemeral=True)
        if uid != d["turn"]:
            return await inter.response.send_message("It's not your turn.", ephemeral=True)

        cleaned = "".join(ch for ch in word.lower().strip() if ch.isalpha())
        if len(cleaned) != 5:
            return await inter.response.send_message("Guess must be exactly 5 letters.", ephemeral=True)
        if not is_valid_guess(cleaned):
            return await inter.response.send_message(
                "That’s not in the Wordle dictionary (UK variants supported).", ephemeral=True
            )

        colors = score_guess(cleaned, d["answer"])
        d["guesses"][uid].append({"word": cleaned, "colors": colors})
        row = render_row(cleaned, colors)

        ch = inter.channel

        # WIN
        if cleaned == d["answer"]:
            await ch.send(row)
            await change_balance(d["guild_id"], uid, d["pot"], announce_channel_id=d["channel_id"])
            bal = await get_balance(d["guild_id"], uid)
            await ch.send(
                f"🏁 Duel **#{id}**: {inter.user.mention} guessed **{d['answer'].upper()}** "
                f"and wins the pot **{d['pot']} {EMO_SHEKEL()}**! (Balance: {bal})"
            )
            d["state"] = "finished"
            return await inter.response.send_message("You win!", ephemeral=True)

        # continue → switch turns
        other = d["challenger_id"] if uid == d["target_id"] else d["target_id"]
        d["turn"] = other
        await ch.send(row)
        await ch.send(f"**Duel #{id}** — It’s now <@{other}>'s turn.")
        await inter.response.send_message("Move submitted.", ephemeral=True)

    @_tree.command(name="worldle_cancel", description="Cancel your pending duel by ID.")
    @app_commands.describe(id="Duel ID")
    async def worldle_cancel(inter: discord.Interaction, id: int):
        if not await guard_worldler_inter(inter):
            return
        d = duels.get(id)
        if not d or d["state"] != "pending":
            return await inter.response.send_message("No such pending duel.", ephemeral=True)
        if inter.channel.id != d["channel_id"]:
            ch = inter.guild.get_channel(d["channel_id"])
            return await inter.response.send_message(
                f"Use this in {ch.mention if ch else 'the duel channel'}.", ephemeral=True
            )
        if inter.user.id not in (d["challenger_id"], d["target_id"]):
            return await inter.response.send_message("Only participants can cancel.", ephemeral=True)
        d["state"] = "cancelled"
        await inter.response.send_message("Duel cancelled.", ephemeral=True)


# -------------------- text shortcut router hook --------------------
async def maybe_route_guess(message: discord.Message, word: str) -> bool:
    """If this channel has an active duel and it's the author's turn, route guess here."""
    if not message.guild:
        return False
    did = _duel_in_channel(message.channel.id)
    if not did:
        return False
    d = duels.get(did)
    if not d or d["state"] != "active":
        return False
    if message.author.id != d["turn"]:
        # It is a duel channel, but not their turn — let caller handle other routes.
        return False

    # mimic slash handler
    cleaned = "".join(ch for ch in word.lower().strip() if ch.isalpha())
    if len(cleaned) != 5 or not is_valid_guess(cleaned):
        await send_boxed(message.channel, "Invalid Guess", "Guess must be **exactly 5 letters** and in dictionary.", icon="📚")
        return True

    colors = score_guess(cleaned, d["answer"])
    d["guesses"][message.author.id].append({"word": cleaned, "colors": colors})
    row = render_row(cleaned, colors)

    # win
    if cleaned == d["answer"]:
        await message.channel.send(row)
        await change_balance(d["guild_id"], message.author.id, d["pot"], announce_channel_id=d["channel_id"])
        bal = await get_balance(d["guild_id"], message.author.id)
        await message.channel.send(
            f"🏁 Duel **#{did}**: {message.author.mention} guessed **{d['answer'].upper()}** "
            f"and wins the pot **{d['pot']} {EMO_SHEKEL()}**! (Balance: {bal})"
        )
        d["state"] = "finished"
        return True

    # switch turns
    other = d["challenger_id"] if message.author.id == d["target_id"] else d["target_id"]
    d["turn"] = other
    await message.channel.send(row)
    await message.channel.send(f"**Duel #{did}** — It’s now <@{other}>'s turn.")
    return True


# -------------------- registration --------------------
def register(_bot: commands.Bot, _tree: app_commands.CommandTree) -> None:
    global bot, tree
    bot, tree = _bot, _tree
    _bind_commands(_tree)
