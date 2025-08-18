# worldle_bot/features/dungeon.py
from __future__ import annotations

import random
import re
from math import ceil
from typing import Optional, Dict, Any, Set, Tuple

import discord
from discord import app_commands
from discord.ext import commands

from ..core.config import (
    EMO_DUNGEON,
    EMO_SHEKEL,
    EMO_STONE,
)
from ..core.utils import (
    send_boxed,
    safe_send,
    make_panel,
    guard_worldler_inter,
    is_worldler,
    score_guess,
    is_valid_guess,
    render_board,
    render_row,
    update_legend,
    legend_overview,
    payout_for_attempt,
)
from ..core.db import (
    change_balance,
    change_stones,
    get_dungeon_tickets_t1,
    change_dungeon_tickets_t1,
    get_dungeon_tickets_t2,
    change_dungeon_tickets_t2,
    get_dungeon_tickets_t3,
    change_dungeon_tickets_t3,
)
from ..features.announce import announce_result


# -------------------- module state --------------------
bot: commands.Bot | None = None
tree: app_commands.CommandTree | None = None

# per-dungeon game object by channel id
dungeon_games: Dict[int, Dict[str, Any]] = {}

# join-gate index keyed by the gate message id
pending_dungeon_gates_by_msg: Dict[int, Dict[str, Any]] = {}


# -------------------- helpers --------------------
def _dungeon_max_for_tier(tier: int) -> int:
    return 5 if tier == 3 else 4 if tier == 2 else 3  # T3=5, T2=4, T1=3


def _dungeon_mult_for_tier(tier: int) -> int:
    return 1 if tier == 3 else 2 if tier == 2 else 3  # T3 base, T2 double, T1 triple


def _dungeon_new_answer() -> str:
    # Uses core.config.ANSWERS indirectly via utils score/valid — keep local choice here:
    from ..core.config import ANSWERS
    return random.choice(ANSWERS)


def _dungeon_join_emoji_matches(emoji: discord.PartialEmoji) -> bool:
    # Try custom first; fallback to unicode swirl
    if emoji.is_unicode_emoji():
        return emoji.name == "🌀"
    # If you have a custom "dungeon" emoji you can special-case here
    return emoji.name in {"🌀", "dungeon", "ww_dungeon"}


def _lock_emoji_matches(emoji: discord.PartialEmoji) -> bool:
    return emoji.is_unicode_emoji() and emoji.name == "🔒"


def _continue_emoji_matches(emoji: discord.PartialEmoji) -> bool:
    return emoji.is_unicode_emoji() and emoji.name == "⏩"


def _cashout_emoji_matches(emoji: discord.PartialEmoji) -> bool:
    return emoji.is_unicode_emoji() and emoji.name == "💰"


async def _make_dungeon_channel(invocation_channel: discord.TextChannel, owner: discord.Member) -> Optional[discord.TextChannel]:
    """Create a private dungeon text channel visible to Worldlers (read-only)."""
    guild = invocation_channel.guild
    me = guild.me
    if not me or not me.guild_permissions.manage_channels:
        await invocation_channel.send("I need **Manage Channels** to open the dungeon.", delete_after=20)
        return None

    # category: reuse solo category if configured (consistent with Solo)
    from ..core.db import get_cfg
    cfg = await get_cfg(guild.id)
    rid = cfg.get("worldler_role_id")
    worldler_role = guild.get_role(rid) if rid else None

    category = guild.get_channel(cfg.get("solo_category_id", 0)) if cfg.get("solo_category_id") else None
    if category and not isinstance(category, discord.CategoryChannel):
        category = None

    base = re.sub(r"[^a-zA-Z0-9]+", "-", owner.display_name).strip("-").lower() or f"user-{owner.id}"
    base = f"{base}-dungeon"
    name = base
    i = 2
    while discord.utils.get(guild.text_channels, name=name):
        name = f"{base}-{i}"
        i += 1

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False, mention_everyone=False),
        owner: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, mention_everyone=False),
        me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True, mention_everyone=False),
    }
    if worldler_role:
        overwrites[worldler_role] = discord.PermissionOverwrite(view_channel=True, send_messages=False, read_message_history=True, mention_everyone=False)

    ch = await guild.create_text_channel(name=name, overwrites=overwrites, category=category, reason="Worldle Dungeon")
    return ch


