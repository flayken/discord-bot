# worldle_bot/features/bounty.py
"""
Hourly Bounty (GMT):
 - /worldle_bounty_setchannel   (admin) bind the bounty channel
 - /worldle_bounty_now          (admin) post a prompt immediately
 - /worldle_bounty_guess WORD   guess in the bounty channel when armed

Behavior:
 - At the top of each GMT hour (first ~40s), post a "gate" prompt to the configured bounty channel.
 - Players react with the bounty emoji to "arm" the bounty. Needs 2 players.
 - After the 2nd player, the bounty arms in BOUNTY_ARM_DELAY_S (default 60s).
 - Once armed, infinite guesses allowed in the bounty channel, with a per-user cooldown.
 - Success pays BOUNTY_PAYOUT to the solver and announces.
 - If a pending prompt expires, or an armed bounty expires, +1 shekel to the Word Pot.

Exports:
 - register(bot, tree): wire commands, listeners, and start the loop.
"""

from __future__ import annotations

import random
from typing import Optional, Dict, Any, Set

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..core.config import (
    EMO_SHEKEL,
    EMO_BOUNTY,
    EMO_BOUNTY_NAME,
    ANSWERS,
)
from ..core.utils import (
    make_panel,
    make_card,
    safe_send,
    send_boxed,
    is_worldler,
    guard_worldler_inter,
    gmt_now_s,
    current_hour_index_gmt,
)
from ..core.db import (
    get_cfg,
    set_cfg,
    get_casino_pot,
    set_casino_pot,
    change_balance,
    inc_stat,
    get_balance,
)

from ..features.announce import announce_result  # thin wrapper for announcements channel


# -------------------- module state --------------------
bot: commands.Bot | None = None
tree: app_commands.CommandTree | None = None

# config knobs
BOUNTY_PAYOUT = 5
BOUNTY_EXPIRE_MIN = 59
BOUNTY_EXPIRE_S = BOUNTY_EXPIRE_MIN * 60
BOUNTY_ARM_DELAY_S = 60
BOUNTY_GUESS_COOLDOWN_S = 5  # per-user

# in-memory game state
# guild_id -> pending gate info
pending_bounties: Dict[int, Dict[str, Any]] = {}
# guild_id -> active armed game
bounty_games: Dict[int, Dict[str, Any]] = {}
# (guild_id, user_id) -> last guess timestamp (per-user throttle)
last_bounty_guess_ts: Dict[tuple[int, int], int] = {}


# -------------------- helpers --------------------
def _bounty_emoji_matches(emoji: discord.PartialEmoji) -> bool:
    target_name = (EMO_BOUNTY_NAME or "ww_bounty").lower()
    if emoji.is_unicode_emoji():
        return emoji.name == "🎯"
    return (emoji.name or "").lower() == target_name


async def _find_bounty_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    cfg = await get_cfg(guild.id)
    ch_id = cfg.get("bounty_channel_id")
    if ch_id:
        ch = guild.get_channel(ch_id)
        if isinstance(ch, discord.TextChannel) and ch.permissions_for(guild.me).send_messages:
            return ch
    if guild.system_channel and guild.system_channel.permissions_for(guild.me).send_messages:
        return guild.system_channel
    for ch in guild.text_channels:
        if ch.permissions_for(guild.me).send_messages:
            return ch
    return None


