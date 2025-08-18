# -------------------- CASINO: Word Pot (new) --------------------
async def casino_start_word_pot(invocation_channel: discord.TextChannel, user: discord.Member) -> Optional[discord.TextChannel]:
    gid, uid = invocation_channel.guild.id, user.id

    bal = await get_balance(gid, uid)
    if bal < 1:
        await invocation_channel.send(f"{user.mention} you need **1 {EMO_SHEKEL()}** to play Word Pot.", allowed_mentions=discord.AllowedMentions.none())
        return None

    existing_cid = casino_channels.get((gid, uid))
    if existing_cid and _key(gid, existing_cid, uid) in casino_games:
        ch = invocation_channel.guild.get_channel(existing_cid)
        if isinstance(ch, discord.TextChannel):
            await invocation_channel.send(f"{user.mention} you already have a Word Pot game running: {ch.mention}", allowed_mentions=discord.AllowedMentions.none())
            return ch
        else:
            casino_channels.pop((gid, uid), None)

    ch = await _make_private_solo_channel(invocation_channel, user)
    if not ch:
        return None

    # charge entry
    await change_balance(gid, uid, -1, announce_channel_id=ch.id)

    casino_games[_key(gid, ch.id, uid)] = {
        "answer": random.choice(ANSWERS), "guesses": [], "max": 3, "legend": {}, "origin_cid": invocation_channel.id, "staked": 1
    }
    casino_channels[(gid, uid)] = ch.id

    pot = await get_casino_pot(gid)
    board = render_board(casino_games[_key(gid, ch.id, uid)]["guesses"], total_rows=3)
    await ch.send(
        f"{user.mention} 🎰 **Word Pot** is live!\n"
        f"• Entry: **1 {EMO_SHEKEL()}** (already paid)\n"
        f"• Current Pot: **{pot} {EMO_SHEKEL()}** (resets to {CASINO_BASE_POT} on win)\n"
        f"• You have **3 tries** — solve within 3 to **win the pot**.\n"
        f"If you fail, your entry adds **+1** to the pot.",
        allowed_mentions=discord.AllowedMentions(users=[user])
    )
    await ch.send(board)
    return ch

async def casino_guess(channel: discord.TextChannel, user: discord.Member, word: str):
    gid, cid, uid = channel.guild.id, channel.id, user.id
    game = casino_games.get(_key(gid, cid, uid))
    if not game:
        await safe_send(channel, f"{user.mention} no Word Pot game here. Start with `/worldle_casino`.",
                        allowed_mentions=discord.AllowedMentions.none())
        return

    cleaned = "".join(ch for ch in word.lower().strip() if ch.isalpha())
    if len(cleaned) != 5:
        await safe_send(channel, "Guess must be **exactly 5 letters**.")
        return
    if not is_valid_guess(cleaned):
        await safe_send(channel, "That’s not in the Wordle dictionary (UK variants supported).")
        return
    if len(game["guesses"]) >= game["max"]:
        await safe_send(channel, "Out of tries! Start a new one with `/worldle_casino`.")
        return

    colors = score_guess(cleaned, game["answer"])
    game["guesses"].append({"word": cleaned, "colors": colors})
    update_legend(game["legend"], cleaned, colors)
    attempt = len(game["guesses"])

    board = render_board(game["guesses"], total_rows=3)
    await safe_send(channel, board)

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

        await safe_send(
            channel,
            f"🏆 {user.mention} solved **{ans}** on attempt **{attempt}** and **WON {pot} {EMO_SHEKEL()}**! "
            f"Pot resets to **{CASINO_BASE_POT}**. (Balance: {bal_new})"
        )

        emb = make_card(
            title="🎰 Word Pot — WIN",
            description=f"{user.mention} won **{pot} {EMO_SHEKEL()}** by solving **{ans}** on attempt **{attempt}**.",
            fields=[
                ("Board", board, False),                          # <-- no code block
                ("Next Pot", f"Resets to **{CASINO_BASE_POT}**", True),
            ],
            color=CARD_COLOR_SUCCESS,
        )
        await _announce_result(channel.guild, origin_cid, content="", embed=emb)

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

        definition_str = f"\n📖 Definition: {definition}" if definition else ""
        await safe_send(
            channel,
            f"❌ Out of tries. The word was **{ans}** — {quip}{definition_str}\n"
            f"The pot increases to **{new_pot} {EMO_SHEKEL()}**."
        )

        fields = [("Board", board, False), ("Pot", f"Now **{new_pot} {EMO_SHEKEL()}**", True)]
        if definition:
            fields.append(("Definition", definition, False))

        emb = make_card(
            title="🎰 Word Pot — Failed",
            description=f"{user.mention} failed **Word Pot** — the word was **{ans}**. {quip}",
            fields=fields,
            color=CARD_COLOR_FAIL,
        )
        await _announce_result(channel.guild, origin_cid, content="", embed=emb)

        try:
            await channel.delete(reason="Word Pot finished (fail)")
        except Exception:
            pass
        return

    # mid-game hint
    legend = legend_overview(game["legend"], game["guesses"])
    msg = f"Attempt **{attempt}/3** — solve within **3** to win the pot."
    if legend:
        msg += f"\n{legend}"
    await safe_send(channel, msg)




