# worldle_bot/features/bounty.py
from __future__ import annotations

import random
from typing import Dict, Any, Optional, Set, Tuple

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..core.config import (
    EMO_BOUNTY, EMO_SHEKEL,
    EMO_BOUNTY_NAME,
    ANSWERS,
)
from ..core.db import (
    change_balance,
    inc_stat,
    get_balance,
    get_casino_pot,
    set_casino_pot,
    get_cfg,
    set_cfg,
)
from ..core.utils import (
    guard_worldler_inter,
    is_worldler,
    send_boxed,
    safe_send,
    make_panel,
    make_card,
    score_guess,
    is_valid_guess,
    render_row,
    gmt_now_s,
    current_hour_index_gmt,
    ensure_bounty_role,
)

# ---------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------
bot: commands.Bot | None = None
tree: app_commands.CommandTree | None = None

# Pending (gate) bounties: guild_id -> { message_id, channel_id, users:set, hour_idx, expires_at, arming_at? }
pending_bounties: Dict[int, Dict[str, Any]] = {}

# Armed bounties: guild_id -> { answer, channel_id, started_at, expires_at }
bounty_games: Dict[int, Dict[str, Any]] = {}

# Per-user cooldown on guesses: (guild_id, user_id) -> last_ts
last_bounty_guess_ts: Dict[Tuple[int, int], int] = {}

# Settings
BOUNTY_PAYOUT = 5
BOUNTY_EXPIRE_MIN = 59
BOUNTY_EXPIRE_S = BOUNTY_EXPIRE_MIN * 60

BOUNTY_ARM_DELAY_S = 60              # wait 60s after 2 reactions before arming
BOUNTY_GUESS_COOLDOWN_S = 5          # 5s per-user cooldown between guesses


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------
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


def _bounty_emoji_matches(emoji: discord.PartialEmoji) -> bool:
    target_name = (EMO_BOUNTY_NAME or "ww_bounty").lower()
    if emoji.is_unicode_emoji():
        return emoji.name == "🎯"
    return (emoji.name or "").lower() == target_name


async def _post_bounty_prompt(guild: discord.Guild, channel: discord.TextChannel, hour_idx: int) -> bool:
    if guild.id in pending_bounties or guild.id in bounty_games:
        return False

    cfg = await get_cfg(guild.id)
    suppress_ping = int(cfg.get("suppress_bounty_ping", 0)) == 1

    rid = await ensure_bounty_role(guild)
    em = EMO_BOUNTY()
    role_mention = "" if suppress_ping else (f"<@&{rid}>" if rid else "")

    desc = (
        f"React with {em} to **arm** this bounty — need **2** players.\n"
        f"**After 2 react, the bounty arms in {BOUNTY_ARM_DELAY_S//60} minute.**\n"
        f"**Prize:** {BOUNTY_PAYOUT} {EMO_SHEKEL()}\n"
        "Use `bg APPLE` or `/worldle_bounty_guess` when armed.\n\n"
        f"⏲️ *This prompt expires in {BOUNTY_EXPIRE_MIN} minutes.*"
    )
    emb = make_panel(title=f"{em} Hourly Bounty (GMT)", description=desc)

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
        "users": set(),
        "hour_idx": hour_idx,
        "expires_at": gmt_now_s() + BOUNTY_EXPIRE_S,
        "arming_at": None,
    }
    await set_cfg(guild.id, last_bounty_hour=hour_idx)
    return True


async def _start_bounty_after_gate(guild: discord.Guild, channel_id: int) -> None:
    if guild.id in bounty_games:
        return
    answer = random.choice(ANSWERS)
    bounty_games[guild.id] = {
        "answer": answer,
        "channel_id": channel_id,
        "started_at": gmt_now_s(),
        "expires_at": gmt_now_s() + BOUNTY_EXPIRE_S,
    }
    await set_cfg(guild.id, last_bounty_ts=gmt_now_s(), suppress_bounty_ping=0)  # re-enable pings
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


