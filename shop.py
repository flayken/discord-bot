# -------------------- Economy / items --------------------
PRICE_STONE = 1
PRICE_BADGE = 5
PRICE_CHICK = 2
PRICE_SNIPER = 100
SNIPER_SNIPE_COST = 1  # price per snipe shot

# NEW: Tier 3 Dungeon Ticket is purchasable (T2/T1 are loot-only)
SHOP_ITEMS = {
    "stone": {
        "label": f"{EMO_STONE()} Stone",
        "price": PRICE_STONE,
        "desc": "Throw with /stone. 49% drop chance per stone (bulk supported).",
    },
    "badge": {
        "label": f"{EMO_BADGE()} Bounty Hunter Badge",
        "price": PRICE_BADGE,
        "desc": "Grants the Bounty Hunter role. Bounties ping that role.",
    },
    "chicken": {
        "label": f"{EMO_CHICKEN()} Fried Chicken",
        "price": PRICE_CHICK,
        "desc": "Use /eat to gain 1h immunity from stones.",
    },
    "sniper": {
        "label": f"{EMO_SNIPER()} Sniper",
        "price": PRICE_SNIPER,
        "desc": f"Lets you `/snipe` other players' solo Wordle (costs {SNIPER_SNIPE_COST} shekel per shot). One-time purchase.",
    },
    # NEW ITEM
    "ticket_t3": {
        "label": f"{EMO_DUNGEON()} Dungeon Ticket (Tier 3)",
        "price": 5,
        "desc": "Opens a Tier 3 Worldle Dungeon. Use `/worldle_dungeon tier:Tier 3`.",
    },
}

# Controls shop item order & /buy choices
SHOP_ORDER = ["stone", "badge", "chicken", "sniper", "ticket_t3"]


@tree.command(name="shop", description="Show the shop.")
async def shop(inter: discord.Interaction):
    if not await guard_worldler_inter(inter):
        return
    lines = ["🛒 **Shop** — buy with `/buy` or sell with `/sell`", ""]
    for key in SHOP_ORDER:
        item = SHOP_ITEMS[key]
        price = item["price"]
        lines.append(f"• **{item['label']}** — {price} {EMO_SHEKEL()}{'' if price==1 else 's'}\n  _{item['desc']}_")
    lines.append("\nExamples: `/buy item: Stone amount: 3`, `/sell item: Stone amount: 2`")
    await send_boxed(inter, "Shop", "\n".join(lines), icon="🛒")



