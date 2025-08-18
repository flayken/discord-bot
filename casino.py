# -------------------- Slash wrappers: Solo + Casino --------------------
@tree.command(name="worldle", description="Start your own Wordle in a private room (free, 5/day).")
async def worldle_start(inter: discord.Interaction):
    if not await guard_worldler_inter(inter): return
    if not inter.guild or not inter.channel: return
    await inter.response.defer(thinking=False)
    ch = await solo_start(inter.channel, inter.user)
    if isinstance(ch, discord.TextChannel):
        await send_boxed(inter, "Solo Room Opened", f"{inter.user.mention} your room is {ch.mention}.", icon="🧩")

@tree.command(name="worldle_casino", description="Play a casino Wordle. First game: Word Pot.")
@app_commands.describe(game="Pick a casino game")
@app_commands.choices(game=[app_commands.Choice(name="Word Pot", value="word_pot")])
async def worldle_casino(inter: discord.Interaction, game: Optional[app_commands.Choice[str]] = None):
    if not await guard_worldler_inter(inter): return
    if not inter.guild or not inter.channel: return
    await inter.response.defer(thinking=False)
    choice = (game.value if game else "word_pot")
    if choice != "word_pot":
        return await send_boxed(inter, "Casino", "Only **Word Pot** is available right now.", icon="🎰")
    ch = await casino_start_word_pot(inter.channel, inter.user)
    if isinstance(ch, discord.TextChannel):
        await send_boxed(inter, "Word Pot Room Opened", f"{inter.user.mention} your room is {ch.mention}.", icon="🎰")


@tree.command(name="worldle_guess", description="Guess your word in this channel.")
@app_commands.describe(word="5-letter guess")
async def worldle_guess(inter: discord.Interaction, word: str):
    if not await guard_worldler_inter(inter): return
    if not inter.guild or not inter.channel: return
    await inter.response.defer(thinking=False)
    gid, cid, uid = inter.guild.id, inter.channel.id, inter.user.id

    if _key(gid, cid, uid) in solo_games:
        await solo_guess(inter.channel, inter.user, word)
    elif _key(gid, cid, uid) in casino_games:
        await casino_guess(inter.channel, inter.user, word)
    elif cid in dungeon_games:
        await dungeon_guess(inter.channel, inter.user, word)
    else:
        # keeps the nice "no game here" message for solo if applicable
        await solo_guess(inter.channel, inter.user, word)


@tree.command(name="worldle_end", description="End your current Wordle here (counts as a fail).")
async def worldle_end(inter: discord.Interaction):
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
        definition_str = f"\n📖 Definition: {definition}" if definition else ""
        await inter.followup.send(
            f"🛑 Ended your **Word Pot** game. The word was **{ans}** — {quip}{definition_str}\n"
            f"Pot is now **{new_pot} {EMO_SHEKEL()}**."
        )

        fields = [("Board", board, False), ("Pot", f"Now **{new_pot} {EMO_SHEKEL()}**", True)]
        if definition:
            fields.append(("Definition", definition, False))

        emb = make_card(
            title="🎰 Word Pot — Ended Early",
            description=f"{inter.user.mention} ended their Word Pot early. The word was **{ans}** — {quip}",
            fields=fields,
            color=CARD_COLOR_FAIL,
        )
        await _announce_result(inter.guild, origin_cid, content="", embed=emb)

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
    definition_str = f"\n📖 Definition: {definition}" if definition else ""
    await inter.followup.send(
        f"🛑 Ended your game. The word was **{ans}** — {quip}{definition_str}"
    )
    fields = [("Board", board, False)]
    if definition:
        fields.append(("Definition", definition, False))

    emb = make_card(
        title="💀 Solo — Ended Early",
        description=f"{inter.user.mention} failed their Worldle (ended early). The word was **{ans}** — {quip}",
        fields=fields,
        color=CARD_COLOR_FAIL,
    )
    await _announce_result(inter.guild, origin_cid, content="", embed=emb)

    try:
        await inter.channel.delete(reason="Wordle World solo ended by user (fail)")
    except Exception:
        pass