# ---------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------
def _bind_commands(_tree: app_commands.CommandTree):

    @_tree.command(name="worldle_bounty_setchannel", description="(Admin) Set this channel for bounty drops.")
    @app_commands.default_permissions(administrator=True)
    async def worldle_bounty_setchannel(inter: discord.Interaction):
        if not inter.guild or not inter.channel:
            return await inter.response.send_message("Server only.", ephemeral=True)
        await set_cfg(inter.guild.id, bounty_channel_id=inter.channel.id)
        await inter.response.send_message(f"✅ Set bounty channel to {inter.channel.mention}.")

    @_tree.command(name="worldle_bounty_now", description="(Admin) Post a bounty prompt **now** (requires emoji arm).")
    @app_commands.default_permissions(administrator=True)
    async def worldle_bounty_now(inter: discord.Interaction):
        if not inter.guild or not inter.channel:
            return await inter.response.send_message("Server only.", ephemeral=True)
        await inter.response.defer(thinking=False)
        if inter.guild.id in bounty_games:
            return await inter.followup.send("There is already an active bounty.")
        if inter.guild.id in pending_bounties:
            return await inter.followup.send("There is already a pending bounty prompt.")
        ch = await _find_bounty_channel(inter.guild)
        if not ch:
            return await inter.followup.send("I can't find a channel I can speak in.")
        ok = await _post_bounty_prompt(inter.guild, ch, current_hour_index_gmt())
        await inter.followup.send("🎯 Bounty prompt posted — needs 2 reactions to arm." if ok else "Couldn't post a bounty prompt.")

    @_tree.command(name="worldle_bounty_guess", description="Guess the active bounty word.")
    @app_commands.describe(word="Your 5-letter guess")
    async def worldle_bounty_guess(inter: discord.Interaction, word: str):
        if not await guard_worldler_inter(inter):
            return
        if not inter.guild or not inter.channel:
            return
        game = bounty_games.get(inter.guild.id)
        if not game:
            return await inter.response.send_message("No active bounty right now.", ephemeral=True)
        if inter.channel.id != game["channel_id"]:
            ch = inter.guild.get_channel(game["channel_id"])
            return await inter.response.send_message(f"Use this in {ch.mention if ch else 'the bounty channel'}.", ephemeral=True)

        # Per-user cooldown
        now_s = gmt_now_s()
        key = (inter.guild.id, inter.user.id)
        last = last_bounty_guess_ts.get(key, 0)
        if (now_s - last) < BOUNTY_GUESS_COOLDOWN_S:
            wait = int(BOUNTY_GUESS_COOLDOWN_S - (now_s - last))
            return await inter.response.send_message(f"Slow down — **{wait}s** cooldown between guesses.", ephemeral=True)

        cleaned = "".join(ch for ch in word.lower().strip() if ch.isalpha())
        if len(cleaned) != 5:
            return await inter.response.send_message("Guess must be exactly 5 letters.", ephemeral=True)
        if not is_valid_guess(cleaned):
            return await inter.response.send_message("That’s not in the Wordle dictionary (UK variants supported).", ephemeral=True)

        # Start cooldown after accepting a valid guess
        last_bounty_guess_ts[key] = now_s

        colors = score_guess(cleaned, game["answer"])
        row = render_row(cleaned, colors)

        await inter.response.send_message(row)

        # WIN
        if cleaned == game["answer"]:
            gid, uid = inter.guild.id, inter.user.id
            await change_balance(gid, uid, BOUNTY_PAYOUT, announce_channel_id=game["channel_id"])
            await inc_stat(gid, uid, "bounties_won", 1)
            bal = await get_balance(gid, uid)

            # capture & clear
            ans_raw = game["answer"]
            ans_up = ans_raw.upper()
            del bounty_games[gid]

            await inter.followup.send(
                f"🏆 {inter.user.mention} solved the Bounty Wordle (**{ans_up}**) and wins **{BOUNTY_PAYOUT} {EMO_SHEKEL()}**! (Balance: {bal})"
            )

            # definition optional (uses dictionary API in your utils via fetch_definition in monolith).
            # To avoid an extra dependency here, we just announce with result row.
            fields = [("Result", row, False)]

            emb = make_card(
                title="🎯 Hourly Bounty — Solved",
                description=f"{inter.user.mention} wins **{BOUNTY_PAYOUT} {EMO_SHEKEL()}** by solving **{ans_up}**.",
                fields=fields,
                color=discord.Color.green(),
            )
            # Post to announcements channel (helper will no-op if not configured)
            await _announce_result(inter.guild, content="", embed=emb)
        else:
            await inter.followup.send("(Keep trying! Unlimited guesses.)")


# ---------------------------------------------------------------------
# Listener hooks
# ---------------------------------------------------------------------
async def _on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    gid = payload.guild_id
    if gid is None or not bot or (bot.user and payload.user_id == bot.user.id):
        return

    pend = pending_bounties.get(gid)
    if not pend or payload.message_id != pend.get("message_id") or not _bounty_emoji_matches(payload.emoji):
        return

    guild = discord.utils.get(bot.guilds, id=gid)
    if not guild:
        return

    try:
        member = guild.get_member(payload.user_id) or await guild.fetch_member(payload.user_id)
    except Exception:
        member = None

    if not member or member.bot or not await is_worldler(guild, member):
        return

    # add user to set
    pend["users"].add(member.id)

    # Build roster text
    names = []
    for uid in sorted(pend["users"]):
        try:
            m = guild.get_member(uid) or await guild.fetch_member(uid)
            names.append(m.mention if m else f"<@{uid}>")
        except Exception:
            names.append(f"<@{uid}>")
    players_txt = ", ".join(names) if names else "—"

    # Edit gate embed showing roster
    try:
        ch = guild.get_channel(pend["channel_id"])
        if isinstance(ch, discord.TextChannel):
            msg = await ch.fetch_message(pend["message_id"])
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

    # If we just reached 2 players, start arming countdown
    if len(pend["users"]) >= 2 and not pend.get("arming_at"):
        pend["arming_at"] = gmt_now_s() + BOUNTY_ARM_DELAY_S
        try:
            ch = guild.get_channel(pend["channel_id"])
            await send_boxed(ch, "Bounty", f"✅ Armed by {', '.join(names[:2])}. **Arming in {BOUNTY_ARM_DELAY_S//60} minute…**", icon="🎯")
        except Exception:
            pass


