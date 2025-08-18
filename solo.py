# worldle_bot/features/solo.py
"""
Solo Wordle (private rooms) + Word Pot (casino).

Slash commands:
 - /worldle                 → start a solo Wordle (private room)
 - /worldle_guess WORD      → guess in your current room (smart routes)
 - /worldle_end             → end your current solo/casino game (fail)
 - /worldle_casino          → start Word Pot (3 tries, shared pot)

This module maintains in-memory maps for active games and their rooms, posts
announcements via features.announce, and updates economy/streak stats via db.
"""

from __future__ import annotations

import random
import re
from typing import Optional, Dict, Any, Tuple

import discord
from discord import app_commands
from discord.ext import commands

from ..core.config import (
    ANSWERS,
    WORLDLER_ROLE_NAME,
    EMO_SHEKEL,
    EMO_STONE,
    EMO_SNIPER,
    EMO_BOUNTY,
    EMO_CHICKEN,
    EMO_BADGE,
    EMO_DUNGEON,
    FAIL_QUIPS,
    CASINO_BASE_POT,  # base pot after a win
)
from ..core.utils import (
    send_boxed,
    safe_send,
    make_card,
    make_panel,
    guard_worldler_inter,
    is_worldler,
    render_board,
    render_row,
    score_guess,
    is_valid_guess,
    update_legend,
    legend_overview,
    payout_for_attempt,
    uk_today_str,
    gmt_now_s,
    fetch_definition,
)
from ..core.db import (
    get_cfg,
    set_cfg,
    change_balance,
    get_balance,
    get_casino_pot,
    set_casino_pot,
    get_solo_wordles_left,
    inc_solo_plays_today,
    update_streak_on_play,
    inc_stat,
    dec_solo_plays_on_date,
)
from ..features.announce import announce_result


# -------------------- module state --------------------
bot: commands.Bot | None = None
tree: app_commands.CommandTree | None = None

# (guild_id, user_id) -> channel_id for solo and casino
solo_channels: Dict[Tuple[int, int], int] = {}
casino_channels: Dict[Tuple[int, int], int] = {}

# (guild_id, channel_id, user_id) -> game dict
solo_games: Dict[Tuple[int, int, int], Dict[str, Any]] = {}
casino_games: Dict[Tuple[int, int, int], Dict[str, Any]] = {}

# Helpers
def _key(gid: int, cid: int, uid: int) -> Tuple[int, int, int]:
    return (gid, cid, uid)


# -------------------- channel factory --------------------
async def _make_private_room(invocation_channel: discord.TextChannel, member: discord.Member, *, suffix: str) -> Optional[discord.TextChannel]:
    """Create a locked text channel for a single player (view for Worldlers)."""
    guild = invocation_channel.guild
    me = guild.me
    if not me or not me.guild_permissions.manage_channels:
        await invocation_channel.send("I need **Manage Channels** to open your private room.", delete_after=20)
        return None

    cfg = await get_cfg(guild.id)
    rid = cfg.get("worldler_role_id")
    worldler_role = guild.get_role(rid) if rid else None

    category = guild.get_channel(cfg.get("solo_category_id", 0)) if cfg.get("solo_category_id") else None
    if category and not isinstance(category, discord.CategoryChannel):
        category = None

    base_name = re.sub(r"[^a-zA-Z0-9]+", "-", member.display_name).strip("-").lower() or f"user-{member.id}"
    base = f"{base_name}-{suffix}"
    name = base
    i = 2
    while discord.utils.get(guild.text_channels, name=name):
        name = f"{base}-{i}"
        i += 1

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False, mention_everyone=False),
        member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, mention_everyone=False),
        me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True, mention_everyone=False),
    }
    if worldler_role:
        overwrites[worldler_role] = discord.PermissionOverwrite(view_channel=True, send_messages=False, read_message_history=True, mention_everyone=False)

    ch = await guild.create_text_channel(name=name, overwrites=overwrites, category=category, reason=f"Worldle {suffix}")
    return ch