@tree.command(name="buy", description="Buy from the shop.")
@app_commands.describe(item="Item", amount="How many")
@app_commands.choices(item=[app_commands.Choice(name=SHOP_ITEMS[k]["label"], value=k) for k in SHOP_ORDER])
async def buy(inter: discord.Interaction, item: app_commands.Choice[str], amount: int):
    if not await guard_worldler_inter(inter): 
        return
    if not inter.guild: 
        return await inter.response.send_message("Server only.", ephemeral=True)
    if amount <= 0: 
        return await inter.response.send_message("Amount must be positive.", ephemeral=True)

    key, gid, uid, cid = item.value, inter.guild.id, inter.user.id, inter.channel_id
    price = SHOP_ITEMS[key]["price"]
    cost = price * amount
    bal = await get_balance(gid, uid)
    if bal < cost:
        return await inter.response.send_message(
            f"Not enough shekels. Cost **{cost} {EMO_SHEKEL()}**, you have **{bal}**.", ephemeral=True
        )

    if key == "stone":
        await change_balance(gid, uid, -cost, announce_channel_id=cid)
        await change_stones(gid, uid, amount)
        stones = await get_stones(gid, uid)
        return await inter.response.send_message(
            f"{EMO_STONE()} Bought **{amount} Stone(s)** (−{cost}). Stones: **{stones}**. Balance: **{await get_balance(gid, uid)}**"
        )

    if key == "badge":
        if await get_badge(gid, uid) >= 1:
            return await inter.response.send_message("You already own the badge.", ephemeral=True)
        await change_balance(gid, uid, -cost, announce_channel_id=cid)
        await set_badge(gid, uid, 1)
        rid = await ensure_bounty_role(inter.guild)
        try:
            m = inter.guild.get_member(uid) or await inter.guild.fetch_member(uid)
            if rid and bot_can_manage_role(inter.guild, inter.guild.get_role(rid)):
                await m.add_roles(inter.guild.get_role(rid), reason="Bought Bounty Hunter Badge")
        except Exception as e:
            log.warning(f"add bounty role failed: {e}")
        return await inter.response.send_message(
            f"{EMO_BADGE()} You bought the **Bounty Hunter Badge** (−{cost}). You now receive bounty pings."
        )

    if key == "chicken":
        await change_balance(gid, uid, -cost, announce_channel_id=cid)
        await change_chickens(gid, uid, amount)
        return await inter.response.send_message(
            f"{EMO_CHICKEN()} Bought **{amount} Fried Chicken** (−{cost}). You have **{await get_chickens(gid, uid)}**."
        )

    if key == "sniper":
        if await get_sniper(gid, uid) >= 1:
            return await inter.response.send_message("You already own the Sniper.", ephemeral=True)
        await change_balance(gid, uid, -cost, announce_channel_id=cid)
        await set_sniper(gid, uid, 1)
        return await inter.response.send_message(
            f"{EMO_SNIPER()} You bought the **Sniper** (−{cost}). You can now use `/snipe` (costs {SNIPER_SNIPE_COST} per shot)."
        )

    if key == "ticket_t3":
        # Dungeon Ticket (Tier 3) — purchasable
        await change_balance(gid, uid, -cost, announce_channel_id=cid)
        await change_dungeon_tickets_t3(gid, uid, amount)
        count = await get_dungeon_tickets_t3(gid, uid)
        return await inter.response.send_message(
            f"{EMO_DUNGEON()} Bought **{amount} Tier-3 Dungeon Ticket(s)** (−{cost}). You now have **{count}**."
        )

    await inter.response.send_message("This item isn't wired yet.", ephemeral=True)


