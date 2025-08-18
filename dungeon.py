# -------------------- DUNGEON globals --------------------
pending_dungeon_gates_by_msg: dict[int, dict] = {}   # gate_msg_id -> {...}
dungeon_games: dict[int, dict] = {}                  # ch_id -> game dict


# ---- Announce cards (UI/UX) ----
CARD_COLOR_DEFAULT = 0x2B2D31
CARD_COLOR_SUCCESS = 0x57F287  # green
CARD_COLOR_FAIL    = 0xED4245  # red
CARD_COLOR_INFO    = 0x5865F2  # blurple

def make_card(title: str, description: Optional[str] = None, *, fields: Optional[list[tuple[str,str,bool]]] = None, color: Optional[int] = None) -> discord.Embed:
    """
    Build a neat announcement embed. Use field tuples as (name, value, inline).
    """
    emb = discord.Embed(
        title=title,
        description=description or "",
        color=(color if color is not None else CARD_COLOR_DEFAULT),
    )
    if fields:
        for name, value, inline in fields:
            emb.add_field(name=name, value=(value or "—"), inline=inline)
    return emb


COLOR_PRIORITY = {"gray":0, "yellow":1, "green":2}

def update_legend(legend: dict[str,str], word: str, colors: list[str]):
    for ch, col in zip(word, colors):
        prev = legend.get(ch)
        if prev is None or COLOR_PRIORITY[col] > COLOR_PRIORITY[prev]:
            legend[ch] = col

ALPHABET = list("abcdefghijklmnopqrstuvwxyz")

def legend_overview(legend: dict[str, str], guesses: Optional[list[dict]] = None) -> str:
    """Render the legend:
       - Correct (green), Present (yellow)
       - Absent (shown with RED tiles)
       - Not used (letters never guessed; shown with GREY tiles)
    """
    if not legend:
        # still show Not used if we have guesses
        used_letters = set()
        if guesses:
            for g in guesses:
                used_letters.update(g.get("word", ""))
        else:
            return ""
    else:
        greens  = sorted([ch for ch, c in legend.items() if c == "green"])
        yellows = sorted([ch for ch, c in legend.items() if c == "yellow"])
        grays   = sorted([ch for ch, c in legend.items() if c == "gray"])

    # Determine which letters are not used yet
    if guesses:
        used_letters = set()
        for g in guesses:
            used_letters.update(g.get("word", ""))
    else:
        used_letters = set(legend.keys())

    not_used = [ch for ch in "abcdefghijklmnopqrstuvwxyz" if ch not in used_letters]

    parts = []
    if legend:
        if greens:
            parts.append("**Correct**: " + " ".join(render_tile(ch, "green") for ch in greens))
        if yellows:
            parts.append("**Present**: " + " ".join(render_tile(ch, "yellow") for ch in yellows))
        if grays:
            # show ABSENT with RED tiles
            parts.append("**Absent**: " + " ".join(render_tile(ch, "red") for ch in grays))

    if not_used:
        parts.append("**Not used**: " + " ".join(render_tile(ch, "gray") for ch in not_used))

    return "\n".join(parts)







# -------------------- DUNGEON emojis/helpers --------------------
EMO_DUNGEON_NAME = os.getenv("WW_DUNGEON_NAME", "ww_dungeon")

def EMO_DUNGEON() -> str:
    e = discord.utils.find(lambda em: em.name.lower() == EMO_DUNGEON_NAME.lower(), bot.emojis)
    return str(e) if e else "🌀"  # fallback

def _dungeon_join_emoji_matches(emoji: discord.PartialEmoji) -> bool:
    if emoji.is_unicode_emoji():
        return emoji.name == "🌀"
    return (emoji.name or "").lower() == EMO_DUNGEON_NAME.lower()

def _lock_emoji_matches(emoji: discord.PartialEmoji) -> bool:
    return emoji.is_unicode_emoji() and emoji.name == "🔒"

def _continue_emoji_matches(emoji: discord.PartialEmoji) -> bool:
    return emoji.is_unicode_emoji() and emoji.name == "⏩"

def _cashout_emoji_matches(emoji: discord.PartialEmoji) -> bool:
    return emoji.is_unicode_emoji() and emoji.name == "💰"



# -------------------- DUNGEON tickets (T1/T2/T3) --------------------
async def get_dungeon_tickets_t1(gid: int, uid: int) -> int:
    async with bot.db.execute("SELECT dungeon_tickets_t1 FROM inv WHERE guild_id=? AND user_id=?", (gid, uid)) as cur:
        row = await cur.fetchone()
    return row[0] if row else 0

