# worldle_bot/features/solo.py
from __future__ import annotations

import asyncio
import pathlib
import random
from typing import Dict, Any, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands

# External but lightweight — used only via asyncio.to_thread
import requests

from ..core.config import (
    EMO_SHEKEL,
    WORLDLER_ROLE_NAME,
    ANSWERS,
    CARD_COLOR_SUCCESS,
    CARD_COLOR_FAIL,
)
from ..core.db import (
    get_cfg,
    ensure_worldler_role,
    get_solo_plays_today,
    inc_solo_plays_today,
    update_streak_on_play,
    change_balance,
    get_balance,
    inc_stat,
)
from ..core.utils import (
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
    uk_today_str,
)

# -----------------------------------------------------------------------------
# Module state
# -----------------------------------------------------------------------------
bot: commands.Bot | None = None
tree: app_commands.CommandTree | None = None

# Active solo games + where each user's solo room lives
# Keys align with monolith: (guild_id, channel_id, user_id)
solo_games: Dict[Tuple[int, int, int], Dict[str, Any]] = {}
solo_channels: Dict[Tuple[int, int], int] = {}  # (guild_id, user_id) -> channel_id

MAX_TRIES_SOLO = 5

# -----------------------------------------------------------------------------
# Fail quips (file-first with safe fallback)
# -----------------------------------------------------------------------------
def _load_fail_quips() -> list[str]:
    p = pathlib.Path("fail_quips.txt")
    fallback = [
        "brutal. the tiles showed no mercy.",
        "close! …to five completely different letters.",
        "rng checked out, skill took a nap.",
        "a flawless victory… for the dictionary.",
        "today’s forecast: 100% gray with scattered cope.",
    ]
    try:
        if p.exists():
            lines = [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines()]
            out = [q for q in lines if q]
            if out:
                return out
    except Exception:
        pass
    return fallback

FAIL_QUIPS = _load_fail_quips()

_definition_cache: Dict[str, str] = {}

def _fetch_definition_sync(word: str) -> str:
    w = word.lower()
    if w in _definition_cache:
        return _definition_cache[w]
    try:
        r = requests.get(f"https://api.dictionaryapi.dev/api/v2/entries/en/{w}", timeout=8)
        if r.status_code != 200:
            _definition_cache[w] = ""
            return ""
        data = r.json()
        if isinstance(data, list) and data:
            meanings = data[0].get("meanings", [])
            for m in meanings:
                defs = m.get("definitions", [])
                if defs:
                    d = defs[0].get("definition", "") or ""
                    d = (d[:220] + "…") if len(d) > 220 else d
                    _definition_cache[w] = d
                    return d
    except Exception:
        pass
    _definition_cache[w] = ""
    return ""

async def fetch_definition(word: str) -> str:
    return await asyncio.to_thread(_fetch_definition_sync, word)

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _key(gid: int, cid: int, uid: int) -> Tuple[int, int, int]:
    return (gid, cid, uid)

async def _make_private_room(invocation_channel: discord.TextChannel, member: discord.Member) -> Optional[discord.TextChannel]:
    """Create a private text channel for the player's solo session (same rules as monolith)."""
    guild = invocation_channel.guild
    me = guild.me
    if not me or not me.guild_permissions.manage_channels:
        await invocation_channel.send("I need **Manage Channels** to open your private Wordle room.", delete_after=20)
        return None

    cfg = await get_cfg(guild.id)
    rid = cfg.get("worldler_role_id") or await ensure_worldler_role(guild)
    worldler_role = guild.get_role(rid) if rid else None

    category = guild.get_channel(cfg.get("solo_category_id") or 0)
    if not isinstance(category, discord.CategoryChannel):
        category = None

    import re as _re
    base = _re.sub(r"[^a-zA-Z0-9]+", "-", member.display_name).strip("-").lower() or f"user-{member.id}"
    base = f"{base}-worldle"
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

    ch = await guild.create_text_channel(name=name, overwrites=overwrites, category=category, reason="Wordle World solo")
    return ch

async def _announce_result(guild: discord.Guild, origin_cid: Optional[int], content: str = "", embed: Optional[discord.Embed] = None):
    """Post to configured announcements channel (silent if unset)."""
    if not guild:
        return
    try:
        cfg = await get_cfg(guild.id)
        ann_id = cfg.get("announcements_channel_id")
        if not ann_id:
            return
        ch = guild.get_channel(ann_id)
        if not isinstance(ch, discord.TextChannel):
            return
        if origin_cid and embed and not embed.footer:
            src = guild.get_channel(origin_cid)
            if isinstance(src, discord.TextChannel):
                embed.set_footer(text=f"from {src.name}")
        await safe_send(ch, content=content or None, embed=embed)
    except Exception:
        pass