async def _post_bounty_prompt(guild: discord.Guild, channel: discord.TextChannel, hour_idx: int) -> bool:
    """Create a gate prompt if none pending/active for this guild. Returns True if posted."""
    if guild.id in pending_bounties or guild.id in bounty_games:
        return False

    cfg = await get_cfg(guild.id)
    suppress_ping = int(cfg.get("suppress_bounty_ping", 0)) == 1

    # optional role ping — rely on roles.ensure_bounty_role to have created one
    rid = cfg.get("bounty_role_id")  # roles.ensure_bounty_role should store this; else None
    role_mention = "" if suppress_ping else (f"<@&{rid}>" if rid else "")

    em = EMO_BOUNTY()
    desc = (
        f"React with {em} to **arm** this bounty — need **2** players.\n"
        f"**After 2 react, the bounty arms in {BOUNTY_ARM_DELAY_S//60} minute.**\n"
        f"**Prize:** {BOUNTY_PAYOUT} {EMO_SHEKEL()}\n"
        "Use `bg APPLE` or `/worldle_bounty_guess` when armed.\n\n"
        f"⏲️ *This prompt expires in {BOUNTY_EXPIRE_MIN} minutes.*"
    )
    emb = make_panel(title=f"{em} Hourly Bounty (GMT)", description=desc)

    # send (use message content for the ping so it actually notifies)
    msg = await safe_send(
        channel,
        content=role_mention or None,
        embed=emb,
        allowed_mentions=discord.AllowedMentions(users=False, roles=(not suppress_ping), everyone=False),
    )
    try:
        await msg.add_reaction(em)
    except Exception:
        try:
            await msg.add_reaction("🎯")
        except Exception:
            pass

    pending_bounties[guild.id] = {
        "message_id": msg.id,
        "channel_id": channel.id,
        "users": set(),   # type: Set[int]
        "hour_idx": hour_idx,
        "expires_at": gmt_now_s() + BOUNTY_EXPIRE_S,
        "arming_at": None,
    }
    await set_cfg(guild.id, last_bounty_hour=hour_idx)
    return True


async def _start_bounty_after_gate(guild: discord.Guild, channel_id: int):
    if guild.id in bounty_games:
        return
    answer = random.choice(ANSWERS)
    bounty_games[guild.id] = {
        "answer": answer,
        "channel_id": channel_id,
        "started_at": gmt_now_s(),
        "expires_at": gmt_now_s() + BOUNTY_EXPIRE_S,
    }
    # re-enable pings for next hour
    await set_cfg(guild.id, last_bounty_ts=gmt_now_s(), suppress_bounty_ping=0)
    ch = guild.get_channel(channel_id)
    if isinstance(ch, discord.TextChannel):
        emb = make_panel(
            title="🎯 Bounty armed!",
            description=(
                f"First to solve in **{BOUNTY_EXPIRE_MIN} minutes** wins **{BOUNTY_PAYOUT} {EMO_SHEKEL()}**.\n"
                "Use `bg WORD` or `/worldle_bounty_guess`."
            ),
        )
        await safe_send(ch, embed=emb)