@tree.command(name="sell", description="Sell items back to the shop for the same price.")
@app_commands.describe(item="Item", amount="How many")
@app_commands.choices(item=[
    app_commands.Choice(name="Stone", value="stone"),
    app_commands.Choice(name="Bounty Hunter Badge", value="badge"),
    app_commands.Choice(name="Fried Chicken", value="chicken"),
    app_commands.Choice(name="Sniper", value="sniper"),
    app_commands.Choice(name="Dungeon Ticket (Tier 3)", value="ticket_t3"),  # NEW
])
async def sell(inter: discord.Interaction, item: app_commands.Choice[str], amount: int=1):
    if not await guard_worldler_inter(inter): 
        return
    if not inter.guild: 
        return await inter.response.send_message("Server only.", ephemeral=True)
    if amount <= 0: 
        return await inter.response.send_message("Amount must be positive.", ephemeral=True)
    key, gid, uid, cid = item.value, inter.guild.id, inter.user.id, inter.channel_id

    if key == "stone":
        have = await get_stones(gid, uid)
        if have < amount: 
            return await inter.response.send_message("You don't have that many stones.", ephemeral=True)
        await change_stones(gid, uid, -amount)
        await change_balance(gid, uid, PRICE_STONE * amount, announce_channel_id=cid)
        return await inter.response.send_message(
            f"Sold **{amount}** {EMO_STONE()} for **{PRICE_STONE*amount} {EMO_SHEKEL()}**."
        )

    if key == "badge":
        have = await get_badge(gid, uid)
        if have < 1: 
            return await inter.response.send_message("You don't own the badge.", ephemeral=True)
        await set_badge(gid, uid, 0)
        await change_balance(gid, uid, PRICE_BADGE, announce_channel_id=cid)
        rid = (await get_cfg(gid))["bounty_role_id"]
        try:
            m = inter.guild.get_member(uid) or await inter.guild.fetch_member(uid)
            r = inter.guild.get_role(rid) if rid else None
            if r and bot_can_manage_role(inter.guild, r):
                await m.remove_roles(r, reason="Sold Bounty Hunter Badge")
        except Exception as e:
            log.warning(f"remove bounty role failed: {e}")
        return await inter.response.send_message(
            f"Sold **Bounty Hunter Badge** for **{PRICE_BADGE} {EMO_SHEKEL()}** and lost the role."
        )

    if key == "chicken":
        have = await get_chickens(gid, uid)
        if have < amount: 
            return await inter.response.send_message("You don't have that many fried chicken.", ephemeral=True)
        await change_chickens(gid, uid, -amount)
        await change_balance(gid, uid, PRICE_CHICK * amount, announce_channel_id=cid)
        return await inter.response.send_message(
            f"Sold **{amount}** {EMO_CHICKEN()} for **{PRICE_CHICK*amount} {EMO_SHEKEL()}**."
        )

    if key == "sniper":
        have = await get_sniper(gid, uid)
        if have < 1:
            return await inter.response.send_message("You don't own the Sniper.", ephemeral=True)
        if amount != 1:
            return await inter.response.send_message("You can only sell one Sniper.", ephemeral=True)
        await set_sniper(gid, uid, 0)
        await change_balance(gid, uid, PRICE_SNIPER, announce_channel_id=cid)
        return await inter.response.send_message(
            f"Sold **Sniper** for **{PRICE_SNIPER} {EMO_SHEKEL()}**. You no longer have access to `/snipe`."
        )

    if key == "ticket_t3":
        have = await get_dungeon_tickets_t3(gid, uid)
        if have < amount:
            return await inter.response.send_message("You don't have that many Tier-3 tickets.", ephemeral=True)
        await change_dungeon_tickets_t3(gid, uid, -amount)
        refund = 5 * amount
        await change_balance(gid, uid, refund, announce_channel_id=cid)
        return await inter.response.send_message(
            f"Sold **{amount}** {EMO_DUNGEON()} Tier-3 Dungeon Ticket(s) for **{refund} {EMO_SHEKEL()}**."
        )

    await inter.response.send_message("Can't sell that.", ephemeral=True)


@tree.command(name="eat", description="Eat a Fried Chicken to gain 1 hour stone immunity.")
@app_commands.describe(amount="How many to eat (each adds 1 hour)")
async def eat(inter: discord.Interaction, amount: int=1):
    if not await guard_worldler_inter(inter): return
    if amount <= 0: return await inter.response.send_message("Amount must be positive.", ephemeral=True)
    gid, uid = inter.guild.id, inter.user.id
    have = await get_chickens(gid, uid)
    if have < amount: return await inter.response.send_message("You don't have that many fried chicken.", ephemeral=True)
    await change_chickens(gid, uid, -amount)
    now = gmt_now_s()
    current = await get_protection_until(gid, uid)
    base = current if current > now else now
    new_until = base + 3600 * amount
    await set_protection_until(gid, uid, new_until)
    mins = (new_until - now) // 60
    await inter.response.send_message(f"{EMO_CHICKEN()} You are protected from stones for **~{mins} minutes**.")

@tree.command(name="inventory", description="See your items.")
async def inventory(inter: discord.Interaction):
    if not await guard_worldler_inter(inter):
        return
    gid, uid = inter.guild.id, inter.user.id
    stones = await get_stones(gid, uid)
    chickens = await get_chickens(gid, uid)
    prot = await get_protection_until(gid, uid)
    sniper = await get_sniper(gid, uid)
    t1 = await get_dungeon_tickets_t1(gid, uid)
    t2 = await get_dungeon_tickets_t2(gid, uid)
    t3 = await get_dungeon_tickets_t3(gid, uid)

    left = max(0, prot - gmt_now_s())
    prot_txt = f" · 🛡️ {left//60}m left" if left>0 else ""
    sniper_owned = "Yes" if sniper else "No"

    body = "\n".join([
        f"• {EMO_STONE()} Stones: **{stones}**",
        f"• {EMO_CHICKEN()} Fried Chicken: **{chickens}**{prot_txt}",
        f"• {EMO_SNIPER()} Sniper: **{sniper_owned}**",
        f"• {EMO_DUNGEON()} Tickets — T1: **{t1}**, T2: **{t2}**, T3: **{t3}**",
    ])
    await send_boxed(inter, "Inventory", body, icon="🎒")



