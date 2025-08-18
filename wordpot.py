# worldle_bot/features/casino/word_pot.py
from __future__ import annotations

import random
from typing import Dict, Any, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands

from ...core.config import (
    EMO_SHEKEL,
    EMO_STONE,
    ANSWERS,
    CARD_COLOR_SUCCESS,
    CARD_COLOR_FAIL,
)
from ...core.db import (
    change_balance,
    get_balance,
    get_casino_pot,
    set_casino_pot,
)
from ...core.utils import (
    send_boxed,
    safe_send,
    make_card,
    score_guess,
    is_valid_guess,
    render_board,
    render_row,
    update_legend,
    legend_overview,
    payout_for_attempt,
    guard_worldler_inter,
)
from ..dailies import DailiesView  # for refresh_pot_label_for_guild

# ---------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------
bot: commands.Bot | None = None
tree: app_commands.CommandTree | None = None

# Track active Word Pot games and per-user casino channel mapping
# Keys align with monolith: (guild_id, channel_id, user_id)
casino_games: Dict[Tuple[int, int, int], Dict[str, Any]] = {}
casino_channels: Dict[Tuple[int, int], int] = {}   # (guild_id, user_id) -> channel_id

CASINO_BASE_POT = 10  # initial pot after a win (same default you had)
ENTRY_COST = 1        # cost to start a Word Pot
MAX_TRIES = 3


# ---------------------------------------------------------------------
# Public helpers (used by other modules, e.g., text shortcuts)
# ---------------------------------------------------------------------
def _key(gid: int, cid: int, uid: int) -> Tuple[int, int, int]:
    return (gid, cid, uid)


async def start_word_pot(invocation_channel: discord.TextChannel, user: discord.Member) -> Optional[discord.TextChannel]:
    """Create a private solo-style channel and start a Word Pot game for `user`."""
    guild = invocation_channel.guild
    gid, uid = guild.id, user.id

    # Check balance
    bal = await get_balance(gid, uid)
    if bal < ENTRY_COST:
        await send_boxed(invocation_channel, "Word Pot", f"{user.mention} you need **{ENTRY_COST} {EMO_SHEKEL()}** to play.", icon="🎰")
        return None

    # If user already has an active casino room, point them to it
    existing_cid = casino_channels.get((gid, uid))
    if existing_cid and _key(gid, existing_cid, uid) in casino_games:
        ch = guild.get_channel(existing_cid)
        if isinstance(ch, discord.TextChannel):
            await send_boxed(invocation_channel, "Word Pot", f"{user.mention} you already have a game running: {ch.mention}", icon="🎰")
            return ch
        else:
            casino_channels.pop((gid, uid), None)

    # Make a private channel similar to your solo room factory (reuse your category/ACL rules)
    ch = await _make_private_room(invocation_channel, user, suffix="word-pot")
    if not ch:
        return None

    # Charge entry fee in the new room for audit
    await change_balance(gid, uid, -ENTRY_COST, announce_channel_id=ch.id)

    # Register game
    answer = random.choice(ANSWERS)
    casino_games[_key(gid, ch.id, uid)] = {
        "answer": answer,
        "guesses": [],
        "legend": {},
        "max": MAX_TRIES,
        "origin_cid": invocation_channel.id,
        "staked": ENTRY_COST,
    }
    casino_channels[(gid, uid)] = ch.id

    pot = await get_casino_pot(gid)
    await send_boxed(
        ch,
        "🎰 Word Pot",
        (
            f"{user.mention} • Entry: **{ENTRY_COST} {EMO_SHEKEL()}** (paid)\n"
            f"• Current Pot: **{pot} {EMO_SHEKEL()}** (resets to {CASINO_BASE_POT} on win)\n"
            f"• You have **{MAX_TRIES} tries** — solve within {MAX_TRIES} to **win the pot**.\n"
            "If you fail, your entry adds **+1** to the pot."
        ),
        icon="🎰",
    )
    await ch.send(render_board([], total_rows=MAX_TRIES))
    return ch