# ---------- HELP PAGER UI ----------
class HelpBook(discord.ui.View):
    def __init__(self, pages: list[discord.Embed], start_index: int = 0, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.pages = pages
        self.index = max(0, min(start_index, len(pages)-1))
        # Build select options from page titles
        self.jump_select.options = [
            discord.SelectOption(label=emb.title[:100] if emb.title else f"Page {i+1}", value=str(i))
            for i, emb in enumerate(self.pages)
        ]
        self._sync_buttons()

    def _sync_buttons(self):
        at_first = self.index <= 0
        at_last = self.index >= len(self.pages) - 1
        self.first_btn.disabled = at_first
        self.prev_btn.disabled = at_first
        self.next_btn.disabled = at_last
        self.last_btn.disabled = at_last

    async def _show(self, interaction: discord.Interaction):
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    @discord.ui.button(label="⏮ First", style=discord.ButtonStyle.secondary)
    async def first_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = 0
        await self._show(interaction)

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.index > 0:
            self.index -= 1
        await self._show(interaction)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.index < len(self.pages) - 1:
            self.index += 1
        await self._show(interaction)

    @discord.ui.button(label="Last ⏭", style=discord.ButtonStyle.secondary)
    async def last_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = len(self.pages) - 1
        await self._show(interaction)

    @discord.ui.select(placeholder="Jump to section…")
    async def jump_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        try:
            self.index = int(select.values[0])
        except Exception:
            pass
        await self._show(interaction)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger)
    async def close_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Keep the message public, just remove the controls
        await interaction.response.edit_message(view=None)

