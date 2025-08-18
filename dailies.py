async def _build_dailies_embed(guild: discord.Guild, user: discord.Member) -> discord.Embed:
    """Boxed summary of daily actions + your current status (UK-time resets)."""
    gid, uid = guild.id, user.id
    today = uk_today_str()

    plays = await get_solo_plays_today(gid, uid, today)
    last_pray, last_beg = await _get_cd(gid, uid)

    left = max(0, 5 - int(plays or 0))
    prayed = "✅ done today" if last_pray == today else "🟢 ready"
    begged = "✅ done today" if last_beg == today else "🟢 ready"

    emb = make_panel(
        title="🗓️ Daily Actions (UK reset)",
        description=(
            "All daily limits reset at **00:00 UK time** (Europe/London).\n"
            "Use the **buttons** below."
        ),
        fields=[
            ("Solo Worldles", f"Play up to **5/day**.\nYou have **{left}** left today. Start with **🧩**.", True),
            ("Pray", f"+5 {EMO_SHEKEL()} once per day.\nStatus: **{prayed}**. Use **🛐**.", True),
            ("Beg", f"+5 {EMO_STONE()} once per day.\nStatus: **{begged}**. Use **🙇**.", True),
            ("Extras", "🎰 **Word Pot** (casino) — not a daily, but fun! Use **🎰**.", False),
        ],
        icon="🗓️"
    )
    emb.set_footer(text=f"Status shown for: {user.display_name}")
    return emb