@tree.command(name="badges", description="Show your badges.")
async def badges_cmd(inter: discord.Interaction):
    if not await guard_worldler_inter(inter): return
    gid, uid = inter.guild.id, inter.user.id
    badges = []
    if await get_badge(gid, uid) >= 1:
        badges.append(f"{EMO_BADGE()} **Bounty Hunter Badge** — receive bounty pings")
    body = "You don't have any badges yet." if not badges else "• " + "\n• ".join(badges)
    await send_boxed(inter, "Badges", body, icon="🏅")


@tree.command(name="balance", description="See your shekels.")
async def balance_cmd(inter: discord.Interaction):
    if not await guard_worldler_inter(inter): return
    bal = await get_balance(inter.guild.id, inter.user.id)
    await send_boxed(inter, "Balance", f"**{bal} {EMO_SHEKEL()}**", icon="💰")


@tree.command(name="pray", description="Receive 5 shekels (once per UK day).")
async def pray(inter: discord.Interaction):
    if not await guard_worldler_inter(inter): return
    gid, uid, cid = inter.guild.id, inter.user.id, inter.channel_id
    today = uk_today_str()
    last_pray, _ = await _get_cd(gid, uid)
    if last_pray == today:
        return await send_boxed(inter, "Daily — Pray", "You already prayed today. Resets at **00:00 UK time**.", icon="🛐", ephemeral=True)
    await change_balance(gid, uid, 5, announce_channel_id=cid)
    await _set_cd(gid, uid, "last_pray", today)
    await send_boxed(inter, "Daily — Pray", f"+5 {EMO_SHEKEL()}  · Balance **{await get_balance(gid, uid)}**", icon="🛐")

@tree.command(name="beg", description="Beg for 5 stones (once per UK day).")
async def beg(inter: discord.Interaction):
    if not await guard_worldler_inter(inter): return
    gid, uid = inter.guild.id, inter.user.id
    today = uk_today_str()
    _, last_beg = await _get_cd(gid, uid)
    if last_beg == today:
        return await send_boxed(inter, "Daily — Beg", "You already begged today. Resets at **00:00 UK time**.", icon="🙇", ephemeral=True)
    await change_stones(gid, uid, 5)
    await _set_cd(gid, uid, "last_beg", today)
    stones = await get_stones(gid, uid)
    await send_boxed(inter, "Daily — Beg", f"{EMO_STONE()} +5 Stones. You now have **{stones}**.", icon="🙇")


