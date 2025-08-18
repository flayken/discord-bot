# worldle_bot/features/casino.py
"""
Casino / Word Pot feature.
"""

import discord
from discord.ext import commands

from ..core.utils import safe_send, send_boxed
from ..core.config import EMO_SHEKEL
from ..core.db import (
    get_casino_pot,
    change_balance,
    get_balance,
)

bot: commands.Bot  # injected in main.py

# Active games keyed by (guild, channel, user)
casino_games: dict[tuple[int, int, int], dict] = {}


# --------------------------------------------------------
# Helpers
# --------------------------------------------------------
def _key(gid: int, cid: int, uid: int) -> tuple[int, int, int]:
    return (gid, cid, uid)


# --------------------------------------------------------
# Start a new Word Pot game
# --------------------------------------------------------
async def casino_start_word_pot(channel: discord.TextChannel, member: discord.Member):
    gid, cid, uid = member.guild.id, channel.id, member.id
    key = _key(gid, cid, uid)

    if key in casino_games:
        await safe_send(channel, f"{member.mention} you already have a Word Pot running here.")
        return None

    # Ensure casino pot exists
    await get_casino_pot(gid)

    game = {
        "state": "active",
        "answer": None,  # TODO: plug in word generator
        "bets": {},
        "channel": cid,
        "user": uid,
    }
    casino_games[key] = game

    emb = discord.Embed(
        title="🎰 Word Pot",
        description="Game started! Guess words with `g WORD`.",
        color=discord.Color.gold(),
    )
    await safe_send(channel, embed=emb)
    return channel


# --------------------------------------------------------
# Handle guesses
# --------------------------------------------------------
async def casino_guess(channel: discord.TextChannel, member: discord.Member, word: str):
    gid, cid, uid = member.guild.id, channel.id, member.id
    key = _key(gid, cid, uid)

    game = casino_games.get(key)
    if not game or game["state"] != "active":
        return

    # TODO: guessing logic
    # For now, just echo the guess
    await safe_send(channel, f"{member.mention} guessed **{word.upper()}**.")

    # Example: award balance (placeholder)
    await change_balance(gid, uid, 1, announce_channel_id=cid)
    bal = await get_balance(gid, uid)
    await safe_send(channel, f"{member.mention} +1 {EMO_SHEKEL()} — Balance **{bal}**")