# -----------------------------------------------------------------------------
# Core API (start + guess)
# -----------------------------------------------------------------------------
async def start_solo(invocation_channel: discord.TextChannel, user: discord.Member) -> Optional[discord.TextChannel]:
    """Start a solo Wordle in a private room; enforce 5/day and streak touch."""
    gid, uid = invocation_channel.guild.id, user.id
    today = uk_today_str()

    plays = await get_solo_plays_today(gid, uid, today)
    if plays >= 5:
        await send_boxed(
            invocation_channel,
            "Solo Wordle",
            f"{user.mention} you've reached your **5 solo games** for today. Resets at **00:00 UK time**.",
            icon="🧩",
        )
        return None

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

    ch = await _make_private_room(invocation_channel, user)
    if not ch:
        return None

    solo_games[_key(gid, ch.id, uid)] = {
        "answer": random.choice(ANSWERS),
        "guesses": [],
        "max": MAX_TRIES_SOLO,
        "legend": {},
        "origin_cid": invocation_channel.id,
        "start_date": today,        # which UK day this slot was consumed
        "snipers_tried": set(),     # shooters who already took a shot at THIS game
    }
    solo_channels[(gid, uid)] = ch.id

    await inc_solo_plays_today(gid, uid, today)
    if plays == 0:
        await update_streak_on_play(gid, uid, today)

    left = 5 - (plays + 1)
    await send_boxed(
        ch,
        "Solo — Your Wordle is ready",
        (
            f"{user.mention} You have **{MAX_TRIES_SOLO} tries**.\n"
            "Payouts if you solve: 1st=5, 2nd=4, 3rd=3, 4th=2, 5th=1.\n"
            f"Today’s uses left **after this**: **{left}**."
        ),
        icon="🧩",
    )
    await ch.send(render_board([]))
    return ch


async def guess_solo(channel: discord.TextChannel, user: discord.Member, word: str):
    """Handle a guess for the user's active solo in this channel."""
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
    await channel.send(board)

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
            color=CARD_COLOR_SUCCESS,
        )
        await _announce_result(channel.guild, origin_cid, content="", embed=emb)

        try:
            await channel.delete(reason="Wordle World solo finished (win)")
        except Exception:
            pass
        return

    # FAIL
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
            color=CARD_COLOR_FAIL,
        )
        await _announce_result(channel.guild, origin_cid, content="", embed=emb)

        try:
            await channel.delete(reason="Wordle World solo finished (out of tries)")
        except Exception:
            pass
        return

    # MID-GAME
    next_attempt = attempt + 1
    legend = legend_overview(game["legend"], game["guesses"])
    payout = payout_for_attempt(next_attempt)
    status = f"Attempt **{attempt}/{game['max']}** — If you solve on attempt **{next_attempt}**, payout will be **{payout}**."
    flds = [("Next", status, False)]
    if legend:
        flds.append(("Legend", legend, False))
    await send_boxed(channel, "Solo — Status", "", icon="🧩", fields=flds)

# -----------------------------------------------------------------------------
# Text shortcuts + router hooks
# -----------------------------------------------------------------------------
async def maybe_start_from_text(message: discord.Message) -> bool:
    """Start solo if the user typed exactly `w`."""
    if not message.guild or not isinstance(message.channel, discord.TextChannel):
        return False
    if message.content.strip().lower() != "w":
        return False

    # Use the same guard text as monolith for non-worldlers
    from ..core.utils import is_worldler
    if not await is_worldler(message.guild, message.author):
        await send_boxed(message.channel, "Access Required", f"{message.author.mention} you need **{WORLDLER_ROLE_NAME}**. Use `/immigrate` to join.", icon="🔐")
        return True

    ch = await start_solo(message.channel, message.author)
    if isinstance(ch, discord.TextChannel):
        await send_boxed(message.channel, "Solo Room Opened", f"{message.author.mention} your room is {ch.mention}.", icon="🧩")
    return True


async def maybe_route_guess(message: discord.Message, word: str) -> bool:
    """If this channel hosts the author's solo, route the guess."""
    gid = getattr(message.guild, "id", 0)
    cid = getattr(message.channel, "id", 0)
    uid = getattr(message.author, "id", 0)
    if _key(gid, cid, uid) not in solo_games:
        return False
    await guess_solo(message.channel, message.author, word)
    return True

# -----------------------------------------------------------------------------
# Slash command binder (just /worldle start — guessing is routed centrally)
# -----------------------------------------------------------------------------
def _bind_commands(_tree: app_commands.CommandTree):
    @_tree.command(name="worldle", description="Start your own Wordle in a private room (free, 5/day).")
    async def worldle_start(inter: discord.Interaction):
        if not await guard_worldler_inter(inter):
            return
        if not inter.guild or not inter.channel:
            return
        await inter.response.defer(thinking=False)
        ch = await start_solo(inter.channel, inter.user)
        if isinstance(ch, discord.TextChannel):
            await send_boxed(inter, "Solo Room Opened", f"{inter.user.mention} your room is {ch.mention}.", icon="🧩")

# -----------------------------------------------------------------------------
# Registration
# -----------------------------------------------------------------------------
def register(_bot: commands.Bot, _tree: app_commands.CommandTree) -> None:
    global bot, tree
    bot, tree = _bot, _tree
    _bind_commands(_tree)