@tree.command(name="stone", description="Throw stones at someone (bulk). 49% drop chance per stone. Max 15 per target per UK day.")
@app_commands.describe(user="Target", times="How many stones to throw (you must own them)")
async def stone_cmd(inter: discord.Interaction, user: discord.Member, times: int=1):
    if not await guard_worldler_inter(inter): return
    if user.bot:
        return await inter.response.send_message("Let's not stone bots.", ephemeral=True)
    if times <= 0:
        return await inter.response.send_message("Times must be positive.", ephemeral=True)

    await inter.response.defer(thinking=False)

    gid, uid, cid = inter.guild.id, inter.user.id, inter.channel_id

    have = await get_stones(gid, uid)
    if have < 1:
        return await inter.followup.send("You don't have any stones. Buy more with `/buy`.")

    today = uk_today_str()
    used_today = await get_stone_count_today(gid, uid, user.id, today)
    remaining_cap = max(0, 15 - used_today)
    if remaining_cap <= 0:
        return await inter.followup.send(f"🛑 You've reached your daily limit of **15** stones against {user.mention}. Resets at **00:00 UK time**.")

    # Only allow up to remaining cap and what they own
    allowed = min(times, remaining_cap, have)
    if allowed < times:
        await inter.followup.send(f"⚠️ You can only throw **{allowed}** more at {user.mention} today (cap 15 per day). Proceeding with **{allowed}**.")
    await change_stones(gid, uid, -allowed)

    # Stats (attempts)
    await inc_stat(gid, uid, "stones_thrown", allowed)
    await inc_stat(gid, user.id, "stoned_received", allowed)

    target_prot = await get_protection_until(gid, user.id)
    now = gmt_now_s()
    protected = target_prot > now

    # Count attempts regardless of protection
    await inc_stone_count_today(gid, uid, user.id, today, allowed)

    if protected:
        left = (target_prot - now)//60
        stones_left = await get_stones(gid, uid)
        return await inter.followup.send(
            f"🛡️ {user.mention} is protected from stones for about **~{left}m**. "
            f"You used **{allowed}** {EMO_STONE()} (left: **{stones_left}**)."
        )

    hits = sum(1 for _ in range(allowed) if random.random() < 0.49)
    victim_bal = await get_balance(gid, user.id)
    drops = min(hits, max(0, victim_bal))

    if drops > 0:
        # Take from victim, add to ground pot
        await change_balance(gid, user.id, -drops, announce_channel_id=cid)
        await add_to_pot(gid, drops)

    stones_left = await get_stones(gid, uid)

    if drops:
        # Plain text status (like before)…
        await inter.followup.send(
            f"💥 {inter.user.mention} stoned {user.mention} with **{allowed}** {EMO_STONE()} — **{drops}** {EMO_SHEKEL()} dropped. Use `/collect`."
        )

        # …plus a boxed message with a one-shot Collect button for THIS bundle.
        # If you'd rather send to the drops channel, replace `target_ch = inter.channel`
        # with: `target_ch = await _get_drops_channel(inter.guild) or inter.channel`
        target_ch = inter.channel
        emb = make_panel(
            title="💥 Shekels Dropped!",
            description=(
                f"{inter.user.mention} hit {user.mention}.\n"
                f"**{drops} {EMO_SHEKEL()}** fell to the ground — press **Collect** to grab this bundle, "
                f"or use `/collect` to scoop **everything** on the ground."
            ),
            icon="🪙"
        )
        view = ShekelDropView(guild_id=gid, channel_id=target_ch.id, amount=drops)
        await safe_send(
            target_ch,
            embed=emb,
            view=view,
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False)
        )
    else:
        await inter.followup.send(
            f"🪨 {inter.user.mention} stoned {user.mention} with **{allowed}** {EMO_STONE()}… no shekels dropped. (You have **{stones_left}** left.)"
        )


@tree.command(name="collect", description="Pick up ALL shekels from the ground.")
async def collect(inter: discord.Interaction):
    if not await guard_worldler_inter(inter): return
    gid, uid, cid = inter.guild.id, inter.user.id, inter.channel_id
    amt = await pop_all_from_pot(gid)
    if amt <= 0: return await inter.response.send_message("Nothing on the ground right now.")
    await change_balance(gid, uid, amt, announce_channel_id=cid)
    bal = await get_balance(gid, uid)
    s = "" if amt == 1 else "s"
    await inter.response.send_message(f"{EMO_SHEKEL()} {inter.user.mention} collected **{amt} shekel{s}**. Balance: **{bal}**")