async def change_dungeon_tickets_t1(gid: int, uid: int, delta: int):
    await bot.db.execute("""
      INSERT INTO inv(guild_id,user_id,dungeon_tickets_t1) VALUES(?,?,?)
      ON CONFLICT(guild_id,user_id) DO UPDATE SET dungeon_tickets_t1=inv.dungeon_tickets_t1+excluded.dungeon_tickets_t1
    """, (gid, uid, delta))
    await bot.db.commit()

async def get_dungeon_tickets_t2(gid: int, uid: int) -> int:
    async with bot.db.execute("SELECT dungeon_tickets_t2 FROM inv WHERE guild_id=? AND user_id=?", (gid, uid)) as cur:
        row = await cur.fetchone()
    return row[0] if row else 0

async def change_dungeon_tickets_t2(gid: int, uid: int, delta: int):
    await bot.db.execute("""
      INSERT INTO inv(guild_id,user_id,dungeon_tickets_t2) VALUES(?,?,?)
      ON CONFLICT(guild_id,user_id) DO UPDATE SET dungeon_tickets_t2=inv.dungeon_tickets_t2+excluded.dungeon_tickets_t2
    """, (gid, uid, delta))
    await bot.db.commit()

async def get_dungeon_tickets_t3(gid: int, uid: int) -> int:
    async with bot.db.execute("SELECT dungeon_tickets_t3 FROM inv WHERE guild_id=? AND user_id=?", (gid, uid)) as cur:
        row = await cur.fetchone()
    return row[0] if row else 0

async def change_dungeon_tickets_t3(gid: int, uid: int, delta: int):
    await bot.db.execute("""
      INSERT INTO inv(guild_id,user_id,dungeon_tickets_t3) VALUES(?,?,?)
      ON CONFLICT(guild_id,user_id) DO UPDATE SET dungeon_tickets_t3=inv.dungeon_tickets_t3+excluded.dungeon_tickets_t3
    """, (gid, uid, delta))
    await bot.db.commit()


# -------------------- DUNGEON channel factory --------------------
async def _make_dungeon_channel(invocation_channel: discord.TextChannel, owner: discord.Member) -> Optional[discord.TextChannel]:
    guild = invocation_channel.guild
    me = guild.me
    if not me or not me.guild_permissions.manage_channels:
        await invocation_channel.send("I need **Manage Channels** to open the dungeon.", delete_after=20)
        return None

    cfg = await get_cfg(guild.id)
    rid = cfg["worldler_role_id"] or await ensure_worldler_role(guild)
    worldler_role = guild.get_role(rid) if rid else None

    category = guild.get_channel(cfg["solo_category_id"]) if cfg.get("solo_category_id") else None
    if category and not isinstance(category, discord.CategoryChannel):
        category = None

    base = re.sub(r"[^a-zA-Z0-9]+", "-", owner.display_name).strip("-").lower() or f"user-{owner.id}"
    base = f"{base}-dungeon"
    name = base
    i = 2
    while discord.utils.get(guild.text_channels, name=name):
        name = f"{base}-{i}"; i += 1

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False, mention_everyone=False),
        owner: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, mention_everyone=False),
        me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True, mention_everyone=False),
    }
    if worldler_role:
        overwrites[worldler_role] = discord.PermissionOverwrite(view_channel=True, send_messages=False, read_message_history=True, mention_everyone=False)

    ch = await guild.create_text_channel(name=name, overwrites=overwrites, category=category, reason="Wordle Dungeon")
    return ch



# -------------------- DUNGEON logic --------------------
def _dungeon_max_for_tier(tier: int) -> int:
    return 5 if tier == 3 else 4 if tier == 2 else 3  # T3=5, T2=4, T1=3

def _dungeon_mult_for_tier(tier: int) -> int:
    return 1 if tier == 3 else 2 if tier == 2 else 3  # T3 base, T2 double, T1 triple

def _dungeon_new_answer() -> str:
    return random.choice(ANSWERS)

