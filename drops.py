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