@tree.command(name="leaderboard", description="Leaderboards you can flip through.")
async def leaderboard(inter: discord.Interaction):
    if not await guard_worldler_inter(inter): 
        return

    # ACK immediately to avoid "Unknown interaction" if DB/user fetches take >3s
    await inter.response.defer(thinking=False)

    guild = inter.guild

    async def _top_balances_page():
        gid = guild.id
        async with bot.db.execute(
            "SELECT user_id,balance FROM wallet WHERE guild_id=? ORDER BY balance DESC LIMIT 10",
            (gid,)
        ) as cur:
            rows = await cur.fetchall()

        emb = discord.Embed(title="🏆 Leaderboard — Most Shekels")
        if not rows:
            emb.description = "Nobody has any shekels yet."
            return emb

        # Gather tiers for suffix
        async with bot.db.execute(
            "SELECT role_id,min_balance FROM role_tier WHERE guild_id=? ORDER BY min_balance ASC",
            (gid,)
        ) as cur:
            tier_rows = await cur.fetchall()
        tiers = [(guild.get_role(rid), min_bal) for (rid, min_bal) in tier_rows if guild.get_role(rid)]

        lines=[]
        you_in_list = False
        for i,(uid,bal) in enumerate(rows,1):
            try:
                member = guild.get_member(uid) or await guild.fetch_member(uid)
                name = member.display_name
            except Exception:
                name = f"User {uid}"
            tier_name = ""
            for role, min_bal in tiers:
                if bal >= min_bal:
                    tier_name = role.name
            tier_suffix = f" ({tier_name})" if tier_name else ""
            marker = " ← you" if uid == inter.user.id else ""
            if marker: 
                you_in_list = True
            lines.append(f"{i}. **{name}** — {bal} {EMO_SHEKEL()}{tier_suffix}{marker}")

        if not you_in_list:
            async with bot.db.execute(
                "SELECT balance FROM wallet WHERE guild_id=? AND user_id=?", 
                (gid, inter.user.id)
            ) as cur:
                me = await cur.fetchone()
            if me:
                bal = me[0]
                tier_name = ""
                for role, min_bal in tiers:
                    if bal >= min_bal:
                        tier_name = role.name
                tier_suffix = f" ({tier_name})" if tier_name else ""
                lines.append(f"— **{inter.user.display_name}** — {bal} {EMO_SHEKEL()}{tier_suffix} ← you")

        emb.description = "\n".join(lines)
        return emb

    async def _make_stat_page(title: str, field: str, icon: str = "", note: str = ""):
        rows = await get_top_stats(guild.id, field)
        emb = discord.Embed(title=title)
        if note:
            emb.set_footer(text=note)
        if not rows:
            emb.description = "No data yet."
            return emb

        icon_sfx = f" {icon}" if icon else ""

        lines=[]
        you_in_list = False
        for i,(uid,val) in enumerate(rows,1):
            try:
                member = guild.get_member(uid) or await guild.fetch_member(uid)
                name = member.display_name
            except Exception:
                name = f"User {uid}"
            marker = " ← you" if uid == inter.user.id else ""
            if marker: 
                you_in_list = True
            lines.append(f"{i}. **{name}** — {val}{icon_sfx}{marker}")

        my_val = await get_my_stat(guild.id, inter.user.id, field)
        if not you_in_list and my_val > 0:
            lines.append(f"— **{inter.user.display_name}** — {my_val}{icon_sfx} ← you")

        emb.description = "\n".join(lines)
        return emb

    pages = [
        await _top_balances_page(),
        await _make_stat_page("🎯 Leaderboard — Most Bounties Won", "bounties_won", EMO_BOUNTY()),
        await _make_stat_page("🪨 Leaderboard — Most Stones Thrown", "stones_thrown", EMO_STONE()),
        await _make_stat_page("💥 Leaderboard — Most Stoned (Received)", "stoned_received", "💥",
                              note="Counts attempts against you, even if you were protected."),
        await _make_stat_page("💀 Leaderboard — Most Failed Solo Worldles", "solo_fails", "💀",
                              note="Only counts solo Worldles (including ending early)."),
        await _make_stat_page("🥷 Leaderboard — Most Snipes", "snipes", EMO_SNIPER()),
        await _make_stat_page("🥶 Leaderboard — Most Sniped", "sniped", "🥶",
                      note="Times your solo was finished by a sniper."),

    ]

    view = HelpBook(pages)
    # Use followup after defer
    await inter.followup.send(embed=pages[0], view=view)