async def _dungeon_settle_and_close(game: dict, payout_each: int, note: str):
    gid = game["guild_id"]
    ch_id = game["channel_id"]
    tier = game.get("tier", "?")
    origin_cid = game.get("origin_cid")
    part_ids = sorted(game.get("participants", set()))
    num_parts = len(part_ids)

    ch = discord.utils.get(bot.get_all_channels(), id=ch_id)
    guild = ch.guild if isinstance(ch, discord.TextChannel) else discord.utils.get(bot.guilds, id=gid)

    # pay
    for uid in part_ids:
        try:
            await change_balance(gid, uid, payout_each, announce_channel_id=ch_id)
        except Exception:
            pass

    # participants (mentions)
    names = []
    if guild:
        for uid in part_ids:
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
                ("Rewards", f"**{payout_each}** {EMO_SHEKEL()} each · Pool: **{max(0, game.get('pool',0))}**", False),
                (f"Rounds solved ({solved_cnt})", solved_block, False),
            ]
        )
        try:
            await ch.send(embed=emb)
        except Exception:
            pass

    # public announcement (same panel) to announcements channel
    if guild:
        summary_emb = make_panel(
            title=f"Dungeon Finished — Tier {tier}",
            description=note,
            icon="🧱",
            fields=[
                ("Participants", names_txt, False),
                ("Rewards", f"**{payout_each}** {EMO_SHEKEL()} each · Pool: **{max(0, game.get('pool',0))}**", False),
                (f"Rounds solved ({solved_cnt})", solved_block, False),
            ]
        )
        try:
            await _announce_result(guild, origin_cid, "",)  # ensure channel chosen
            # send directly to announcements channel using _announce_result's channel picking
            # (empty content makes _announce_result post only the board/body; we will send panel ourselves)
            cfg = await get_cfg(guild.id)
            ann_id = cfg.get("announcements_channel_id")
            if ann_id:
                ann_ch = guild.get_channel(ann_id)
                if isinstance(ann_ch, discord.TextChannel):
                    await safe_send(ann_ch, embed=summary_emb)
        except Exception:
            pass

    dungeon_games.pop(ch_id, None)
    for mid, g in list(pending_dungeon_gates_by_msg.items()):
        if g.get("dungeon_channel_id") == ch_id:
            pending_dungeon_gates_by_msg.pop(mid, None)

    if isinstance(ch, discord.TextChannel):
        try:
            await ch.delete(reason="Dungeon closed")
        except Exception:
            pass







async def _dungeon_start_round(game: dict):
    # keep cumulative list across rounds
    game.setdefault("solved_rounds", [])  # list[str] of solved ANSWERS (UPPER)
    game["answer"] = _dungeon_new_answer()
    game["guesses"] = []
    game["legend"] = {}
    game["max"] = _dungeon_max_for_tier(game["tier"])
    game["state"] = "active"

    ch = discord.utils.get(bot.get_all_channels(), id=game["channel_id"])
    if isinstance(ch, discord.TextChannel):
        await ch.send(
            f"🗝️ **New Wordle begins!** Tier **{game['tier']}** — you have **{game['max']} tries**.\n"
            f"Guess with `g APPLE` here."
        )
        blank_board = render_board([], total_rows=game["max"])
        await ch.send(blank_board)