# -------------------- commands --------------------
def _bind_commands(_tree: app_commands.CommandTree):

    @_tree.command(name="worldle_bounty_setchannel", description="(Admin) Set this channel for bounty drops.")
    @app_commands.default_permissions(administrator=True)
    async def worldle_bounty_setchannel(inter: discord.Interaction):
        if not inter.guild or not inter.channel:
            return await send_boxed(inter, "Bounty", "Server only.", icon="🎯", ephemeral=True)
        await set_cfg(inter.guild.id, bounty_channel_id=inter.channel.id)
        await send_boxed(inter, "Bounty", f"Set bounty channel to {inter.channel.mention}.", icon="🎯")

    @_tree.command(name="worldle_bounty_now", description="(Admin) Post a bounty prompt now (requires emoji arm).")
    @app_commands.default_permissions(administrator=True)
    async def worldle_bounty_now(inter: discord.Interaction):
        if not inter.guild:
            return await send_boxed(inter, "Bounty", "Server only.", icon="🎯", ephemeral=True)
        await inter.response.defer(thinking=False)

        if inter.guild.id in bounty_games:
            return await safe_send(inter.channel, "There is already an active bounty.")
        if inter.guild.id in pending_bounties:
            return await safe_send(inter.channel, "There is already a pending bounty prompt.")

        ch = await _find_bounty_channel(inter.guild)
        if not ch:
            return await safe_send(inter.channel, "I can't find a channel I can speak in.")

        ok = await _post_bounty_prompt(inter.guild, ch, current_hour_index_gmt())
        await safe_send(inter.channel, "🎯 Bounty prompt posted — needs 2 reactions to arm." if ok else "Couldn't post a bounty prompt.")

    @_tree.command(name="worldle_bounty_guess", description="Guess the active bounty word.")
    @app_commands.describe(word="Your 5-letter guess")
    async def worldle_bounty_guess(inter: discord.Interaction, word: str):
        if not await guard_worldler_inter(inter):
            return
        if not inter.guild or not inter.channel:
            return
        game = bounty_games.get(inter.guild.id)
        if not game:
            return await send_boxed(inter, "Bounty", "No active bounty right now.", icon="🎯", ephemeral=True)
        if inter.channel.id != game["channel_id"]:
            ch = inter.guild.get_channel(game["channel_id"])
            where = ch.mention if isinstance(ch, discord.TextChannel) else "the bounty channel"
            return await send_boxed(inter, "Bounty", f"Use this in {where}.", icon="🎯", ephemeral=True)

        # per-user cooldown
        now_s = gmt_now_s()
        key = (inter.guild.id, inter.user.id)
        last = last_bounty_guess_ts.get(key, 0)
        delta = now_s - last
        if delta < BOUNTY_GUESS_COOLDOWN_S:
            wait = int(BOUNTY_GUESS_COOLDOWN_S - delta)
            return await send_boxed(inter, "Slow down", f"**{wait}s** cooldown between guesses.", icon="⏳", ephemeral=True)

        cleaned = "".join(ch for ch in word.lower().strip() if ch.isalpha())
        if len(cleaned) != 5:
            return await send_boxed(inter, "Invalid Guess", "Guess must be exactly **5 letters**.", icon="❗", ephemeral=True)
        from ..core.utils import is_valid_guess, render_row, score_guess  # local import to avoid cycles
        if not is_valid_guess(cleaned):
            return await send_boxed(inter, "Invalid Guess", "That’s not in the Wordle dictionary (UK variants supported).", icon="📚", ephemeral=True)

        # start cooldown
        last_bounty_guess_ts[key] = now_s

        colors = score_guess(cleaned, game["answer"])
        row = render_row(cleaned, colors)

        # live feedback
        await inter.response.send_message(row)

        if cleaned == game["answer"]:
            gid, uid = inter.guild.id, inter.user.id
            await change_balance(gid, uid, BOUNTY_PAYOUT, announce_channel_id=game["channel_id"])
            await inc_stat(gid, uid, "bounties_won", 1)
            bal = await get_balance(gid, uid)

            # capture & clear
            ans_raw = game["answer"]
            ans_up = ans_raw.upper()
            del bounty_games[gid]

            # small confirmation in-channel
            await safe_send(
                inter.channel,
                f"🏆 {inter.user.mention} solved the Bounty Wordle (**{ans_up}**) and wins **{BOUNTY_PAYOUT} {EMO_SHEKEL()}**! (Balance: {bal})",
                allowed_mentions=discord.AllowedMentions(users=[inter.user])
            )

            # definition + neat card in announcements
            from ..core.utils import fetch_definition  # same helper you used
            definition = await fetch_definition(ans_raw)
            fields = []
            if definition:
                fields.append(("Definition", definition, False))
            fields.append(("Result", row, False))  # emojis render

            emb = make_card(
                title="🎯 Hourly Bounty — Solved",
                description=f"{inter.user.mention} wins **{BOUNTY_PAYOUT} {EMO_SHEKEL()}** by solving **{ans_up}**.",
                fields=fields,
            )
            await announce_result(inter.guild, origin_cid=None, content="", embed=emb)
        else:
            await inter.followup.send("(Keep trying! Unlimited guesses.)")