def build_help_pages(guild_name: str | None = None) -> list[discord.Embed]:
    g = guild_name or "this server"
    shek = EMO_SHEKEL()
    stone = EMO_STONE()
    chick = EMO_CHICKEN()
    badge = EMO_BADGE()
    sniper = EMO_SNIPER()
    bounty = EMO_BOUNTY()

    pages: list[discord.Embed] = []

    # 1) Overview
    emb = discord.Embed(title="Wordle World — Overview")
    emb.description = (
        "Play Wordle-style games, earn coins (**Shekels**), and unlock roles. "
        "Daily limits and resets are based on **00:00 UK time** (London).\n\n"
        "Flip through these pages for a quick tour and exact commands."
    )
    emb.add_field(name="Quick Start", value="• Type **`/immigrate`** to join.\n• Then type **`w`** to start a solo game.", inline=False)
    emb.add_field(name="Currencies & Items", value=f"• {shek} **Shekels** — used in shop and fees.\n• Items: {stone}, {chick}, {badge}, {sniper}.", inline=False)
    emb.set_footer(text="Pages: Overview, Join & Roles, Solo, Word Pot, Bounty, Duels, Economy, Stones, Streaks, Admin, Shortcuts")
    pages.append(emb)

    # 2) Join & Roles
    emb = discord.Embed(title="Join & Roles")
    emb.add_field(name="Join the game", value=f"• **/immigrate** — grants the **{WORLDLER_ROLE_NAME}** role and a small bonus.", inline=False)
    emb.add_field(name="Tier Roles", value="• Admins can bind balance thresholds to roles.\n• Your roles auto-update as your balance changes.", inline=False)
    emb.add_field(name="Helpful Admin Commands", value="• **/role_maketier**, **/role_addtier**, **/role_removetier**, **/role_sync**", inline=False)
    pages.append(emb)

    # 3) Solo Mode
    emb = discord.Embed(title="Solo Wordle (private room)")
    emb.description = (
        "Five tries to guess a 5-letter word. UK dictionary variants are allowed. "
        "You get **5 solo games per day**."
    )
    emb.add_field(name="Start", value="• **`w`** or **/worldle**", inline=False)
    emb.add_field(name="Guess", value="• **`g APPLE`** or **/worldle_guess word:APPLE**", inline=False)
    emb.add_field(
        name="Payouts",
        value=f"• Solve on 1st→5 {shek} · 2nd→4 · 3rd→3 · 4th→2 · 5th→1",
        inline=False,
    )
    emb.add_field(name="End Early", value="• **/worldle_end** (counts as a fail).", inline=False)
    pages.append(emb)

    # 4) Word Pot (Casino)
    emb = discord.Embed(title="Word Pot (Casino)")
    emb.description = (
        "A shared prize pool across the server.\n"
        "• Costs **1** shekel to play.\n"
        "• You have **3 tries**. If you solve within 3, you **win the whole pot**.\n"
        f"• Pot **resets to {CASINO_BASE_POT}** after a win."
    )
    emb.add_field(name="Start / Guess / End", value="• **/worldle_casino** to start.\n• Guess with **`g WORD`** (same as solo) or **/worldle_guess** in your room.\n• **/worldle_end** to end early (fail).", inline=False)
    emb.add_field(name="Announcements & Quips", value="• Wins/fails are announced publicly with spicy fail quips. 🎤", inline=False)
    pages.append(emb)

    # 5) Bounty
    emb = discord.Embed(title="Hourly Bounty")
    emb.description = (
        f"A server-wide race. When the prompt drops, **react with {bounty}** to arm it (needs 2 players), "
        "then guess in the bounty channel."
    )
    emb.add_field(name="Admin Setup", value="• **/worldle_bounty_setchannel** to choose the channel.", inline=False)
    emb.add_field(name="Manual Drop", value="• **/worldle_bounty_now** posts a prompt immediately.", inline=False)
    emb.add_field(name="Play", value="• **`bg WORD`** or **/worldle_bounty_guess word:WORD**", inline=False)
    emb.add_field(name="Reward", value=f"• First solver wins **{BOUNTY_PAYOUT} {shek}**.", inline=False)
    pages.append(emb)

    # 6) Duels
    emb = discord.Embed(title="Duels")
    emb.description = "Challenge a player. Stake goes into a pot; first to solve wins the lot."
    emb.add_field(name="Create", value="• **/worldle_challenge user:@Name amount:10**", inline=False)
    emb.add_field(name="Accept / Cancel", value="• **/worldle_accept id:**, **/worldle_cancel id:**", inline=False)
    emb.add_field(name="Guess", value="• **/worldle_duel_guess id:123 word:APPLE** or just **`g APPLE`** when it’s your turn.", inline=False)
    pages.append(emb)

    # 7) Economy & Shop
    emb = discord.Embed(title="Economy & Shop")
    emb.add_field(name="Daily", value=f"• **/pray** → +5 {shek} (once per day)\n• **/beg** → +5 {stone} (once per day)", inline=False)
    emb.add_field(
        name="Shop",
        value=(
            f"• **/shop**, **/buy**, **/sell**\n"
            f"• Items: {stone} **Stone** (49% drop chance), {chick} **Fried Chicken** (+1h protection), "
            f"{badge} **Bounty Hunter Badge** (pings), {sniper} **Sniper** (guess into others’ solo)."
        ),
        inline=False,
    )
    emb.add_field(name="Wallet / Inventory / Badges / LB", value="• **/balance**, **/inventory**, **/badges**, **/leaderboard**", inline=False)
    pages.append(emb)

    # 8) Stones & Protection
    emb = discord.Embed(title="Stones & Protection")
    emb.description = (
        f"Throw {stone} at players to make them drop shekels into the ground pot.\n"
        "• Each throw has a **49%** chance to hit.\n"
        "• If they’re protected, stones are wasted (no drop).\n"
        "• You can only stone the **same player up to 15 times per day**. You can still stone others."
    )
    emb.add_field(name="Commands", value=f"• **/stone user:@Name times:5**\n• **/collect** to pick up all ground shekels.\n• **/eat amount:1** to gain protection (1 hour each).", inline=False)
    pages.append(emb)

    # 9) Streaks
    emb = discord.Embed(title="Streaks (UK days)")
    emb.description = "Play at least one solo game per day to keep your streak alive."
    emb.add_field(name="See Streaks", value="• **/streaks** (server top)\n• **/mystreak** (yours)", inline=False)
    pages.append(emb)

    # 10) Admin / Setup
    emb = discord.Embed(title="Admin / Setup")
    emb.add_field(name="Solo Rooms Category", value="• **/worldle_set_category**", inline=False)
    emb.add_field(name="Announcements Channel", value="• **/worldle_set_announce**", inline=False)
    emb.add_field(name="Bounty Channel", value="• **/worldle_bounty_setchannel**", inline=False)
    emb.add_field(name="Resync Commands", value="• **/worldle_resync**", inline=False)
    emb.add_field(name="Set Balance", value=f"• **/set_balance user:@Name amount:123** ({shek})", inline=False)
    pages.append(emb)

    # 11) Text Shortcuts (recap)
    emb = discord.Embed(title="Text Shortcuts (Anywhere)")
    emb.add_field(name="Start Solo", value="• **`w`**", inline=False)
    emb.add_field(name="Guess", value="• **`g WORD`** (smart: solo, duel turn, or bounty if in the bounty channel)", inline=False)
    emb.add_field(name="Bounty Guess", value="• **`bg WORD`**", inline=False)
    pages.append(emb)

    return pages