# NEW: streak leaderboard
@tree.command(name="streaks", description="Show current solo play streaks (UK days).")
async def streaks_cmd(inter: discord.Interaction):
    if not await guard_worldler_inter(inter): return
    gid = inter.guild.id
    async with bot.db.execute("""
        SELECT user_id,cur,best
        FROM solo_streak
        WHERE guild_id=?
        ORDER BY cur DESC, best DESC
        LIMIT 10
    """, (gid,)) as cur:
        top = await cur.fetchall()

    lines = []
    if not top:
        my_last, my_cur, my_best = await _get_streak(gid, inter.user.id)
        if my_cur > 0:
            lines.append(f"1. **{inter.user.display_name}** — {my_cur} (best {my_best}) ← you")
        else:
            lines.append("No streaks yet. Play a solo Wordle to start your streak!")
    else:
        you_in_list = False
        for i, (uid, cur_s, best_s) in enumerate(top, 1):
            try:
                member = inter.guild.get_member(uid) or await inter.guild.fetch_member(uid)
                name = member.display_name
            except Exception:
                name = f"User {uid}"
            marker = " ← you" if uid == inter.user.id else ""
            if marker: you_in_list = True
            lines.append(f"{i}. **{name}** — {cur_s} (best {best_s}){marker}")
        if not you_in_list:
            my_last, my_cur, my_best = await _get_streak(gid, inter.user.id)
            if my_cur > 0:
                lines.append(f"— **{inter.user.display_name}** — {my_cur} (best {my_best}) ← you")

    await send_boxed(inter, "Streaks (UK days)", "\n".join(lines), icon="🔥")

@tree.command(name="mystreak", description="Show your solo streak (UK time).")
async def my_streak_cmd(inter: discord.Interaction):
    if not await guard_worldler_inter(inter): return
    gid, uid = inter.guild.id, inter.user.id
    last_date, cur_s, best_s = await _get_streak(gid, uid)
    if cur_s <= 0:
        return await send_boxed(inter, "Your Streak", "You don't have a streak yet. Start a solo Wordle to begin!", icon="🔥", ephemeral=True)
    await send_boxed(inter, "Your Streak", f"**{cur_s}** (best **{best_s}**) — last counted day: {last_date or '—'} (UK time).", icon="🔥")



@tree.command(name="set_balance", description="(Admin) Set a user's balance exactly.")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(user="Member", amount="New balance")
async def set_balance(inter: discord.Interaction, user: discord.Member, amount: int):
    if not inter.guild: return await inter.response.send_message("Server only.", ephemeral=True)
    if amount < 0: return await inter.response.send_message("Amount must be ≥ 0.", ephemeral=True)
    gid, cid = inter.guild.id, inter.channel_id
    current = await get_balance(gid, user.id)
    await change_balance(gid, user.id, amount - current, announce_channel_id=cid)
    await inter.response.send_message(f"✅ Set {user.mention}'s balance to **{amount} {EMO_SHEKEL()}**.")