# -------------------- SOLO --------------------
async def solo_start(invocation_channel: discord.TextChannel, user: discord.Member) -> Optional[discord.TextChannel]:
    gid, uid = invocation_channel.guild.id, user.id
    today = uk_today_str()  # UK-local reset

    left = await get_solo_wordles_left(gid, uid)  # DB counts + cap inside
    if left <= 0:
        await send_boxed(
            invocation_channel,
            "Solo Wordle",
            f"{user.mention} you've reached your **5 solo games** for today. Resets at **00:00 UK time**.",
            icon="🧩",
        )
        return None

    # if user already has a running solo, surface it
    existing_cid = solo_channels.get((gid, uid))
    if existing_cid and _key(gid, existing_cid, uid) in solo_games:
        ch = invocation_channel.guild.get_channel(existing_cid)
        if isinstance(ch, discord.TextChannel):
            await send_boxed(
                invocation_channel,
                "Solo Wordle",
                f"{user.mention} you already have a game running: {ch.mention}",
                icon="🧩",
            )
            return ch
        else:
            solo_channels.pop((gid, uid), None)

    ch = await _make_private_room(invocation_channel, user, suffix="worldle")
    if not ch:
        return None

    solo_games[_key(gid, ch.id, uid)] = {
        "answer": random.choice(ANSWERS),
        "guesses": [],
        "max": 5,
        "legend": {},
        "origin_cid": invocation_channel.id,
        "start_date": today,       # record the UK day slot
        "snipers_tried": set(),    # shooters who took a shot at THIS game
    }
    solo_channels[(gid, uid)] = ch.id

    # consume a daily slot & touch streak (once per UK day)
    await inc_solo_plays_today(gid, uid, today)
    await update_streak_on_play(gid, uid, today)

    await send_boxed(
        ch,
        "Solo — Your Wordle is ready",
        (
            f"{user.mention} You have **5 tries**.\n"
            "Payouts if you solve: 1st=5, 2nd=4, 3rd=3, 4th=2, 5th=1.\n"
            f"Today’s uses left **after this**: **{left-1}**."
        ),
        icon="🧩",
    )
    board = render_board(solo_games[_key(gid, ch.id, uid)]["guesses"])
    await ch.send(board)  # plain board
    return ch