# -------------------- listeners --------------------
async def _on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    gid = payload.guild_id
    if gid is None or (bot and bot.user and payload.user_id == bot.user.id):
        return

    pend = pending_bounties.get(gid)
    if not pend or payload.message_id != pend.get("message_id") or not _bounty_emoji_matches(payload.emoji):
        return

    guild = discord.utils.get(bot.guilds, id=gid) if bot else None
    if not guild:
        return

    # Validate player
    try:
        member = guild.get_member(payload.user_id) or await guild.fetch_member(payload.user_id)
    except Exception:
        member = None
    if not member or member.bot or not await is_worldler(guild, member):
        return

    # track
    users: Set[int] = pend["users"]
    users.add(member.id)

    # show roster on the card
    try:
        ch = guild.get_channel(pend["channel_id"])
        if isinstance(ch, discord.TextChannel):
            msg = await ch.fetch_message(pend["message_id"])
            names = []
            for uid in sorted(users):
                try:
                    m = guild.get_member(uid) or await guild.fetch_member(uid)
                    names.append(m.mention if m else f"<@{uid}>")
                except Exception:
                    names.append(f"<@{uid}>")
            players_txt = ", ".join(names) if names else "—"
            desc = (
                f"React with {EMO_BOUNTY()} to **arm** this bounty — need **2** players.\n"
                f"After 2 react, the bounty **arms in {BOUNTY_ARM_DELAY_S//60} minute**.\n"
                f"**Prize:** {BOUNTY_PAYOUT} {EMO_SHEKEL()}\n"
                "Use `bg APPLE` or `/worldle_bounty_guess` when armed.\n\n"
                f"⏲️ This prompt expires in {BOUNTY_EXPIRE_MIN} minutes."
            )
            emb = make_panel(
                title=f"{EMO_BOUNTY()} Hourly Bounty (GMT)",
                description=desc,
                fields=[("Players ready", players_txt, False)],
            )
            await msg.edit(embed=emb)
    except Exception:
        pass

    # start arming countdown if we just reached 2
    if len(users) >= 2 and not pend.get("arming_at"):
        pend["arming_at"] = gmt_now_s() + BOUNTY_ARM_DELAY_S
        try:
            ch = guild.get_channel(pend["channel_id"])
            await send_boxed(ch, "Bounty", f"✅ Armed by **{len(users)}** players. **Arming in {BOUNTY_ARM_DELAY_S//60} minute…**", icon="🎯")
        except Exception:
            pass

    # tidy up: remove the user's reaction so others can easily click too
    try:
        ch = guild.get_channel(pend["channel_id"])
        if isinstance(ch, discord.TextChannel):
            msg = await ch.fetch_message(pend["message_id"])
            await msg.remove_reaction(payload.emoji, member)  # type: ignore[name-defined]
    except Exception:
        pass


async def _on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    gid = payload.guild_id
    if gid is None:
        return

    pend = pending_bounties.get(gid)
    if not pend or payload.message_id != pend.get("message_id") or not _bounty_emoji_matches(payload.emoji):
        return

    # Remove from set
    uid = payload.user_id
    if uid in pend.get("users", set()):
        pend["users"].discard(uid)

    guild = discord.utils.get(bot.guilds, id=gid) if bot else None
    if not guild:
        return

    # If countdown was running but we dropped below 2, cancel it
    if pend.get("arming_at") and len(pend["users"]) < 2:
        pend["arming_at"] = None
        try:
            ch = guild.get_channel(pend["channel_id"])
            await send_boxed(ch, "Bounty", "⏹️ Arming **cancelled** — need 2 players again.", icon="🎯")
        except Exception:
            pass

    # Edit roster on the card
    try:
        ch = guild.get_channel(pend["channel_id"])
        if isinstance(ch, discord.TextChannel):
            msg = await ch.fetch_message(pend["message_id"])
            names = []
            for id_ in sorted(pend["users"]):
                try:
                    m = guild.get_member(id_) or await guild.fetch_member(id_)
                    names.append(m.mention if m else f"<@{id_}>")
                except Exception:
                    names.append(f"<@{id_}>")
            players_txt = ", ".join(names) if names else "—"
            desc = (
                f"React with {EMO_BOUNTY()} to **arm** this bounty — need **2** players.\n"
                f"After 2 react, the bounty **arms in {BOUNTY_ARM_DELAY_S//60} minute**.\n"
                f"**Prize:** {BOUNTY_PAYOUT} {EMO_SHEKEL()}\n"
                "Use `bg APPLE` or `/worldle_bounty_guess` when armed.\n\n"
                f"⏲️ This prompt expires in {BOUNTY_EXPIRE_MIN} minutes."
            )
            emb = make_panel(
                title=f"{EMO_BOUNTY()} Hourly Bounty (GMT)",
                description=desc,
                fields=[("Players ready", players_txt, False)],
            )
            await msg.edit(embed=emb)
    except Exception:
        pass


