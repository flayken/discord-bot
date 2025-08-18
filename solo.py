# -------------------- SOLO (private rooms + daily cap + announcements + streak touch) --------------------
def _key(gid, cid, uid): return (gid, cid, uid)

async def _make_private_solo_channel(invocation_channel: discord.TextChannel, member: discord.Member) -> Optional[discord.TextChannel]:
    guild = invocation_channel.guild
    me = guild.me
    if not me or not me.guild_permissions.manage_channels:
        await invocation_channel.send("I need **Manage Channels** to open your private Wordle room.", delete_after=20)
        return None

    cfg = await get_cfg(guild.id)
    rid = cfg["worldler_role_id"] or await ensure_worldler_role(guild)
    worldler_role = guild.get_role(rid) if rid else None

    category = guild.get_channel(cfg["solo_category_id"]) if cfg.get("solo_category_id") else None
    if category and not isinstance(category, discord.CategoryChannel):
        category = None

    base = re.sub(r"[^a-zA-Z0-9]+", "-", member.display_name).strip("-").lower() or f"user-{member.id}"
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

async def _announce_result(
    guild: discord.Guild,
    origin_cid: Optional[int],
    content: str,
    board: Optional[str] = None,
    *,
    title: str | None = None,
    color: discord.Color = discord.Color.blurple(),
    embed: Optional[discord.Embed] = None,
):
    """
    Post to the configured announcements channel.
    If `embed` is provided, use it. Otherwise build a simple card from title/content/board.
    If nothing to post (no embed, no content, no board), do nothing.
    """
    if not guild:
        return

    cfg = await get_cfg(guild.id)
    ann_id = cfg.get("announcements_channel_id")
    if not ann_id:
        return

    ch = guild.get_channel(ann_id)
    if not isinstance(ch, discord.TextChannel):
        return
    if not ch.permissions_for(guild.me).send_messages:
        return

    # If no payload at all, don't send a blank card (lets you "probe" channel in callers)
    if embed is None and not (content or board or title):
        return

    if embed is None:
        emb = discord.Embed(
            title=title or "📣 Announcement",
            description=content or "",
            color=color
        )
        if board:
            emb.add_field(name="Board", value=board, inline=False)
    else:
        emb = embed

    # Add source footer if we can (and don’t overwrite an existing one)
    if origin_cid and not emb.footer:
        src = guild.get_channel(origin_cid)
        if isinstance(src, discord.TextChannel):
            emb.set_footer(text=f"from {src.name}")

    await safe_send(
        ch,
        embed=emb,
        allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
    )








async def solo_start(invocation_channel: discord.TextChannel, user: discord.Member) -> Optional[discord.TextChannel]:
    gid, uid = invocation_channel.guild.id, user.id
    today = uk_today_str()  # UK-local reset

    plays = await get_solo_plays_today(gid, uid, today)
    if plays >= 5:
        await invocation_channel.send(f"{user.mention} you've reached your **5 solo games** for today. Resets at **00:00 UK time**.", allowed_mentions=discord.AllowedMentions.none())
        return None

    existing_cid = solo_channels.get((gid, uid))
    if existing_cid and _key(gid, existing_cid, uid) in solo_games:
        ch = invocation_channel.guild.get_channel(existing_cid)
        if isinstance(ch, discord.TextChannel):
            await invocation_channel.send(f"{user.mention} you already have a game running: {ch.mention}", allowed_mentions=discord.AllowedMentions.none())
            return ch
        else:
            solo_channels.pop((gid, uid), None)

    ch = await _make_private_solo_channel(invocation_channel, user)
    if not ch:
        return None

    solo_games[_key(gid, ch.id, uid)] = {
        "answer": random.choice(ANSWERS),
        "guesses": [],
        "max": 5,
        "legend": {},
        "origin_cid": invocation_channel.id,
        "start_date": uk_today_str(),  # record which UK day this slot was consumed
        "snipers_tried": set(),        # NEW: shooters who already took a shot at THIS game
    }
    solo_channels[(gid, uid)] = ch.id
    await inc_solo_plays_today(gid, uid, today)
    # Streak touch only once per UK day
    if plays == 0:
        await update_streak_on_play(gid, uid, today)

    board = render_board(solo_games[_key(gid, ch.id, uid)]["guesses"])
    left = 5 - (plays + 1)
    await ch.send(
        f"{user.mention} 🎮 **Your Wordle is ready!** (today’s uses left after this: **{left}**)\n"
        f"You have **5 tries**.\nPayouts if you solve: 1st=5, 2nd=4, 3rd=3, 4th=2, 5th=1.",
        allowed_mentions=discord.AllowedMentions(users=[user])
    )
    await ch.send(board)
    return ch