# -------------------- core flow --------------------
async def _dungeon_start_round(game: Dict[str, Any]):
    # keep cumulative list across rounds
    game.setdefault("solved_rounds", [])
    game["answer"] = _dungeon_new_answer()
    game["guesses"] = []
    game["legend"] = {}
    game["max"] = _dungeon_max_for_tier(game["tier"])
    game["state"] = "active"

    ch = discord.utils.get(bot.get_all_channels(), id=game["channel_id"]) if bot else None
    if isinstance(ch, discord.TextChannel):
        await send_boxed(
            ch,
            "Dungeon — New Wordle",
            f"Tier **{game['tier']}** — you have **{game['max']} tries**.\nGuess with `g APPLE` here.",
            icon="🌀",
        )
        blank_board = render_board([], total_rows=game["max"])
        await ch.send(blank_board)  # board plain


async def _dungeon_settle_and_close(game: Dict[str, Any], payout_each: int, *, note: str):
    gid = game["guild_id"]
    ch_id = game["channel_id"]
    tier = game.get("tier", "?")
    origin_cid = game.get("origin_cid")
    part_ids: Set[int] = set(game.get("participants", set()))
    num_parts = len(part_ids)

    ch = discord.utils.get(bot.get_all_channels(), id=ch_id) if bot else None
    guild = ch.guild if isinstance(ch, discord.TextChannel) else (bot.get_guild(gid) if bot else None)

    # pay every participant
    for uid in part_ids:
        try:
            await change_balance(gid, uid, payout_each, announce_channel_id=ch_id)
        except Exception:
            pass

    # participants mentions
    names = []
    if guild:
        for uid in sorted(part_ids):
            try:
                m = guild.get_member(uid) or await guild.fetch_member(uid)
                names.append(m.mention if m else f"<@{uid}>")
            except Exception:
                names.append(f"<@{uid}>")
    names_txt = ", ".join(names) if names else f"{num_parts} adventurer(s)"

    solved_list = game.get("solved_rounds", [])
    solved_cnt = len(solved_list)
    solved_block = "—" if not solved_list else "\n".join(f"• **{w}**" for w in solved_list)

    # In-channel wrap-up
    if isinstance(ch, discord.TextChannel):
        emb = make_panel(
            title=f"Dungeon Finished — Tier {tier}",
            description=note,
            icon="🧱",
            fields=[
                ("Participants", names_txt, False),
                ("Rewards", f"**{payout_each}** {EMO_SHEKEL()} each · Pool: **{max(0, game.get('pool', 0))}**", False),
                (f"Rounds solved ({solved_cnt})", solved_block, False),
            ]
        )
        try:
            await ch.send(embed=emb)
        except Exception:
            pass

    # public announcement
    if guild:
        summary_emb = make_panel(
            title=f"Dungeon Finished — Tier {tier}",
            description=note,
            icon="🧱",
            fields=[
                ("Participants", names_txt, False),
                ("Rewards", f"**{payout_each}** {EMO_SHEKEL()} each · Pool: **{max(0, game.get('pool', 0))}**", False),
                (f"Rounds solved ({solved_cnt})", solved_block, False),
            ]
        )
        try:
            await announce_result(guild, origin_cid, "", embed=summary_emb)
        except Exception:
            pass

    # cleanup state
    dungeon_games.pop(ch_id, None)
    for mid, g in list(pending_dungeon_gates_by_msg.items()):
        if g.get("dungeon_channel_id") == ch_id:
            pending_dungeon_gates_by_msg.pop(mid, None)

    if isinstance(ch, discord.TextChannel):
        try:
            await ch.delete(reason="Dungeon closed")
        except Exception:
            pass