async def guess_word_pot(channel: discord.TextChannel, user: discord.Member, word: str):
    """Submit a guess in an active Word Pot channel."""
    gid, cid, uid = channel.guild.id, channel.id, user.id
    game = casino_games.get(_key(gid, cid, uid))
    if not game:
        await send_boxed(channel, "Word Pot", f"{user.mention} no Word Pot game here. Start with `/worldle_casino`.", icon="🎰")
        return

    cleaned = "".join(ch for ch in word.lower().strip() if ch.isalpha())
    if len(cleaned) != 5:
        await send_boxed(channel, "Invalid Guess", "Guess must be **exactly 5 letters**.", icon="❗")
        return
    if not is_valid_guess(cleaned):
        await send_boxed(channel, "Invalid Guess", "That’s not in the Wordle dictionary (UK variants supported).", icon="📚")
        return
    if len(game["guesses"]) >= game["max"]:
        await send_boxed(channel, "Word Pot", "Out of tries! Start a new one with `/worldle_casino`.", icon="🎰")
        return

    colors = score_guess(cleaned, game["answer"])
    game["guesses"].append({"word": cleaned, "colors": colors})
    update_legend(game["legend"], cleaned, colors)
    attempt = len(game["guesses"])

    board = render_board(game["guesses"], total_rows=MAX_TRIES)
    await safe_send(channel, board)

    # WIN
    if cleaned == game["answer"]:
        pot = await get_casino_pot(gid)
        await change_balance(gid, uid, pot, announce_channel_id=cid)
        bal_new = await get_balance(gid, uid)
        ans = game["answer"].upper()
        origin_cid = game.get("origin_cid")

        # cleanup
        _cleanup_casino(gid, cid, uid)

        # reset pot
        await set_casino_pot(gid, CASINO_BASE_POT)
        await DailiesView.refresh_pot_label_for_guild(gid)

        await send_boxed(
            channel,
            "🏆 Word Pot — WIN",
            f"{user.mention} solved **{ans}** on attempt **{attempt}** and **WON {pot} {EMO_SHEKEL()}**!\nPot resets to **{CASINO_BASE_POT}**. (Balance: {bal_new})",
            icon="🎰",
        )

        emb = make_card(
            title="🎰 Word Pot — WIN",
            description=f"{user.mention} won **{pot} {EMO_SHEKEL()}** by solving **{ans}** on attempt **{attempt}**.",
            fields=[
                ("Board", board, False),
                ("Next Pot", f"Resets to **{CASINO_BASE_POT}**", True),
            ],
            color=CARD_COLOR_SUCCESS,
        )
        await _announce_result(channel.guild, origin_cid, content="", embed=emb)

        try:
            await channel.delete(reason="Word Pot finished (win)")
        except Exception:
            pass
        return

    # FAIL (out of tries)
    if attempt == game["max"]:
        cur_pot = await get_casino_pot(gid)
        add_amt = (game.get("staked", 0) or 0)
        new_pot = cur_pot + add_amt
        await set_casino_pot(gid, new_pot)
        await DailiesView.refresh_pot_label_for_guild(gid)

        ans = game["answer"].upper()
        origin_cid = game.get("origin_cid")

        _cleanup_casino(gid, cid, uid)

        fields = [("Board", board, False), ("Pot", f"Now **{new_pot} {EMO_SHEKEL()}**", True)]
        await send_boxed(
            channel,
            "🎰 Word Pot — Failed",
            f"❌ The word was **{ans}**.",
            icon="🎰",
            fields=fields,
        )

        emb = make_card(
            title="🎰 Word Pot — Failed",
            description=f"{user.mention} failed **Word Pot** — the word was **{ans}**.",
            fields=fields,
            color=CARD_COLOR_FAIL,
        )
        await _announce_result(channel.guild, origin_cid, content="", embed=emb)

        try:
            await channel.delete(reason="Word Pot finished (fail)")
        except Exception:
            pass
        return

    # MID-GAME STATUS
    legend = legend_overview(game["legend"], game["guesses"])
    msg = f"Attempt **{attempt}/{MAX_TRIES}** — solve within **{MAX_TRIES}** to win the pot."
    flds = [("Next", msg, False)]
    if legend:
        flds.append(("Legend", legend, False))
    await send_boxed(channel, "Word Pot — Status", "", icon="🎰", fields=flds)


# ---------------------------------------------------------------------
# Slash command wrapper
# ---------------------------------------------------------------------
def _bind_commands(_tree: app_commands.CommandTree):

    @_tree.command(name="worldle_casino", description="Play a casino Wordle. First game: Word Pot.")
    @app_commands.describe(game="Pick a casino game")
    @app_commands.choices(game=[app_commands.Choice(name="Word Pot", value="word_pot")])
    async def worldle_casino(inter: discord.Interaction, game: Optional[app_commands.Choice[str]] = None):
        if not await guard_worldler_inter(inter):
            return
        if not inter.guild or not inter.channel:
            return
        await inter.response.defer(thinking=False)
        choice = (game.value if game else "word_pot")
        if choice != "word_pot":
            return await send_boxed(inter, "Casino", "Only **Word Pot** is available right now.", icon="🎰")
        ch = await start_word_pot(inter.channel, inter.user)
        if isinstance(ch, discord.TextChannel):
            await send_boxed(inter, "Word Pot Room Opened", f"{inter.user.mention} your room is {ch.mention}.", icon="🎰")


