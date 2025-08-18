# -------------------- Setup (category + announcements) --------------------
@tree.command(name="worldle_set_category", description="(Admin) Set THIS channel's category for solo Wordle rooms.")
@app_commands.default_permissions(administrator=True)
async def worldle_set_category(inter: discord.Interaction):
    if not inter.guild or not inter.channel:
        return await send_boxed(inter, "Solo Category", "Server only.", icon="🛠", ephemeral=True)
    cat = getattr(inter.channel, "category", None)
    if not isinstance(cat, discord.CategoryChannel):
        return await send_boxed(inter, "Solo Category", "This channel isn’t inside a category. Move it, then run again.", icon="🛠", ephemeral=True)
    await set_cfg(inter.guild.id, solo_category_id=cat.id)
    await send_boxed(inter, "Solo Category", f"Solo rooms will be created under **{cat.name}**.", icon="🛠")

@tree.command(name="worldle_set_announce", description="(Admin) Set THIS channel for solo Wordle result announcements.")
@app_commands.default_permissions(administrator=True)
async def worldle_set_announce(inter: discord.Interaction):
    if not inter.guild or not inter.channel:
        return await send_boxed(inter, "Announcements", "Server only.", icon="📣", ephemeral=True)
    await set_cfg(inter.guild.id, announcements_channel_id=inter.channel.id)
    await send_boxed(inter, "Announcements", f"All announcements will be posted in {inter.channel.mention}.", icon="📣")



# -------------------- Join / Help / Resync --------------------
@tree.command(name="immigrate", description=f"Join Wordle World: get the {WORLDLER_ROLE_NAME} role and a welcome bonus.")
async def immigrate(inter: discord.Interaction):
    if not inter.guild:
        return await inter.response.send_message("Run this in a server.", ephemeral=True)
    guild = inter.guild
    member = inter.user

    rid = await ensure_worldler_role(guild)
    if not rid:
        return await inter.response.send_message("I need **Manage Roles** to create the role. Ask an admin.", ephemeral=True)
    role = guild.get_role(rid)
    if role in (guild.get_member(member.id) or await guild.fetch_member(member.id)).roles:
        bal = await get_balance(guild.id, member.id)
        return await inter.response.send_message(f"You're already a **{WORLDLER_ROLE_NAME}**! Balance: **{bal}**.", ephemeral=True)

    try:
        await member.add_roles(role, reason="Wordle World immigration")
    except Exception as e:
        return await inter.response.send_message(f"Couldn't add the role. Do I have **Manage Roles** and is my role above **{WORLDLER_ROLE_NAME}**? ({e})", ephemeral=True)

    await change_balance(guild.id, member.id, START_BONUS, announce_channel_id=inter.channel_id)
    bal = await get_balance(guild.id, member.id)
    await inter.response.send_message(
        f"🌍 Welcome to **Wordle World** {member.mention}!\n"
        f"• Granted **{WORLDLER_ROLE_NAME}** role\n"
        f"• Welcome bonus: **+{START_BONUS} {EMO_SHEKEL()}** — Balance **{bal}**",
        allowed_mentions=discord.AllowedMentions.none()
    )

@tree.command(name="help", description="Interactive help: learn the game and commands.")
async def help_cmd(inter: discord.Interaction):
    pages = build_help_pages(getattr(inter.guild, "name", None))
    view = HelpBook(pages)
    # Public (non-ephemeral) by default — keep it visible to everyone
    await inter.response.send_message(embed=pages[0], view=view)


@tree.command(name="worldle_resync", description="(Admin) Fix duplicate commands / force-refresh here.")
@app_commands.default_permissions(administrator=True)
async def worldle_resync(inter: discord.Interaction):
    if not inter.guild:
        return await inter.response.send_message("Run in a server.", ephemeral=True)
    await inter.response.defer(ephemeral=True)
    tree.clear_commands(guild=inter.guild)
    await tree.sync(guild=inter.guild)
    tree.copy_global_to(guild=inter.guild)
    await tree.sync(guild=inter.guild)
    await tree.sync()
    await inter.followup.send("✅ Commands refreshed here. If any still don’t appear, close/reopen the slash picker.")


# -------------------- Admin emoji debug tools --------------------
@tree.command(name="ww_emoji_test", description="(Admin) Show how my named emojis resolve right now.")
@app_commands.default_permissions(administrator=True)
async def ww_emoji_test(inter: discord.Interaction):
    txt = (
        f"badge: {EMO_BADGE()}  (expects name: `{EMO_BADGE_NAME}`)\n"
        f"chicken: {EMO_CHICKEN()}  (expects name: `{EMO_CHICKEN_NAME}`)\n"
        f"sniper: {EMO_SNIPER()}  (expects name: `{EMO_SNIPER_NAME}`)\n"
        f"bounty: {EMO_BOUNTY()}  (expects name: `{EMO_BOUNTY_NAME}`)\n"
        f"shekel: {EMO_SHEKEL()}  (expects name: `{EMO_SHEKEL_NAME}`)\n"
        f"stone:  {EMO_STONE()}  (expects name: `{EMO_STONE_NAME}`)\n"
    )
    await inter.response.send_message(txt)

@tree.command(name="ww_refresh_tiles", description="(Admin) Re-scan tile emojis (wl_*) without restarting.")
@app_commands.default_permissions(administrator=True)
async def ww_refresh_tiles(inter: discord.Interaction):
    build_emoji_lookup()
    await inter.response.send_message("✅ Tile emoji cache rebuilt.")