async def solo_guess(channel: discord.TextChannel, user: discord.Member, word: str):
    gid, cid, uid = channel.guild.id, channel.id, user.id
    game = solo_games.get(_key(gid, cid, uid))
    if not game:
        await send_boxed(channel, "Solo Wordle", f"{user.mention} no game here. Start with `w` or `/worldle`.", icon="🧩")
        return

    cleaned = "".join(ch for ch in word.lower().strip() if ch.isalpha())
    if len(cleaned) != 5:
        await send_boxed(channel, "Invalid Guess", "Guess must be **exactly 5 letters**.", icon="❗")
        return
    if not is_valid_guess(cleaned):
        await send_boxed(channel, "Invalid Guess", "That’s not in the Wordle dictionary (UK variants supported).", icon="📚")
        return
    if len(game["guesses"]) >= game["max"]:
        await send_boxed(channel, "Solo Wordle", "Out of tries! Start a new one with `w`.", icon="🧩")
        return

    colors = score_guess(cleaned, game["answer"])
    game["guesses"].append({"word": cleaned, "colors": colors})
    update_legend(game["legend"], cleaned, colors)

    board = render_board(game["guesses"])
    await channel.send(board)  # plain

    attempt = len(game["guesses"])

    def _cleanup():
        solo_games.pop(_key(gid, cid, uid), None)
        if solo_channels.get((gid, uid)) == cid:
            solo_channels.pop((gid, uid), None)

    # WIN
    if cleaned == game["answer"]:
        payout = payout_for_attempt(attempt)
        if payout:
            await change_balance(gid, uid, payout, announce_channel_id=cid)
        bal_new = await get_balance(gid, uid)
        origin_cid = game.get("origin_cid")
        ans = game["answer"].upper()
        _cleanup()

        await send_boxed(
            channel,
            "🏁 Solo — Solved!",
            f"{user.mention} solved **{ans}** on attempt **{attempt}** and earned **{payout} {EMO_SHEKEL()}**.\nBalance **{bal_new}**",
            icon="🎉",
        )

        emb = make_card(
            title="🏁 Solo — Finished",
            description=f"{user.mention} solved **{ans}** in **{attempt}** tries and earned **{payout} {EMO_SHEKEL()}**.",
            fields=[("Board", board, False)],
        )
        await announce_result(channel.guild, origin_cid, content="", embed=emb)

        try:
            await channel.delete(reason="Worldle World solo finished (win)")
        except Exception:
            pass
        return

    # FAIL (out of tries)
    if attempt == game["max"]:
        ans_raw = game["answer"]
        ans = ans_raw.upper()
        origin_cid = game.get("origin_cid")
        quip = random.choice(FAIL_QUIPS)
        definition = await fetch_definition(ans_raw)
        _cleanup()
        await inc_stat(gid, uid, "solo_fails", 1)
        bal_now = await get_balance(gid, uid)

        desc = f"❌ Out of tries. The word was **{ans}** — {quip}\nBalance **{bal_now}**."
        fields = [("Board", board, False)]
        if definition:
            fields.append(("Definition", definition, False))
        await send_boxed(channel, "💀 Solo — Failed", desc, icon="💀", fields=fields)

        emb = make_card(
            title="💀 Solo — Failed",
            description=f"{user.mention} failed their Worldle. The word was **{ans}** — {quip}",
            fields=fields,
        )
        await announce_result(channel.guild, origin_cid, content="", embed=emb)

        try:
            await channel.delete(reason="Worldle World solo finished (out of tries)")
        except Exception:
            pass
        return

    # MID-GAME STATUS (box the legend)
    next_attempt = attempt + 1
    legend = legend_overview(game["legend"], game["guesses"])
    payout = payout_for_attempt(next_attempt)
    status = f"Attempt **{attempt}/{game['max']}** — If you solve on attempt **{next_attempt}**, payout will be **{payout}**."
    flds = [("Next", status, False)]
    if legend:
        flds.append(("Legend", legend, False))
    await send_boxed(channel, "Solo — Status", "", icon="🧩", fields=flds)


# -------------------- CASINO: Word Pot --------------------
async def casino_start_word_pot(invocation_channel: discord.TextChannel, user: discord.Member) -> Optional[discord.TextChannel]:
    gid, uid = invocation_channel.guild.id, user.id

    bal = await get_balance(gid, uid)
    if bal < 1:
        await send_boxed(invocation_channel, "Word Pot", f"{user.mention} you need **1 {EMO_SHEKEL()}** to play.", icon="🎰")
        return None

    existing_cid = casino_channels.get((gid, uid))
    if existing_cid and _key(gid, existing_cid, uid) in casino_games:
        ch = invocation_channel.guild.get_channel(existing_cid)
        if isinstance(ch, discord.TextChannel):
            await send_boxed(invocation_channel, "Word Pot", f"{user.mention} you already have a game running: {ch.mention}", icon="🎰")
            return ch
        else:
            casino_channels.pop((gid, uid), None)

    ch = await _make_private_room(invocation_channel, user, suffix="wordpot")
    if not ch:
        return None

    # charge entry
    await change_balance(gid, uid, -1, announce_channel_id=ch.id)

    casino_games[_key(gid, ch.id, uid)] = {
        "answer": random.choice(ANSWERS),
        "guesses": [],
        "max": 3,
        "legend": {},
        "origin_cid": invocation_channel.id,
        "staked": 1,
    }
    casino_channels[(gid, uid)] = ch.id

    pot = await get_casino_pot(gid)
    await send_boxed(
        ch,
        "🎰 Word Pot",
        (
            f"{user.mention} • Entry: **1 {EMO_SHEKEL()}** (paid)\n"
            f"• Current Pot: **{pot} {EMO_SHEKEL()}** (resets to {CASINO_BASE_POT} on win)\n"
            "• You have **3 tries** — solve within 3 to **win the pot**.\n"
            "If you fail, your entry adds **+1** to the pot."
        ),
        icon="🎰",
    )
    board = render_board(casino_games[_key(gid, ch.id, uid)]["guesses"], total_rows=3)
    await ch.send(board)  # plain
    return ch