@tree.command(name="snipe", description="Snipe another player's active Worldle (costs 1 shekel per shot).")
@app_commands.describe(target="Player you're sniping", word="Your 5-letter guess")
async def snipe_cmd(inter: discord.Interaction, target: discord.Member, word: str):
    # Check permissions/role first (don't defer yet so guard can reply ephemerally if needed)
    if not await guard_worldler_inter(inter):
        return
    if target.bot or target.id == inter.user.id:
        return await inter.response.send_message("Pick a real target (not yourself/bots).", ephemeral=True)

    gid, uid = inter.guild.id, inter.user.id

    # Must own the Sniper
    if await get_sniper(gid, uid) < 1:
        return await inter.response.send_message("You need to **buy the Sniper** first in `/shop`.", ephemeral=True)

    # Must have enough shekels to fire
    bal = await get_balance(gid, uid)
    if bal < SNIPER_SNIPE_COST:
        return await inter.response.send_message(f"You need **{SNIPER_SNIPE_COST} {EMO_SHEKEL()}** to fire a shot.", ephemeral=True)

    # Validate guess
    cleaned = "".join(ch for ch in word.lower().strip() if ch.isalpha())
    if len(cleaned) != 5:
        return await inter.response.send_message("Guess must be exactly 5 letters.", ephemeral=True)
    if not is_valid_guess(cleaned):
        return await inter.response.send_message("That’s not in the Wordle dictionary (UK variants supported).", ephemeral=True)

    # Target must have an active SOLO game
    target_cid = solo_channels.get((gid, target.id))
    if not target_cid or _key(gid, target_cid, target.id) not in solo_games:
        return await inter.response.send_message("That player has no active Worldle right now.", ephemeral=True)

    game = solo_games[_key(gid, target_cid, target.id)]

    # --- NEW: one shot per shooter per target game ---
    tried = game.setdefault("snipers_tried", set())
    if uid in tried:
        return await inter.response.send_message(
            "You’ve already taken your one shot at this Worldle. You can’t snipe it again.",
            ephemeral=True
        )
    # --------------------------------------------------

    # Defer now so we can safely use followups regardless of channel deletions later
    if not inter.response.is_done():
        try:
            await inter.response.defer(ephemeral=True, thinking=False)
        except Exception:
            pass

    # Pay to fire (charge into the victim's room for audit trail)
    await change_balance(gid, uid, -SNIPER_SNIPE_COST, announce_channel_id=target_cid)

    # Lock in that this shooter has used their shot for THIS game
    tried.add(uid)

    colors = score_guess(cleaned, game["answer"])
    row = render_row(cleaned, colors)

    # Post the snipe shot in the victim's room (ignore if channel vanished)
    try:
        ch = inter.guild.get_channel(target_cid)
        if isinstance(ch, discord.TextChannel):
            await ch.send(f"{EMO_SNIPER()} **{inter.user.display_name}** sniped with `{cleaned.upper()}`:\n{row}")
    except Exception:
        ch = None  # if anything went wrong, treat as missing

    # MISS → tell sniper and exit
    if cleaned != game["answer"]:
        try:
            await inter.followup.send("Missed shot. (Doesn't consume their tries.)", ephemeral=True)
        except Exception:
            pass
        return

    # HIT
    next_attempt = len(game["guesses"]) + 1  # snipe shot doesn't consume victim's tries
    payout = payout_for_attempt(next_attempt)
    if payout:
        await change_balance(gid, uid, payout, announce_channel_id=target_cid)

    # Count stats: shooter made a snipe; victim got sniped
    try:
        await inc_stat(gid, uid, "snipes", 1)
        await inc_stat(gid, target.id, "sniped", 1)
    except Exception:
        pass

    # Roll back the victim's daily solo slot (sniped games shouldn't count)
    try:
        start_day = game.get("start_date") or uk_today_str()
        await dec_solo_plays_on_date(gid, target.id, start_day)
    except Exception:
        pass

    origin_cid = game.get("origin_cid")
    ans = game["answer"].upper()

    # Build a final board for the announcement: victim guesses + this snipe shot
    try:
        guesses_for_board = list(game["guesses"]) + [{"word": cleaned, "colors": colors}]
        board_str = render_board(guesses_for_board)
    except Exception:
        board_str = None

    # Tell the sniper first (ephemeral is safe)
    try:
        await inter.followup.send(
            f"Hit confirmed. **Word: {ans}** · You earned **{payout} {EMO_SHEKEL()}**.",
            ephemeral=True
        )
    except Exception:
        pass

    # Public announcement (include board if we built it)
    ann = (
        f"🎯 {inter.user.mention} **sniped** {target.mention}'s Worldle (**{ans}**) on attempt **{next_attempt}** "
        f"and stole **{payout} {EMO_SHEKEL()}**!"
    )
    if board_str:
        ann += f"\n{board_str}"
    try:
        await _announce_result(inter.guild, origin_cid, ann)
    except Exception:
        pass

    # Cleanup the victim's game state
    try:
        solo_games.pop(_key(gid, target_cid, target.id), None)
        if solo_channels.get((gid, target.id)) == target_cid:
            solo_channels.pop((gid, target.id), None)
    except Exception:
        pass

    # Delete the victim's channel last (ignore errors if already gone)
    try:
        if isinstance(ch, discord.TextChannel):
            await ch.delete(reason="Worldle sniped (finished)")
    except Exception:
        pass