async def dungeon_guess(channel: discord.TextChannel, author: discord.Member, word: str):
    ch_id = channel.id
    game = dungeon_games.get(ch_id)
    if not game or game.get("state") not in ("active",):
        await send_boxed(channel, "Dungeon", "No active dungeon round right now.", icon="🌀")
        return
    if author.id not in game["participants"]:
        await send_boxed(channel, "Dungeon", f"{author.mention} you're not registered for this dungeon.", icon="🌀")
        return

    cleaned = "".join(ch for ch in word.lower().strip() if ch.isalpha())
    if len(cleaned) != 5:
        await send_boxed(channel, "Invalid Guess", "Guess must be **exactly 5 letters**.", icon="❗")
        return
    if not is_valid_guess(cleaned):
        await send_boxed(channel, "Invalid Guess", "That’s not in the Wordle dictionary (UK variants supported).", icon="📚")
        return
    if len(game["guesses"]) >= game["max"]:
        await send_boxed(channel, "Dungeon", "Out of tries for this round.", icon="🌀")
        return

    colors = score_guess(cleaned, game["answer"])
    game["guesses"].append({"word": cleaned, "colors": colors})
    update_legend(game["legend"], cleaned, colors)

    board = render_board(game["guesses"], total_rows=game["max"])
    await safe_send(channel, board)  # board plain

    attempt = len(game["guesses"])
    if cleaned == game["answer"]:
        base = payout_for_attempt(attempt)
        gained = base * _dungeon_mult_for_tier(game["tier"])
        game["pool"] = game.get("pool", 0) + gained

        # record solved word (UPPER)
        try:
            game.setdefault("solved_rounds", []).append(game["answer"].upper())
        except Exception:
            pass

        # Loot drops
        loot_msgs = []
        if random.random() < 0.40:
            await change_stones(game["guild_id"], author.id, 1)
            loot_msgs.append(f"+1 {EMO_STONE()}")
        if game["tier"] == 3 and random.random() < 0.10:
            await change_dungeon_tickets_t2(game["guild_id"], author.id, 1)
            loot_msgs.append("+1 Ticket (Tier 2)")
        elif game["tier"] == 2 and random.random() < 0.10:
            await change_dungeon_tickets_t1(game["guild_id"], author.id, 1)
            loot_msgs.append("+1 Ticket (Tier 1)")

        legend = legend_overview(game["legend"])
        extra = f" 🎁 Loot: {' · '.join(loot_msgs)}" if loot_msgs else ""
        fields = [("Pool", f"Added **+{gained} {EMO_SHEKEL()}** (now **{game['pool']}**).", True)]
        if legend:
            fields.append(("Legend", legend, False))
        await send_boxed(
            channel,
            f"✅ Solved on attempt {attempt}!",
            f"**Owner**: react **⏩** to **Continue** or **💰** to **Cash Out** for everyone.{extra}",
            icon="🌀",
            fields=fields,
        )
        msg = await safe_send(channel, "⏩ Continue or 💰 Cash Out?")
        try:
            await msg.add_reaction("⏩")
            await msg.add_reaction("💰")
        except Exception:
            pass
        game["decision_msg_id"] = msg.id
        game["state"] = "await_decision"
        return

    if attempt == game["max"]:
        half_each = ceil(max(0, game.get("pool", 0)) / 2)
        await _dungeon_settle_and_close(game, half_each, note="❌ Round failed; reward halved (rounded up).")
        return

    next_attempt = attempt + 1
    payout = payout_for_attempt(next_attempt) * _dungeon_mult_for_tier(game["tier"])
    hint = legend_overview(game["legend"])
    flds = [("Next", f"Attempt **{attempt}/{game['max']}** — Solve on attempt **{next_attempt}** to add **+{payout}** to the pool.", False)]
    if hint:
        flds.append(("Legend", hint, False))
    await send_boxed(channel, "Dungeon — Status", "", icon="🌀", fields=flds)