async def casino_guess(channel: discord.TextChannel, user: discord.Member, word: str):
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

    board = render_board(game["guesses"], total_rows=3)
    await safe_send(channel, board)  # plain

    def _cleanup():
        casino_games.pop(_key(gid, cid, uid), None)
        if casino_channels.get((gid, uid)) == cid:
            casino_channels.pop((gid, uid), None)

    # WIN
    if cleaned == game["answer"]:
        pot = await get_casino_pot(gid)
        await change_balance(gid, uid, pot, announce_channel_id=cid)
        bal_new = await get_balance(gid, uid)
        ans = game["answer"].upper()
        origin_cid = game.get("origin_cid")
        _cleanup()
        await set_casino_pot(gid, CASINO_BASE_POT)

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
        )
        await announce_result(channel.guild, origin_cid, content="", embed=emb)

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
        ans_raw = game["answer"]
        ans = ans_raw.upper()
        quip = random.choice(FAIL_QUIPS)
        definition = await fetch_definition(ans_raw)
        origin_cid = game.get("origin_cid")
        _cleanup()

        fields = [("Board", board, False), ("Pot", f"Now **{new_pot} {EMO_SHEKEL()}**", True)]
        if definition:
            fields.append(("Definition", definition, False))

        await send_boxed(
            channel,
            "🎰 Word Pot — Failed",
            f"❌ The word was **{ans}** — {quip}",
            icon="🎰",
            fields=fields,
        )

        emb = make_card(
            title="🎰 Word Pot — Failed",
            description=f"{user.mention} failed **Word Pot** — the word was **{ans}**. {quip}",
            fields=fields,
        )
        await announce_result(channel.guild, origin_cid, content="", embed=emb)

        try:
            await channel.delete(reason="Word Pot finished (fail)")
        except Exception:
            pass
        return

    # MID-GAME STATUS
    legend = legend_overview(game["legend"], game["guesses"])
    msg = f"Attempt **{attempt}/3** — solve within **3** to win the pot."
    flds = [("Next", msg, False)]
    if legend:
        flds.append(("Legend", legend, False))
    await send_boxed(channel, "Word Pot — Status", "", icon="🎰", fields=flds)


