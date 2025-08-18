# -------------------- Duels --------------------
def _new_duel_id() -> int:
    global _next_duel_id
    did = _next_duel_id; _next_duel_id += 1; return did

def _duel_in_channel(ch_id: int) -> Optional[int]:
    for k, d in duels.items():
        if d["state"]=="active" and d["channel_id"] == ch_id:
            return k
    return None

@tree.command(name="worldle_challenge", description="Challenge a player to a Wordle duel for a stake.")
@app_commands.describe(user="Opponent", amount="Stake (shekels)")
async def worldle_challenge(inter: discord.Interaction, user: discord.Member, amount: int):
    if not await guard_worldler_inter(inter): return
    if not inter.guild or not inter.channel: return
    if user.bot or user.id == inter.user.id:
        return await inter.response.send_message("Pick a real opponent (not yourself/bots).", ephemeral=True)
    if amount <= 0:
        return await inter.response.send_message("Stake must be positive.", ephemeral=True)

    gid, cid = inter.guild.id, inter.channel.id
    for d in duels.values():
        if d["state"] in ("pending","active") and (d["challenger_id"] in (inter.user.id,user.id) or d["target_id"] in (inter.user.id,user.id)):
            return await inter.response.send_message("Either you or they are already in a pending/active duel.", ephemeral=True)
    if await get_balance(gid, inter.user.id) < amount:
        return await inter.response.send_message("You don't have enough shekels.", ephemeral=True)

    did = _new_duel_id()
    duels[did] = {
        "id": did, "guild_id": gid, "channel_id": cid,
        "challenger_id": inter.user.id, "target_id": user.id,
        "stake": amount, "pot": 0, "state": "pending", "created": time.time(),
        "answer": None, "turn": None,
        "guesses": {inter.user.id: [], user.id: []},
    }
    await inter.response.send_message(
        f"⚔️ Duel **#{did}** created: {inter.user.mention} challenges {user.mention} for **{amount} {EMO_SHEKEL()}**.\n"
        f"{user.mention}, accept with `/worldle_accept id:{did}` or decline with `/worldle_cancel id:{did}`.",
        allowed_mentions=discord.AllowedMentions(users=[inter.user, user])
    )

@tree.command(name="worldle_accept", description="Accept a Wordle duel by ID.")
@app_commands.describe(id="Duel ID")
async def worldle_accept(inter: discord.Interaction, id: int):
    if not await guard_worldler_inter(inter): return
    d = duels.get(id)
    if not d or d["state"] != "pending":
        return await inter.response.send_message("No such pending duel.", ephemeral=True)
    if inter.channel.id != d["channel_id"]:
        ch = inter.guild.get_channel(d["channel_id"])
        return await inter.response.send_message(f"Use this in {ch.mention if ch else 'the duel channel'}.", ephemeral=True)
    if inter.user.id != d["target_id"]:
        return await inter.response.send_message("Only the challenged player can accept.", ephemeral=True)
    if time.time() - d["created"] > 10*60:
        d["state"] = "cancelled"
        return await inter.response.send_message("That duel expired.", ephemeral=True)

    gid, cid = d["guild_id"], d["channel_id"]
    a, b, stake = d["challenger_id"], d["target_id"], d["stake"]
    if await get_balance(gid, a) < stake or await get_balance(gid, b) < stake:
        d["state"] = "cancelled"
        return await inter.response.send_message("One of you no longer has enough shekels. Duel cancelled.", ephemeral=True)

    await change_balance(gid, a, -stake, announce_channel_id=cid)
    await change_balance(gid, b, -stake, announce_channel_id=cid)
    d["pot"] = stake * 2
    d["answer"] = random.choice(ANSWERS)
    d["turn"] = random.choice([a, b])
    d["state"] = "active"

    ch = inter.channel
    starter = f"<@{d['turn']}>"
    await ch.send(
        f"⚔️ Duel **#{id}** started between <@{a}> and <@{b}> for **{stake}** each (**pot {d['pot']} {EMO_SHEKEL()}**).\n"
        f"Starting player chosen at random: {starter} goes first.\n"
        f"Guess with `g APPLE` here or `/worldle_duel_guess id:{id} word:APPLE`."
    )
    await inter.response.send_message("Accepted. Good luck!", ephemeral=True)

@tree.command(name="worldle_duel_guess", description="Play your turn in a Wordle duel.")
@app_commands.describe(id="Duel ID", word="Your 5-letter guess")
async def worldle_duel_guess(inter: discord.Interaction, id: int, word: str):
    if not await guard_worldler_inter(inter): return
    d = duels.get(id)
    if not d or d["state"] != "active":
        return await inter.response.send_message("No such active duel.", ephemeral=True)
    if inter.channel.id != d["channel_id"]:
        ch = inter.guild.get_channel(d["channel_id"])
        return await inter.response.send_message(f"Use this in {ch.mention if ch else 'the duel channel'}.", ephemeral=True)

    uid = inter.user.id
    if uid not in (d["challenger_id"], d["target_id"]):
        return await inter.response.send_message("You're not in that duel.", ephemeral=True)
    if uid != d["turn"]:
        return await inter.response.send_message("It's not your turn.", ephemeral=True)

    cleaned = "".join(ch for ch in word.lower().strip() if ch.isalpha())
    if len(cleaned) != 5:
        return await inter.response.send_message("Guess must be exactly 5 letters.", ephemeral=True)
    if not is_valid_guess(cleaned):
        return await inter.response.send_message("That’s not in the Wordle dictionary (UK variants supported).", ephemeral=True)

    colors = score_guess(cleaned, d["answer"])
    d["guesses"][uid].append({"word": cleaned, "colors": colors})
    row = render_row(cleaned, colors)

    ch = inter.channel
    if cleaned == d["answer"]:
        await ch.send(row)
        await change_balance(d["guild_id"], uid, d["pot"], announce_channel_id=d["channel_id"])
        bal = await get_balance(d["guild_id"], uid)
        await ch.send(f"🏁 Duel **#{id}**: {inter.user.mention} guessed **{d['answer'].upper()}** and wins the pot **{d['pot']} {EMO_SHEKEL()}**! (Balance: {bal})")
        d["state"] = "finished"
        return await inter.response.send_message("You win!", ephemeral=True)

    other = d["challenger_id"] if uid == d["target_id"] else d["target_id"]
    d["turn"] = other
    await ch.send(row)
    await ch.send(f"**Duel #{id}** — It’s now <@{other}>'s turn.")
    await inter.response.send_message("Move submitted.", ephemeral=True)

@tree.command(name="worldle_cancel", description="Cancel your pending duel by ID.")
@app_commands.describe(id="Duel ID")
async def worldle_cancel(inter: discord.Interaction, id: int):
    if not await guard_worldler_inter(inter): return
    d = duels.get(id)
    if not d or d["state"] != "pending":
        return await inter.response.send_message("No such pending duel.", ephemeral=True)
    if inter.channel.id != d["channel_id"]:
        ch = inter.guild.get_channel(d["channel_id"])
        return await inter.response.send_message(f"Use this in {ch.mention if ch else 'the duel channel'}.", ephemeral=True)
    if inter.user.id not in (d["challenger_id"], d["target_id"]):
        return await inter.response.send_message("Only participants can cancel.", ephemeral=True)
    d["state"] = "cancelled"
    await inter.response.send_message("Duel cancelled.", ephemeral=True)