# -------------------- scheduler --------------------
@tasks.loop(seconds=20)
async def bounty_loop():
    if not bot:
        return

    now = gmt_now_s()
    hour_idx = current_hour_index_gmt()
    within_window = (now % 3600) < 40  # first ~40s of the hour

    # 1) Expire pending (not armed) prompts
    for gid, pend in list(pending_bounties.items()):
        try:
            if now >= pend.get("expires_at", 0):
                pending_bounties.pop(gid, None)
                guild = discord.utils.get(bot.guilds, id=gid)
                if not guild:
                    continue
                ch = guild.get_channel(pend["channel_id"])

                # Suppress next-hour bounty ping
                try:
                    await set_cfg(guild.id, suppress_bounty_ping=1)
                except Exception:
                    pass

                # +1 to Word Pot
                pot = await get_casino_pot(gid)
                new_pot = pot + 1
                await set_casino_pot(gid, new_pot)

                if isinstance(ch, discord.TextChannel):
                    emb = make_panel(
                        title="⏲️ Bounty prompt expired",
                        description=f"+1 {EMO_SHEKEL()} to **Word Pot** (now **{new_pot}**).",
                    )
                    try:
                        msg = await ch.fetch_message(pend["message_id"])
                        await msg.reply(embed=emb)
                    except Exception:
                        await safe_send(ch, embed=emb)
        except Exception:
            continue

    # 1.5) Arm any prompts whose countdown reached zero
    for gid, pend in list(pending_bounties.items()):
        try:
            arm_at = pend.get("arming_at")
            if arm_at and now >= arm_at and len(pend.get("users", set())) >= 2:
                guild = discord.utils.get(bot.guilds, id=gid)
                if not guild:
                    continue
                ch = guild.get_channel(pend["channel_id"])
                if isinstance(ch, discord.TextChannel):
                    try:
                        msg = await ch.fetch_message(pend["message_id"])
                        await msg.reply("🔔 **Arming now!**")
                    except Exception:
                        await safe_send(ch, "🔔 **Arming now!**")

                channel_id = pend["channel_id"]
                pending_bounties.pop(gid, None)
                await _start_bounty_after_gate(guild, channel_id)
        except Exception:
            continue

    # 2) Expire ARMED bounties
    for gid, game in list(bounty_games.items()):
        try:
            if now >= game.get("expires_at", 0):
                bounty_games.pop(gid, None)
                guild = discord.utils.get(bot.guilds, id=gid)
                if not guild:
                    continue
                ch = guild.get_channel(game["channel_id"])

                # Suppress next-hour ping
                try:
                    await set_cfg(guild.id, suppress_bounty_ping=1)
                except Exception:
                    pass

                # +1 to Word Pot
                pot = await get_casino_pot(gid)
                new_pot = pot + 1
                await set_casino_pot(gid, new_pot)

                if isinstance(ch, discord.TextChannel):
                    emb = make_panel(
                        title="⏲️ Bounty expired",
                        description=(
                            f"No solve in **{BOUNTY_EXPIRE_MIN} minutes**.\n"
                            f"+1 {EMO_SHEKEL()} to **Word Pot** (now **{new_pot}**)."
                        ),
                    )
                    await safe_send(ch, embed=emb)
        except Exception:
            continue

    # 3) Drop a NEW bounty prompt this hour
    if within_window:
        for guild in list(bot.guilds):
            try:
                if guild.id in bounty_games or guild.id in pending_bounties:
                    continue
                cfg = await get_cfg(guild.id)
                if cfg.get("last_bounty_hour", 0) == hour_idx:
                    continue
                ch = await _find_bounty_channel(guild)
                if not ch:
                    continue
                await _post_bounty_prompt(guild, ch, hour_idx)
            except Exception:
                continue


@bounty_loop.before_loop
async def _before_bounty_loop():
    # wait for client
    if bot:
        await bot.wait_until_ready()


# -------------------- registration --------------------
def register(_bot: commands.Bot, _tree: app_commands.CommandTree) -> None:
    """Wire commands, listeners, and start the hourly loop."""
    global bot, tree
    bot, tree = _bot, _tree

    # commands
    _bind_commands(_tree)

    # listeners (attach once)
    if not hasattr(bot, "_ww_bounty_listeners"):
        async def _add(payload: discord.RawReactionActionEvent):
            try:
                await _on_raw_reaction_add(payload)
            except Exception:
                pass

        async def _remove(payload: discord.RawReactionActionEvent):
            try:
                await _on_raw_reaction_remove(payload)
            except Exception:
                pass

        bot.add_listener(_add, name="on_raw_reaction_add")
        bot.add_listener(_remove, name="on_raw_reaction_remove")
        bot._ww_bounty_listeners = True  # type: ignore[attr-defined]

    # start loop
    if not bounty_loop.is_running():
        bounty_loop.start()