# -------------------- Slash wrappers --------------------
def _bind_commands(_tree: app_commands.CommandTree):

    @_tree.command(name="worldle", description="Start your own Wordle in a private room (free, 5/day).")
    async def worldle_start_cmd(inter: discord.Interaction):
        if not await guard_worldler_inter(inter):
            return
        if not inter.guild or not inter.channel:
            return
        await inter.response.defer(thinking=False)
        ch = await solo_start(inter.channel, inter.user)  # type: ignore[arg-type]
        if isinstance(ch, discord.TextChannel):
            await send_boxed(inter, "Solo Room Opened", f"{inter.user.mention} your room is {ch.mention}.", icon="🧩")

    @_tree.command(name="worldle_casino", description="Play a casino Wordle. First game: Word Pot.")
    @app_commands.describe(game="Pick a casino game")
    @app_commands.choices(game=[app_commands.Choice(name="Word Pot", value="word_pot")])
    async def worldle_casino_cmd(inter: discord.Interaction, game: Optional[app_commands.Choice[str]] = None):
        if not await guard_worldler_inter(inter):
            return
        if not inter.guild or not inter.channel:
            return
        await inter.response.defer(thinking=False)
        choice = (game.value if game else "word_pot")
        if choice != "word_pot":
            return await send_boxed(inter, "Casino", "Only **Word Pot** is available right now.", icon="🎰")
        ch = await casino_start_word_pot(inter.channel, inter.user)  # type: ignore[arg-type]
        if isinstance(ch, discord.TextChannel):
            await send_boxed(inter, "Word Pot Room Opened", f"{inter.user.mention} your room is {ch.mention}.", icon="🎰")

    @_tree.command(name="worldle_guess", description="Guess your word in this channel.")
    @app_commands.describe(word="5-letter guess")
    async def worldle_guess_cmd(inter: discord.Interaction, word: str):
        if not await guard_worldler_inter(inter):
            return
        if not inter.guild or not inter.channel:
            return
        await inter.response.defer(thinking=False)
        gid, cid, uid = inter.guild.id, inter.channel.id, inter.user.id

        if _key(gid, cid, uid) in solo_games:
            await solo_guess(inter.channel, inter.user, word)  # type: ignore[arg-type]
        elif _key(gid, cid, uid) in casino_games:
            await casino_guess(inter.channel, inter.user, word)  # type: ignore[arg-type]
        else:
            # keeps the nice "no game here" message for solo if applicable
            await solo_guess(inter.channel, inter.user, word)  # type: ignore[arg-type]

    @_tree.command(name="worldle_end", description="End your current Wordle here (counts as a fail).")
    async def worldle_end_cmd(inter: discord.Interaction):
        if not await guard_worldler_inter(inter):
            return
        if not inter.guild or not inter.channel:
            return

        gid, cid, uid = inter.guild.id, inter.channel.id, inter.user.id

        # --- Word Pot first ---
        cgame = casino_games.get(_key(gid, cid, uid))
        if cgame:
            board = render_board(cgame["guesses"], total_rows=3)
            ans_raw = cgame["answer"]
            ans = ans_raw.upper()
            origin_cid = cgame.get("origin_cid")

            cur_pot = await get_casino_pot(gid)
            new_pot = cur_pot + (cgame.get("staked", 0) or 0)
            await set_casino_pot(gid, new_pot)

            casino_games.pop(_key(gid, cid, uid), None)
            if casino_channels.get((gid, uid)) == cid:
                casino_channels.pop((gid, uid), None)

            quip = random.choice(FAIL_QUIPS)
            definition = await fetch_definition(ans_raw)

            await inter.response.send_message(board)
            extra_def = f"\n📖 Definition: {definition}" if definition else ""
            await inter.followup.send(
                f"🛑 Ended your **Word Pot** game. The word was **{ans}** — {quip}{extra_def}\n"
                f"Pot is now **{new_pot} {EMO_SHEKEL()}**."
            )

            fields = [("Board", board, False), ("Pot", f"Now **{new_pot} {EMO_SHEKEL()}**", True)]
            if definition:
                fields.append(("Definition", definition, False))

            emb = make_card(
                title="🎰 Word Pot — Ended Early",
                description=f"{inter.user.mention} ended their Word Pot early. The word was **{ans}** — {quip}",
                fields=fields,
            )
            await announce_result(inter.guild, origin_cid, content="", embed=emb)

            try:
                await inter.channel.delete(reason="Word Pot ended by user (fail)")
            except Exception:
                pass
            return

        # --- Solo fallback ---
        sgame = solo_games.get(_key(gid, cid, uid))
        if not sgame:
            return await inter.response.send_message("You don't have a game running here.")

        board = render_board(sgame["guesses"])
        ans_raw = sgame["answer"]
        ans = ans_raw.upper()
        origin_cid = sgame.get("origin_cid")

        solo_games.pop(_key(gid, cid, uid), None)
        if solo_channels.get((gid, uid)) == cid:
            solo_channels.pop((gid, uid), None)

        quip = random.choice(FAIL_QUIPS)
        definition = await fetch_definition(ans_raw)

        await inter.response.send_message(board)
        extra_def = f"\n📖 Definition: {definition}" if definition else ""
        await inter.followup.send(f"🛑 Ended your game. The word was **{ans}** — {quip}{extra_def}")

        fields = [("Board", board, False)]
        if definition:
            fields.append(("Definition", definition, False))

        emb = make_card(
            title="💀 Solo — Ended Early",
            description=f"{inter.user.mention} failed their Worldle (ended early). The word was **{ans}** — {quip}",
            fields=fields,
        )
        await announce_result(inter.guild, origin_cid, content="", embed=emb)

        try:
            await inter.channel.delete(reason="Worldle World solo ended by user (fail)")
        except Exception:
            pass


# -------------------- registration --------------------
def register(_bot: commands.Bot, _tree: app_commands.CommandTree) -> None:
    """Expose slash commands and keep module state references."""
    global bot, tree
    bot, tree = _bot, _tree
    _bind_commands(_tree)