# -------------------- slash commands --------------------
def _bind_commands(_tree: app_commands.CommandTree):

    @_tree.command(name="worldle_dungeon", description="Open a Worldle Dungeon (Tier 1/2/3).")
    @app_commands.describe(tier="Dungeon tier")
    @app_commands.choices(tier=[
        app_commands.Choice(name="Tier 1 (triple rewards · 3 tries)", value=1),
        app_commands.Choice(name="Tier 2 (double rewards · 4 tries)", value=2),
        app_commands.Choice(name="Tier 3 (base rewards · 5 tries)",   value=3),
    ])
    async def worldle_dungeon_open(inter: discord.Interaction, tier: app_commands.Choice[int]):
        if not await guard_worldler_inter(inter):
            return
        if not inter.guild or not inter.channel:
            return
        gid, uid = inter.guild.id, inter.user.id
        t = tier.value

        # ticket check
        if t == 3:
            if await get_dungeon_tickets_t3(gid, uid) < 1:
                return await inter.response.send_message(
                    f"You need a **{EMO_DUNGEON()} Dungeon Ticket (Tier 3)**. Buy it in `/shop`.", ephemeral=True
                )
        elif t == 2:
            if await get_dungeon_tickets_t2(gid, uid) < 1:
                return await inter.response.send_message("You need a **Tier 2 Dungeon Ticket** (loot from Tier 3).", ephemeral=True)
        else:
            if await get_dungeon_tickets_t1(gid, uid) < 1:
                return await inter.response.send_message("You need a **Tier 1 Dungeon Ticket** (loot from Tier 2).", ephemeral=True)

        await inter.response.defer(thinking=False)

        # consume ticket
        if t == 3:   await change_dungeon_tickets_t3(gid, uid, -1)
        elif t == 2: await change_dungeon_tickets_t2(gid, uid, -1)
        else:        await change_dungeon_tickets_t1(gid, uid, -1)

        # Create dungeon channel
        ch = await _make_dungeon_channel(inter.channel, inter.user)  # type: ignore[arg-type]
        if not ch:
            # refund on failure to create the channel
            if t == 3:   await change_dungeon_tickets_t3(gid, uid, +1)
            elif t == 2: await change_dungeon_tickets_t2(gid, uid, +1)
            else:        await change_dungeon_tickets_t1(gid, uid, +1)
            return await inter.followup.send("Couldn't create the dungeon channel (ticket refunded).")

        # Register game
        dungeon_games[ch.id] = {
            "guild_id": gid,
            "channel_id": ch.id,
            "owner_id": uid,
            "tier": t,
            "participants": {uid},
            "state": "await_start",
            "answer": None, "guesses": [], "max": _dungeon_max_for_tier(t), "legend": {}, "pool": 0,
            "gate_msg_id": None, "welcome_msg_id": None, "decision_msg_id": None,
            "origin_cid": inter.channel.id,
            "solved_rounds": [],
        }

        # Gate message in current channel (boxed) with dynamic participants list
        part_txt = f"<@{uid}> (owner)"
        gate_embed = make_panel(
            title=f"{EMO_DUNGEON()} Dungeon Gate (Tier {t})",
            description=(
                f"Click {EMO_DUNGEON()} below **to join**. You’ll gain write access in {ch.mention}.\n"
                f"When everyone’s in, the **owner** will **lock** the dungeon from inside to start the game."
            ),
            fields=[("Participants", part_txt, False)],
            icon="🌀",
        )
        join_msg = await inter.channel.send(embed=gate_embed)  # type: ignore[arg-type]
        try:
            await join_msg.add_reaction(EMO_DUNGEON())
        except Exception:
            try:
                await join_msg.add_reaction("🌀")
            except Exception:
                pass

        pending_dungeon_gates_by_msg[join_msg.id] = {
            "guild_id": gid,
            "gate_channel_id": inter.channel.id,
            "dungeon_channel_id": ch.id,
            "owner_id": uid,
            "participants": {uid},
            "tier": t,
            "state": "gate_open",
        }

        # Spooky welcome in dungeon channel (boxed) with lock control
        welcome_txt = (
            "🕯️ **Welcome, adventurers…**\n"
            "The air is cold and the walls whisper letters you cannot see.\n"
            "Solve quickly or **lose half your spoils** to the shadows.\n\n"
            f"**Tier {t}**: rewards multiplier ×{_dungeon_mult_for_tier(t)}, tries **{_dungeon_max_for_tier(t)}** per Wordle.\n"
            "When everyone has joined, the **owner** must click **🔒** below to seal the gate and begin."
        )
        welcome = await send_boxed(
            ch,
            f"Dungeon — Tier {t}",
            f"Participants: <@{uid}> (owner)\n\n{welcome_txt}",
            icon="🌀",
        )
        try:
            welcome_msg = welcome if isinstance(welcome, discord.Message) else await ch.fetch_message(ch.last_message_id)  # type: ignore[arg-type]
        except Exception:
            welcome_msg = None

        if welcome_msg:
            try:
                await welcome_msg.add_reaction("🔒")
            except Exception:
                pass
            dungeon_games[ch.id]["welcome_msg_id"] = welcome_msg.id

        dungeon_games[ch.id]["gate_msg_id"] = join_msg.id

        await inter.followup.send(f"Opened {ch.mention} and posted a **join gate** here. Players must react {EMO_DUNGEON()} to join.")