# ---------------------------------------------------------------------
# Channel factory (same semantics as your solo room maker)
# ---------------------------------------------------------------------
async def _make_private_room(invocation_channel: discord.TextChannel, member: discord.Member) -> Optional[discord.TextChannel]:
    guild = invocation_channel.guild
    me = guild.me
    if not me or not me.guild_permissions.manage_channels:
        await invocation_channel.send("I need **Manage Channels** to open your private Wordle room.", delete_after=20)
        return None

    # Reuse the same category + worldler role semantics as solo rooms
    from ...core.db import get_cfg, ensure_worldler_role  # local import to avoid cycles
    cfg = await get_cfg(guild.id)
    rid = cfg.get("worldler_role_id") or await ensure_worldler_role(guild)
    worldler_role = guild.get_role(rid) if rid else None

    category = guild.get_channel(cfg.get("solo_category_id") or 0)
    if not isinstance(category, discord.CategoryChannel):
        category = None

    import re as _re
    base = _re.sub(r"[^a-zA-Z0-9]+", "-", member.display_name).strip("-").lower() or f"user-{member.id}"
    base = f"{base}-worldpot"
    name = base
    i = 2
    while discord.utils.get(guild.text_channels, name=name):
        name = f"{base}-{i}"; i += 1

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False, mention_everyone=False),
        member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, mention_everyone=False),
        me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True, mention_everyone=False),
    }
    if worldler_role:
        overwrites[worldler_role] = discord.PermissionOverwrite(view_channel=True, send_messages=False, read_message_history=True, mention_everyone=False)

    ch = await guild.create_text_channel(name=name, overwrites=overwrites, category=category, reason="Wordle World — Word Pot")
    return ch


# ---------------------------------------------------------------------
# Announcements helper (matches bounty’s helper)
# ---------------------------------------------------------------------
async def _announce_result(guild: discord.Guild, origin_cid: Optional[int], content: str = "", embed: Optional[discord.Embed] = None):
    """Post to configured announcements channel (no-op if unset)."""
    if not guild:
        return
    try:
        from ...core.db import get_cfg
        cfg = await get_cfg(guild.id)
        ann_id = cfg.get("announcements_channel_id")
        if not ann_id:
            return
        ch = guild.get_channel(ann_id)
        if not isinstance(ch, discord.TextChannel):
            return
        # include a small 'from' footer if we can (mimic your monolith feel)
        if origin_cid and embed and not embed.footer:
            src = guild.get_channel(origin_cid)
            if isinstance(src, discord.TextChannel):
                embed.set_footer(text=f"from {src.name}")
        await safe_send(ch, content=content or None, embed=embed)
    except Exception:
        pass


# ---------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------
def _cleanup_casino(gid: int, cid: int, uid: int) -> None:
    casino_games.pop(_key(gid, cid, uid), None)
    if casino_channels.get((gid, uid)) == cid:
        casino_channels.pop((gid, uid), None)


# ---------------------------------------------------------------------
# Router for text shortcuts (`wc` starts; guesses go via `g WORD` in channel)
# ---------------------------------------------------------------------
async def maybe_start_from_text(message: discord.Message) -> bool:
    """Start Word Pot if the user typed `wc`."""
    if not message.guild or not isinstance(message.channel, discord.TextChannel):
        return False
    if message.content.strip().lower() != "wc":
        return False

    # worldler guard here uses your `is_worldler` path; reusing inter-guard yields ephemeral text,
    # so just rely on the same helper send_boxed if needed elsewhere.
    from ...core.utils import is_worldler, WORLDLER_ROLE_NAME
    if not await is_worldler(message.guild, message.author):
        await send_boxed(message.channel, "Access Required", f"{message.author.mention} you need **{WORLDLER_ROLE_NAME}**. Use `/immigrate`.", icon="🔐")
        return True

    ch = await start_word_pot(message.channel, message.author)
    if isinstance(ch, discord.TextChannel):
        await send_boxed(message.channel, "Word Pot Room Opened", f"{message.author.mention} your room is {ch.mention}.", icon="🎰")
    return True


async def maybe_route_guess(message: discord.Message, word: str) -> bool:
    """If this channel is a Word Pot channel for the author, route the guess."""
    gid = getattr(message.guild, "id", 0)
    cid = getattr(message.channel, "id", 0)
    uid = getattr(message.author, "id", 0)
    if _key(gid, cid, uid) not in casino_games:
        return False
    await guess_word_pot(message.channel, message.author, word)
    return True


# ---------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------
def register(_bot: commands.Bot, _tree: app_commands.CommandTree) -> None:
    global bot, tree
    bot, tree = _bot, _tree
    _bind_commands(_tree)