async def dungeon_guess(channel: discord.TextChannel, author: discord.Member, word: str):
    ch_id = channel.id
    game = dungeon_games.get(ch_id)
    if not game or game.get("state") not in ("active",):
        await safe_send(channel, "No active dungeon round right now.")
        return
    if author.id not in game["participants"]:
        await safe_send(channel, f"{author.mention} you're not registered for this dungeon.", allowed_mentions=discord.AllowedMentions.none())
        return

    cleaned = "".join(ch for ch in word.lower().strip() if ch.isalpha())
    if len(cleaned) != 5:
        await safe_send(channel, "Guess must be **exactly 5 letters**.")
        return
    if not is_valid_guess(cleaned):
        await safe_send(channel, "That’s not in the Wordle dictionary (UK variants supported).")
        return
    if len(game["guesses"]) >= game["max"]:
        await safe_send(channel, "Out of tries for this round.")
        return

    colors = score_guess(cleaned, game["answer"])
    game["guesses"].append({"word": cleaned, "colors": colors})
    update_legend(game["legend"], cleaned, colors)

    board = render_board(game["guesses"], total_rows=game["max"])
    await safe_send(channel, board)

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

        # Loot: 40% stone, 10% ticket down-tier (T3->T2, T2->T1)
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
        await safe_send(channel,
            f"✅ **Solved on attempt {attempt}!** Added **+{gained} {EMO_SHEKEL()}** to the dungeon pool "
            f"(now **{game['pool']}**).{extra}\n"
            f"{legend}\n\n"
            f"**Owner**: react **⏩** to **Continue** or **💰** to **Cash Out** for everyone."
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
        from math import ceil
        half_each = ceil(max(0, game.get("pool", 0)) / 2)
        await _dungeon_settle_and_close(game, half_each, note="❌ Round failed; reward halved (rounded up).")
        return

    next_attempt = attempt + 1
    payout = payout_for_attempt(next_attempt) * _dungeon_mult_for_tier(game["tier"])
    hint = legend_overview(game["legend"])
    txt = f"Attempt **{attempt}/{game['max']}** — Solve on attempt **{next_attempt}** to add **+{payout}** to the pool."
    if hint: txt += f"\n{hint}"
    await safe_send(channel, txt)






@tree.command(name="worldle_dungeon", description="Open a Worldle Dungeon (Tier 1/2/3).")
@app_commands.describe(tier="Dungeon tier")
@app_commands.choices(tier=[
    app_commands.Choice(name="Tier 1 (triple rewards · 3 tries)", value=1),
    app_commands.Choice(name="Tier 2 (double rewards · 4 tries)", value=2),
    app_commands.Choice(name="Tier 3 (base rewards · 5 tries)",   value=3),
])
async def worldle_dungeon_open(inter: discord.Interaction, tier: app_commands.Choice[int]):
    if not await guard_worldler_inter(inter): return
    if not inter.guild or not inter.channel: return
    gid, uid = inter.guild.id, inter.user.id
    t = tier.value

    # Check ticket ownership
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

    # Consume ticket
    if t == 3:   await change_dungeon_tickets_t3(gid, uid, -1)
    elif t == 2: await change_dungeon_tickets_t2(gid, uid, -1)
    else:        await change_dungeon_tickets_t1(gid, uid, -1)

    # Create dungeon channel
    ch = await _make_dungeon_channel(inter.channel, inter.user)
    if not ch:
        # refund on failure to create the channel
        if t == 3:   await change_dungeon_tickets_t3(gid, uid, +1)
        elif t == 2: await change_dungeon_tickets_t2(gid, uid, +1)
        else:        await change_dungeon_tickets_t1(gid, uid, +1)
        return await inter.followup.send("Couldn't create the dungeon channel (ticket refunded).")

    # Register game (track origin_cid for announcements later)
    dungeon_games[ch.id] = {
        "guild_id": gid,
        "channel_id": ch.id,
        "owner_id": uid,
        "tier": t,
        "participants": {uid},
        "state": "await_start",
        "answer": None, "guesses": [], "max": _dungeon_max_for_tier(t), "legend": {}, "pool": 0,
        "gate_msg_id": None, "welcome_msg_id": None, "decision_msg_id": None,
        "origin_cid": inter.channel.id,  # <— used by _announce_result at the end
        "solved_words": [],              # <— NEW: keep a history of solved words
    }

    # Gate message in current channel (to join)
    join_msg = await inter.channel.send(
        f"{EMO_DUNGEON()} **Dungeon Gate (Tier {t}) opened by {inter.user.mention}!**\n"
        f"Click {EMO_DUNGEON()} below **to join**. You’ll gain write access in {ch.mention}.\n"
        f"When ready, the owner will **lock** the dungeon from inside to start the game.",
        allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False)
    )
    try:
        await join_msg.add_reaction(EMO_DUNGEON())
    except Exception:
        try: await join_msg.add_reaction("🌀")
        except Exception: pass

    pending_dungeon_gates_by_msg[join_msg.id] = {
        "guild_id": gid,
        "gate_channel_id": inter.channel.id,
        "dungeon_channel_id": ch.id,
        "owner_id": uid,
        "participants": {uid},
        "tier": t,
        "state": "gate_open",
    }

    # Spooky welcome in dungeon channel with lock control
    welcome_txt = (
        "🕯️ **Welcome, adventurers…**\n"
        "The air is cold and the walls whisper letters you cannot see.\n"
        "Solve quickly or **lose half your spoils** to the shadows.\n\n"
        f"**Tier {t}**: rewards multiplier ×{_dungeon_mult_for_tier(t)}, tries **{_dungeon_max_for_tier(t)}** per Wordle.\n"
        "When everyone has joined, the **owner** must click **🔒** below to seal the gate and begin."
    )
    welcome = await ch.send(
        f"🌀 **Dungeon — Tier {t}**\nParticipants: <@{uid}> (owner)\n\n{welcome_txt}",
        allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False)
    )
    try:
        await welcome.add_reaction("🔒")
    except Exception:
        pass

    dungeon_games[ch.id]["gate_msg_id"] = join_msg.id
    dungeon_games[ch.id]["welcome_msg_id"] = welcome.id

    await inter.followup.send(f"Opened {ch.mention} and posted a **join gate** here. Players must react {EMO_DUNGEON()} to join.")