async def _on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    gid = payload.guild_id
    if gid is None:
        return
    pend = pending_bounties.get(gid)
    if not pend or payload.message_id != pend.get("message_id") or not _bounty_emoji_matches(payload.emoji):
        return

    uid = payload.user_id
    if uid and uid in pend.get("users", set()):
        pend["users"].discard(uid)

    if not bot:
        return

    guild = discord.utils.get(bot.guilds, id=gid)
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

    # Refresh roster on gate embed
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


# ---------------------------------------------------------------------
# Hourly loop
# ---------------------------------------------------------------------
@tasks.loop(seconds=20)
async def bounty_loop():
    if not bot:
        return
    now = gmt_now_s()
    hour_idx = current_hour_index_gmt()
    within_window = (now % 3600) < 40

    # 1) Expire pending prompts
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

    # 1.5) Arm any prompts whose countdown finished
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
    for guild in list(bot.guilds):
        try:
            if guild.id in bounty_games or guild.id in pending_bounties:
                continue
            cfg = await get_cfg(guild.id)
            if cfg.get("last_bounty_hour", 0) == hour_idx:
                continue
            if not within_window:
                continue
            ch = await _find_bounty_channel(guild)
            if not ch:
                continue
            await _post_bounty_prompt(guild, ch, hour_idx)
        except Exception:
            continue


@bounty_loop.before_loop
async def _before_bounty_loop():
    if bot:
        await bot.wait_until_ready()


# ---------------------------------------------------------------------
# Announce helper (re-uses your announcement channel logic)
# ---------------------------------------------------------------------
async def _announce_result(guild: discord.Guild, content: str = "", embed: Optional[discord.Embed] = None):
    """Post to configured announcements channel (no-op if unset)."""
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
        await safe_send(ch, content=content or None, embed=embed)
    except Exception:
        pass


# ---------------------------------------------------------------------
# Public router (for text shortcuts)
# ---------------------------------------------------------------------
async def maybe_route_guess(message: discord.Message, word: str) -> bool:
    """If this channel is the active bounty channel, route the guess like `/worldle_bounty_guess`."""
    game = bounty_games.get(getattr(message.guild, "id", 0))
    if not game or message.channel.id != game["channel_id"]:
        return False

    # Emulate the slash handler quickly (without ephemerals)
    now_s = gmt_now_s()
    key = (message.guild.id, message.author.id)
    last = last_bounty_guess_ts.get(key, 0)
    if (now_s - last) < BOUNTY_GUESS_COOLDOWN_S:
        wait = int(BOUNTY_GUESS_COOLDOWN_S - (now_s - last))
        await safe_send(message.channel, f"{message.author.mention} slow down — **{wait}s** cooldown.",)
        return True

    cleaned = "".join(ch for ch in word.lower().strip() if ch.isalpha())
    if len(cleaned) != 5 or not is_valid_guess(cleaned):
        await safe_send(message.channel, f"{message.author.mention} guess must be exactly 5 letters (valid Wordle word).")
        return True

    last_bounty_guess_ts[key] = now_s

    colors = score_guess(cleaned, game["answer"])
    row = render_row(cleaned, colors)
    await safe_send(message.channel, row)

    if cleaned == game["answer"]:
        gid, uid = message.guild.id, message.author.id
        await change_balance(gid, uid, BOUNTY_PAYOUT, announce_channel_id=game["channel_id"])
        await inc_stat(gid, uid, "bounties_won", 1)
        bal = await get_balance(gid, uid)
        ans_up = game["answer"].upper()
        bounty_games.pop(gid, None)

        await safe_send(
            message.channel,
            f"🏆 {message.author.mention} solved the Bounty Wordle (**{ans_up}**) and wins **{BOUNTY_PAYOUT} {EMO_SHEKEL()}**! (Balance: {bal})"
        )

        emb = make_card(
            title="🎯 Hourly Bounty — Solved",
            description=f"{message.author.mention} wins **{BOUNTY_PAYOUT} {EMO_SHEKEL()}** by solving **{ans_up}**.",
            fields=[("Result", row, False)],
            color=discord.Color.green(),
        )
        await _announce_result(message.guild, content="", embed=emb)
    return True


# ---------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------
def register(_bot: commands.Bot, _tree: app_commands.CommandTree) -> None:
    global bot, tree
    bot, tree = _bot, _tree

    _bind_commands(_tree)

    # listeners
    bot.add_listener(_on_raw_reaction_add, "on_raw_reaction_add")
    bot.add_listener(_on_raw_reaction_remove, "on_raw_reaction_remove")

    # start loop once
    if not bounty_loop.is_running():
        bounty_loop.start()
