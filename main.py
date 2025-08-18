# -------------------- Text shortcuts --------------------
class Shim:
    def __init__(self, message: discord.Message):
        self.guild = message.guild
        self.channel = message.channel
        self.user = message.author
        class Resp:
            def __init__(self, ch): self._ch = ch
            async def send_message(self, content=None, **kwargs):
                kwargs.pop("ephemeral", None)
                return await self._ch.send(content, **kwargs)
        class Follow:
            def __init__(self, ch): self._ch = ch
            async def send(self, content=None, **kwargs):
                kwargs.pop("ephemeral", None)
                return await self._ch.send(content, **kwargs)
        self.response = Resp(self.channel)
        self.followup = Follow(self.channel)
        self.command = None

@bot.event
async def on_message(msg: discord.Message):
    if not msg.guild or msg.author.bot:
        return

    # 🔔 10% random shekel drop on EVERY user message (boxed with Collect button)
    # (requires the ShekelDropView + maybe_drop_shekel_on_message you added earlier)
    try:
        await maybe_drop_shekel_on_message(msg)
    except Exception as e:
        log.warning(f"shekel drop failed: {e}")

    content = msg.content.strip()
    if not content:
        return

    lower = content.lower()

    # --- SOLO shortcut ---
    if lower == "w":
        if not await guard_worldler_msg(msg):
            await send_boxed(msg.channel, "Access Required", f"{msg.author.mention} you need **{WORLDLER_ROLE_NAME}**. Use `/immigrate` to join.", icon="🔐")
            return
        ch = await solo_start(msg.channel, msg.author)
        if isinstance(ch, discord.TextChannel):
            await send_boxed(msg.channel, "Solo Room Opened", f"{msg.author.mention} your room is {ch.mention}.", icon="🧩")
        return

    # --- CASINO shortcut (Word Pot) ---
    if lower == "wc":
        if not await guard_worldler_msg(msg):
            await send_boxed(msg.channel, "Access Required", f"{msg.author.mention} you need **{WORLDLER_ROLE_NAME}**. Use `/immigrate`.", icon="🔐")
            return
        ch = await casino_start_word_pot(msg.channel, msg.author)
        if isinstance(ch, discord.TextChannel):
            await send_boxed(msg.channel, "Word Pot Room Opened", f"{msg.author.mention} your room is {ch.mention}.", icon="🎰")
        return

    # --- GUESS (smart: duel turn / bounty / casino / dungeon / solo) ---
    if lower.startswith("g "):
        if not await guard_worldler_msg(msg):
            await send_boxed(msg.channel, "Access Required", f"{msg.author.mention} you need **{WORLDLER_ROLE_NAME}**. Use `/immigrate`.", icon="🔐")
            return
        word = content.split(None, 1)[1]

        did = _duel_in_channel(msg.channel.id)
        if did:
            d = duels.get(did)
            if d and d["state"] == "active" and msg.author.id == d["turn"]:
                inter = Shim(msg)
                await worldle_duel_guess.callback(inter, did, word)
                return

        game = bounty_games.get(msg.guild.id)
        if game and game["channel_id"] == msg.channel.id:
            inter = Shim(msg)
            await worldle_bounty_guess.callback(inter, word)
            return

        if _key(msg.guild.id, msg.channel.id, msg.author.id) in casino_games:
            await casino_guess(msg.channel, msg.author, word)
            return

        if msg.channel.id in dungeon_games:
            await dungeon_guess(msg.channel, msg.author, word)
            return

        await solo_guess(msg.channel, msg.author, word)
        return

    # --- Bounty guess shortcut ---
    if lower.startswith("bg "):
        if not await guard_worldler_msg(msg):
            await send_boxed(msg.channel, "Access Required", f"{msg.author.mention} you need **{WORLDLER_ROLE_NAME}**. Use `/immigrate`.", icon="🔐")
            return
        word = content.split(None, 1)[1]
        inter = Shim(msg)
        await worldle_bounty_guess.callback(inter, word)
        return






# -------------------- run --------------------
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Missing DISCORD_TOKEN in environment.")
    bot.run(TOKEN)