class ShekelDropView(discord.ui.View):
    def __init__(self, guild_id: int, channel_id: int, amount: int = 1, timeout: float = 600):
        super().__init__(timeout=timeout)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.amount = int(max(1, amount))  # set before using
        self.claimed = False

        btn = discord.ui.Button(
            label=f"Collect {self.amount}",
            style=discord.ButtonStyle.success,
            emoji=EMO_SHEKEL(),
        )
        btn.callback = self._on_collect
        self.add_item(btn)
        self._btn = btn

    async def _on_collect(self, interaction: discord.Interaction):
        if not interaction.guild or interaction.guild.id != self.guild_id:
            return await interaction.response.send_message("Wrong server for this drop.", ephemeral=True)
        if self.claimed:
            return await interaction.response.send_message("Too late — already collected.", ephemeral=True)

        taken = await take_from_pot(self.guild_id, self.amount)
        if taken <= 0:
            self.claimed = True
            self._btn.disabled = True
            self._btn.style = discord.ButtonStyle.secondary
            self._btn.label = "Already taken"
            try:
                await interaction.response.edit_message(view=self)
            except Exception:
                pass
            return await interaction.followup.send("Someone scooped it, or `/collect` emptied the ground.", ephemeral=True)

        # Award and update the button
        await change_balance(self.guild_id, interaction.user.id, taken, announce_channel_id=self.channel_id)
        self.claimed = True
        self._btn.disabled = True
        self._btn.style = discord.ButtonStyle.secondary
        self._btn.label = f"Collected by {interaction.user.display_name}"
        try:
            await interaction.response.edit_message(view=self)
        except Exception:
            pass

        # PUBLIC announcement so others see who collected it
        s = "" if taken == 1 else "s"
        try:
            # Prefer the interaction channel; fall back to the stored channel id
            ch = interaction.channel or interaction.guild.get_channel(self.channel_id)
            await safe_send(
                ch,
                f"{EMO_SHEKEL()} {interaction.user.mention} collected **{taken} shekel{s}**.",
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )
        except Exception:
            # If sending publicly fails for any reason, at least tell the collector
            await interaction.followup.send(f"You collected **{taken} shekel{s}**.", ephemeral=True)



async def maybe_drop_shekel_on_message(msg: discord.Message):
    """
    Ambient drop:
      • At most one RNG roll per 20-minute slot *per guild* (DB-coordinated).
      • 10% chance to mint a bundle of **1–5** shekels when the slot is claimed.
      • Posts to the configured Drops channel, else the current channel.
    """
    if not msg.guild or msg.author.bot:
        return

    gid = msg.guild.id
    slot = _current_20m_slot()

    # Try to claim (guild, slot). If another process already claimed it, this INSERT
    # is ignored and rowcount will be 0 — meaning we've already rolled for this slot.
    cur = await bot.db.execute(
        "INSERT OR IGNORE INTO ambient_rolls(guild_id, slot) VALUES(?, ?)",
        (gid, slot),
    )
    await bot.db.commit()
    if getattr(cur, "rowcount", 0) == 0:
        return  # someone already rolled this 20-minute window

    # Only the claimer attempts RNG
    if random.random() >= SHEKEL_DROP_CHANCE:  # 10% default
        return

    # Amount: uniform 1..5
    amount = random.randint(SHEKEL_DROP_MIN, SHEKEL_DROP_MAX)

    # Mint into the ground pot
    await add_to_pot(gid, amount)

    # Post in configured Drops Channel, else current channel
    target = await _get_drops_channel(msg.guild) or msg.channel

    emb = make_panel(
        title="💰 Shekel Drop!",
        description=(
            f"A shiny {EMO_SHEKEL()} hit the floor — **{amount}**!\n"
            "Press **Collect** to grab this bundle, or use `/collect` to scoop **everything** on the ground."
        ),
        icon="🪙",
    )
    view = ShekelDropView(guild_id=gid, channel_id=target.id, amount=amount)

    await safe_send(
        target,
        embed=emb,
        view=view,
        allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
    )




async def take_from_pot(gid: int, amount: int) -> int:
    async with bot.db.execute("SELECT pot FROM ground WHERE guild_id=?", (gid,)) as cur:
        row = await cur.fetchone()
    pot = row[0] if row else 0
    take = min(max(0, int(amount)), pot)
    if take > 0:
        await bot.db.execute("UPDATE ground SET pot = pot - ? WHERE guild_id=?", (take, gid))
        await bot.db.commit()
    return take


async def _get_drops_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    cfg = await get_cfg(guild.id)
    ch_id = cfg.get("drops_channel_id")
    if ch_id:
        ch = guild.get_channel(ch_id)
        if isinstance(ch, discord.TextChannel) and ch.permissions_for(guild.me).send_messages:
            return ch
    return None

@tree.command(name="set_drops_channel", description="(Admin) Set THIS channel for shekel drop announcements.")
@app_commands.default_permissions(administrator=True)
async def set_drops_channel_cmd(inter: discord.Interaction):
    if not inter.guild or not inter.channel:
        return await send_boxed(inter, "Drops Channel", "Server only.", icon="🪙", ephemeral=True)
    await set_cfg(inter.guild.id, drops_channel_id=inter.channel.id)
    await send_boxed(inter, "Drops Channel", f"Shekel drops will be announced in {inter.channel.mention}.", icon="🪙")