class DailiesView(discord.ui.View):
    def __init__(self, guild_id: int, *, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.guild_id = guild_id

    async def _ensure_worldler(self, inter: discord.Interaction) -> bool:
        if not inter.guild:
            await inter.response.send_message("Server only.", ephemeral=True)
            return False
        if not await is_worldler(inter.guild, inter.user):
            # This one can stay ephemeral since it's an access warning
            await inter.response.send_message(
                f"You need the **{WORLDLER_ROLE_NAME}** role. Use `/immigrate` to join.",
                ephemeral=True
            )
            return False
        return True

    async def _refresh_panel(self, inter: discord.Interaction):
        """Rebuild & edit the /dailies message after an action."""
        try:
            emb = await _build_dailies_embed(inter.guild, inter.user)
            # inter.message is the panel message that contained the button
            if inter.message:
                await inter.message.edit(embed=emb, view=self)
        except Exception as e:
            log.warning(f"[dailies] refresh failed: {e}")

    @discord.ui.button(label="Start Solo (w)", style=discord.ButtonStyle.primary, emoji="🧩")
    async def btn_solo(self, inter: discord.Interaction, button: discord.ui.Button):
        if not await self._ensure_worldler(inter): 
            return
        await inter.response.defer(thinking=False)  # public
        ch = await solo_start(inter.channel, inter.user)
        if isinstance(ch, discord.TextChannel):
            await send_boxed(
                inter,
                "Solo Room Opened",
                f"{inter.user.mention} your room is {ch.mention}.",
                icon="🧩",
            )
        else:
            await send_boxed(inter, "Solo", "Couldn't start a solo right now.", icon="🧩")
        await self._refresh_panel(inter)

    @discord.ui.button(label="Pray (+5)", style=discord.ButtonStyle.success, emoji="🛐")
    async def btn_pray(self, inter: discord.Interaction, button: discord.ui.Button):
        if not await self._ensure_worldler(inter): 
            return
        gid, uid, cid = inter.guild.id, inter.user.id, inter.channel.id
        today = uk_today_str()
        last_pray, _ = await _get_cd(gid, uid)
        if last_pray == today:
            await send_boxed(inter, "Daily — Pray", "You already prayed today. Resets at **00:00 UK time**.", icon="🛐")
        else:
            await change_balance(gid, uid, 5, announce_channel_id=cid)
            await _set_cd(gid, uid, "last_pray", today)
            bal = await get_balance(gid, uid)
            await send_boxed(inter, "Daily — Pray", f"+5 {EMO_SHEKEL()}  · Balance **{bal}**", icon="🛐")
        await self._refresh_panel(inter)

    @discord.ui.button(label="Beg (+5 stones)", style=discord.ButtonStyle.secondary, emoji="🙇")
    async def btn_beg(self, inter: discord.Interaction, button: discord.ui.Button):
        if not await self._ensure_worldler(inter): 
            return
        gid, uid = inter.guild.id, inter.user.id
        today = uk_today_str()
        _, last_beg = await _get_cd(gid, uid)
        if last_beg == today:
            await send_boxed(inter, "Daily — Beg", "You already begged today. Resets at **00:00 UK time**.", icon="🙇")
        else:
            await change_stones(gid, uid, 5)
            await _set_cd(gid, uid, "last_beg", today)
            stones = await get_stones(gid, uid)
            await send_boxed(inter, "Daily — Beg", f"{EMO_STONE()} +5 Stones. You now have **{stones}**.", icon="🙇")
        await self._refresh_panel(inter)

    @discord.ui.button(label="Word Pot", style=discord.ButtonStyle.secondary, emoji="🎰")
    async def btn_wordpot(self, inter: discord.Interaction, button: discord.ui.Button):
        if not await self._ensure_worldler(inter): 
            return
        await inter.response.defer(thinking=False)  # public
        ch = await casino_start_word_pot(inter.channel, inter.user)
        if isinstance(ch, discord.TextChannel):
            await send_boxed(inter, "Word Pot Room Opened", f"{inter.user.mention} your room is {ch.mention}.", icon="🎰")
        else:
            await send_boxed(inter, "Word Pot", "Couldn't start Word Pot.", icon="🎰")
        await self._refresh_panel(inter)







async def dailies_reaction_listener(payload: discord.RawReactionActionEvent):
    """Independent reaction handler for /dailies panels only."""
    try:
        if payload.user_id == (bot.user.id if bot.user else 0):
            return
        if payload.message_id not in dailies_msg_ids:
            return

        guild = discord.utils.get(bot.guilds, id=payload.guild_id)
        if not guild:
            return
        try:
            member = guild.get_member(payload.user_id) or await guild.fetch_member(payload.user_id)
        except Exception:
            return
        if not member or member.bot or not await is_worldler(guild, member):
            return

        channel = guild.get_channel(payload.channel_id) if hasattr(payload, "channel_id") else None
        if not isinstance(channel, discord.TextChannel):
            # Fallback: try fetching via the message
            try:
                channel = await guild.fetch_channel(payload.channel_id)
            except Exception:
                return

        emoji_name = payload.emoji.name

        # React: 🧩 = Start Solo
        if emoji_name == "🧩":
            ch = await solo_start(channel, member)
            if isinstance(ch, discord.TextChannel):
                await safe_send(
                    channel,
                    f"🧩 {member.mention} your solo room is {ch.mention}.",
                    allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False)
                )

        # React: 🛐 = Pray
        elif emoji_name == "🛐":
            gid, uid = guild.id, member.id
            today = uk_today_str()
            last_pray, _ = await _get_cd(gid, uid)
            if last_pray == today:
                await safe_send(channel, f"🛐 {member.mention} you already prayed today.", 
                                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
            else:
                await change_balance(gid, uid, 5, announce_channel_id=channel.id)
                await _set_cd(gid, uid, "last_pray", today)
                bal = await get_balance(gid, uid)
                await safe_send(channel, f"🛐 {member.mention} +5 {EMO_SHEKEL()} — Balance **{bal}**",
                                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))

        # React: 🙇 = Beg
        elif emoji_name == "🙇":
            gid, uid = guild.id, member.id
            today = uk_today_str()
            _, last_beg = await _get_cd(gid, uid)
            if last_beg == today:
                await safe_send(channel, f"🙇 {member.mention} you already begged today.",
                                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
            else:
                await change_stones(gid, uid, 5)
                await _set_cd(gid, uid, "last_beg", today)
                stones = await get_stones(gid, uid)
                await safe_send(channel, f"🙇 {member.mention} {EMO_STONE()} +5 Stones — You now have **{stones}**.",
                                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))

        # React: 🎰 = Word Pot
        elif emoji_name == "🎰":
            ch = await casino_start_word_pot(channel, member)
            if isinstance(ch, discord.TextChannel):
                await safe_send(
                    channel,
                    f"🎰 {member.mention} Word Pot room: {ch.mention}",
                    allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False)
                )

        # Tidy up: remove the user’s reaction so others can easily use it too
        try:
            msg = await channel.fetch_message(payload.message_id)
            await msg.remove_reaction(payload.emoji, member)
        except Exception:
            pass

    except Exception as e:
        log.warning(f"[dailies] reaction handler error: {e}")