# -------------------- reaction listeners --------------------
async def _on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.guild_id is None or (bot and payload.user_id == bot.user.id):  # type: ignore[union-attr]
        return
    guild = bot.get_guild(payload.guild_id) if bot else None
    if not guild:
        return

    # ---------- DUNGEON: join gate (boxed + edits only) ----------
    gate = pending_dungeon_gates_by_msg.get(payload.message_id)
    if gate and _dungeon_join_emoji_matches(payload.emoji):
        try:
            member = guild.get_member(payload.user_id) or await guild.fetch_member(payload.user_id)
        except Exception:
            member = None
        if member and (not member.bot) and await is_worldler(guild, member):
            # Track participant
            gate["participants"].add(member.id)

            # Grant write access to the dungeon channel
            dch = guild.get_channel(gate["dungeon_channel_id"])
            if isinstance(dch, discord.TextChannel):
                try:
                    await dch.set_permissions(member, view_channel=True, send_messages=True, read_message_history=True)
                except Exception:
                    pass
                # Mirror into the dungeon game object
                g = dungeon_games.get(dch.id)
                if g:
                    g["participants"].add(member.id)
                gmsg_id = g.get("welcome_msg_id") if g else None
                if gmsg_id:
                    # Edit the in-room welcome to show all participants (no new lines)
                    try:
                        msg = await dch.fetch_message(gmsg_id)
                        names = []
                        for uid in sorted(g["participants"]):
                            try:
                                mm = guild.get_member(uid) or await guild.fetch_member(uid)
                                names.append(mm.mention if mm else f"<@{uid}>")
                            except Exception:
                                names.append(f"<@{uid}>")
                        await msg.edit(content=(
                            f"🌀 **Dungeon — Tier {g['tier']}**\n"
                            f"Participants: {', '.join(names)}\n\n"
                            "When ready, the **owner** clicks 🔒 to start."
                        ))
                    except Exception:
                        pass

            # Edit the original *gate* message to include the live roster
            try:
                gate_ch = guild.get_channel(gate["gate_channel_id"])
                if isinstance(gate_ch, discord.TextChannel):
                    jmsg = await gate_ch.fetch_message(payload.message_id)
                    names = []
                    for uid in sorted(gate["participants"]):
                        try:
                            mm = guild.get_member(uid) or await guild.fetch_member(uid)
                            names.append(mm.mention if mm else f"<@{uid}>")
                        except Exception:
                            names.append(f"<@{uid}>")
                    emb = make_panel(
                        title=f"{EMO_DUNGEON()} Dungeon Gate — Tier {gate['tier']}",
                        description=(
                            "Click the swirl below to **join**. "
                            "When everyone’s in, the **owner** will lock the dungeon from inside to begin."
                        ),
                        fields=[("Participants", ", ".join(names) if names else "—", False)],
                        icon="🌀",
                    )
                    await jmsg.edit(content=None, embed=emb)
            except Exception:
                pass
        return

    # ---------- DUNGEON: owner locks 🔒 to start ----------
    for ch_id, game in list(dungeon_games.items()):
        if payload.message_id == game.get("welcome_msg_id") and _lock_emoji_matches(payload.emoji):
            if payload.user_id != game.get("owner_id"):
                return
            mid = game.get("gate_msg_id")
            if mid in pending_dungeon_gates_by_msg:
                pending_dungeon_gates_by_msg.pop(mid, None)
            ch = guild.get_channel(ch_id)
            if isinstance(ch, discord.TextChannel):
                await send_boxed(ch, "Dungeon", "🔒 **Gate closed.** No further joins. The dungeon begins!", icon="🌀")
                # public announcement
                try:
                    await announce_result(
                        guild,
                        game.get("origin_cid"),
                        f"{EMO_DUNGEON()} **Dungeon gate closed** — Tier {game.get('tier')} has **started** in {ch.mention}. Good luck, adventurers!"
                    )
                except Exception:
                    pass
            await _dungeon_start_round(game)
            return

    # ---------- DUNGEON: owner decision (⏩ continue / 💰 cash out) ----------
    for ch_id, game in list(dungeon_games.items()):
        if payload.message_id == game.get("decision_msg_id") and game.get("state") == "await_decision":
            if payload.user_id != game.get("owner_id"):
                return
            ch = guild.get_channel(ch_id)
            if _continue_emoji_matches(payload.emoji):
                game["decision_msg_id"] = None
                if isinstance(ch, discord.TextChannel):
                    await send_boxed(ch, "Dungeon", "⏩ **Continuing…**", icon="🌀")
                await _dungeon_start_round(game)
                return
            if _cashout_emoji_matches(payload.emoji):
                pool = max(0, game.get("pool", 0))
                await _dungeon_settle_and_close(game, pool, note="💰 **Cashed out in time.**")
                return


# -------------------- external entry (guess router) --------------------
async def maybe_route_guess(message: discord.Message, word: str) -> bool:
    """Called by text shortcut router: if this channel is a dungeon, handle guess."""
    if not message.guild:
        return False
    game = dungeon_games.get(message.channel.id)
    if not game:
        return False
    await dungeon_guess(message.channel, message.author, word)  # type: ignore[arg-type]
    return True


# -------------------- registration --------------------
def register(_bot: commands.Bot, _tree: app_commands.CommandTree) -> None:
    global bot, tree
    bot, tree = _bot, _tree
    _bind_commands(_tree)
    # hook reaction listener
    bot.add_listener(_on_raw_reaction_add, "on_raw_reaction_add")