async def solo_guess(channel: discord.TextChannel, user: discord.Member, word: str):
    gid, cid, uid = channel.guild.id, channel.id, user.id
    game = solo_games.get(_key(gid,cid,uid))
    if not game:
        await channel.send(f"{user.mention} no game here. Start with `w` or `/worldle`.", allowed_mentions=discord.AllowedMentions.none())
        return

    cleaned = "".join(ch for ch in word.lower().strip() if ch.isalpha())
    if len(cleaned) != 5:
        await channel.send("Guess must be **exactly 5 letters**."); return
    if not is_valid_guess(cleaned):
        await channel.send("That’s not in the Wordle dictionary (UK variants supported)."); return
    if len(game["guesses"]) >= game["max"]:
        await channel.send("Out of tries! Start a new one with `w`."); return

    colors = score_guess(cleaned, game["answer"])
    game["guesses"].append({"word": cleaned, "colors": colors})
    update_legend(game["legend"], cleaned, colors)

    board = render_board(game["guesses"])
    attempt = len(game["guesses"])

    await channel.send(board)

    def _cleanup():
        solo_games.pop(_key(gid,cid,uid), None)
        if solo_channels.get((gid, uid)) == cid:
            solo_channels.pop((gid, uid), None)

    if cleaned == game["answer"]:
        payout = payout_for_attempt(attempt)
        if payout:
            await change_balance(gid, uid, payout, announce_channel_id=cid)
        bal_new = await get_balance(gid, uid)
        origin_cid = game.get("origin_cid")
        ans = game["answer"].upper()
        _cleanup()

        await channel.send(
            f"🎉 {user.mention} solved it on attempt **{attempt}**! **Word: {ans}** · Payout **{payout} {EMO_SHEKEL()}**. Balance **{bal_new}**.",
            allowed_mentions=discord.AllowedMentions.none()
        )

        emb = make_card(
            title="🏁 Solo — Finished",
            description=f"{user.mention} solved **{ans}** in **{attempt}** tries and earned **{payout} {EMO_SHEKEL()}**.",
            fields=[("Board", board, False)],  # <-- no code block
            color=CARD_COLOR_SUCCESS,
        )
        await _announce_result(channel.guild, origin_cid, content="", embed=emb)

        try: await channel.delete(reason="Wordle World solo finished (win)")
        except Exception: pass
        return

    if attempt == game["max"]:
        ans_raw = game["answer"]
        ans = ans_raw.upper()
        origin_cid = game.get("origin_cid")
        quip = random.choice(FAIL_QUIPS)
        definition = await fetch_definition(ans_raw)
        _cleanup()
        await inc_stat(gid, uid, "solo_fails", 1)
        bal_now = await get_balance(gid, uid)
        def_line = f"\n📖 Definition: {definition}" if definition else ""
        await channel.send(f"❌ Out of tries. The word was **{ans}** — {quip}{def_line}\nBalance **{bal_now}**.")

        fields = [("Board", board, False)]
        if definition:
            fields.append(("Definition", definition, False))
        emb = make_card(
            title="💀 Solo — Failed",
            description=f"{user.mention} failed their Worldle. The word was **{ans}** — {quip}",
            fields=fields,
            color=CARD_COLOR_FAIL,
        )
        await _announce_result(channel.guild, origin_cid, content="", embed=emb)

        try: await channel.delete(reason="Wordle World solo finished (out of tries)")
        except Exception: pass
        return

    next_attempt = attempt + 1
    legend = legend_overview(game["legend"], game["guesses"])
    payout = payout_for_attempt(next_attempt)
    msg = f"Attempt **{attempt}/{game['max']}** — If you solve on attempt **{next_attempt}**, payout will be **{payout}**."
    if legend: msg += f"\n{legend}"
    await channel.send(msg)