async def dailies_raw_reaction_add(payload: discord.RawReactionActionEvent):
    """Independent reaction handler for /dailies panels only (and refresh the panel)."""
    try:
        # Only handle reactions on dailies panels we sent
        if payload.message_id not in dailies_msg_ids:
            return

        # Ignore the bot's own reactions
        if bot.user and payload.user_id == bot.user.id:
            return

        guild = discord.utils.get(bot.guilds, id=payload.guild_id)
        if not guild:
            return

        try:
            member = guild.get_member(payload.user_id) or await guild.fetch_member(payload.user_id)
        except Exception:
            member = None
        if not member or member.bot or not await is_worldler(guild, member):
            return

        # Resolve channel to reply in
        channel = guild.get_channel(getattr(payload, "channel_id", 0))
        if not isinstance(channel, discord.TextChannel):
            try:
                channel = await guild.fetch_channel(getattr(payload, "channel_id", 0))
            except Exception:
                return

        emoji_name = payload.emoji.name

        # 🧩 Start Solo
        if emoji_name == "🧩":
            ch = await solo_start(channel, member)
            if isinstance(ch, discord.TextChannel):
                await safe_send(
                    channel,
                    f"🧩 {member.mention} your solo room is {ch.mention}.",
                    allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False)
                )

        # 🛐 Pray
        elif emoji_name == "🛐":
            gid, uid = guild.id, member.id
            today = uk_today_str()
            last_pray, _ = await _get_cd(gid, uid)
            if last_pray == today:
                await safe_send(channel, f"🛐 {member.mention} you already prayed today (resets 00:00 UK).",
                                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
            else:
                await change_balance(gid, uid, 5, announce_channel_id=channel.id)
                await _set_cd(gid, uid, "last_pray", today)
                bal = await get_balance(gid, uid)
                await safe_send(channel, f"🛐 {member.mention} +5 {EMO_SHEKEL()} — Balance **{bal}**",
                                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))

        # 🙇 Beg
        elif emoji_name == "🙇":
            gid, uid = guild.id, member.id
            today = uk_today_str()
            _, last_beg = await _get_cd(gid, uid)
            if last_beg == today:
                await safe_send(channel, f"🙇 {member.mention} you already begged today (resets 00:00 UK).",
                                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
            else:
                await change_stones(gid, uid, 5)
                await _set_cd(gid, uid, "last_beg", today)
                stones = await get_stones(gid, uid)
                await safe_send(channel, f"🙇 {member.mention} {EMO_STONE()} +5 Stones — You now have **{stones}**.",
                                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))

        # 🎰 Word Pot (casino)
        elif emoji_name == "🎰":
            ch = await casino_start_word_pot(channel, member)
            if isinstance(ch, discord.TextChannel):
                await safe_send(
                    channel,
                    f"🎰 {member.mention} Word Pot room: {ch.mention}",
                    allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False)
                )

        # Tidy up: remove the user's reaction so others can easily click too
        try:
            msg = await channel.fetch_message(payload.message_id)
            await msg.remove_reaction(payload.emoji, member)
        except Exception:
            pass

        # 🔄 Refresh the panel embed (leave existing buttons/view intact)
        try:
            msg = await channel.fetch_message(payload.message_id)
            new_emb = await _build_dailies_embed(guild, member)
            await msg.edit(embed=new_emb)
        except Exception:
            pass

    except Exception as e:
        log.warning(f"[dailies] reaction handler error: {e}")


      

@tree.command(name="dailies", description="See and click your daily actions (UK reset).")
async def dailies_cmd(inter: discord.Interaction):
    if not inter.guild:
        return await inter.response.send_message("Run this in a server.", ephemeral=True)
    if not await guard_worldler_inter(inter):
        return

    emb = await _build_dailies_embed(inter.guild, inter.user)
    view = DailiesView(inter.guild.id)

    # Send panel publicly (not ephemeral)
    await inter.response.send_message(embed=emb, view=view)

    # (Optional) keep the reaction shortcuts you already had
    try:
        msg = await inter.original_response()
        dailies_msg_ids.add(msg.id)
        for emo in ("🧩", "🛐", "🙇", "🎰"):
            try:
                await msg.add_reaction(emo)
            except Exception:
                pass
    except Exception:
        pass
