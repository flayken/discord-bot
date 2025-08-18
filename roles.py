# -------------------- role tiers --------------------
def bot_can_manage_role(guild: discord.Guild, role: discord.Role) -> bool:
    me = guild.me
    return bool(me and me.guild_permissions.manage_roles and role < me.top_role)

async def ensure_worldler_role(guild: discord.Guild) -> int:
    cfg = await get_cfg(guild.id)
    if cfg["worldler_role_id"]:
        role = guild.get_role(cfg["worldler_role_id"])
        if role: return role.id
    role = discord.utils.find(lambda r: r.name.lower()==WORLDLER_ROLE_NAME.lower(), guild.roles)
    if role is None:
        if not guild.me or not guild.me.guild_permissions.manage_roles:
            log.warning(f"[worldler] Missing Manage Roles in guild {guild.id}")
            return 0
        role = await guild.create_role(name=WORLDLER_ROLE_NAME, reason="Wordle World membership role")
    await set_cfg(guild.id, worldler_role_id=role.id)
    return role.id

async def ensure_bounty_role(guild: discord.Guild) -> int:
    cfg = await get_cfg(guild.id)
    if cfg["bounty_role_id"]:
        role = guild.get_role(cfg["bounty_role_id"])
        if role: return role.id
    role = discord.utils.find(lambda r: r.name.lower()==BOUNTY_ROLE_NAME.lower(), guild.roles)
    if role is None:
        if not guild.me or not guild.me.guild_permissions.manage_roles:
            log.warning(f"[bounty-role] Missing Manage Roles in guild {guild.id}")
            return 0
        role = await guild.create_role(name=BOUNTY_ROLE_NAME, reason="Wordle World bounty role")
    await set_cfg(guild.id, bounty_role_id=role.id)
    return role.id

async def is_worldler(guild: discord.Guild, member: discord.abc.User) -> bool:
    cfg = await get_cfg(guild.id)
    rid = cfg["worldler_role_id"]
    if not rid:
        return False
    try:
        m = guild.get_member(member.id) or await guild.fetch_member(member.id)
        return any(r.id == rid for r in m.roles)
    except Exception:
        return False

async def sync_member_role_tiers(guild: discord.Guild, member: discord.Member):
    if not await is_worldler(guild, member):
        return
    async with bot.db.execute("SELECT role_id,min_balance FROM role_tier WHERE guild_id=? ORDER BY min_balance ASC",(guild.id,)) as cur:
        rows = await cur.fetchall()
    if not rows: return
    manageable=[]
    for role_id, min_bal in rows:
        role = guild.get_role(role_id)
        if role and bot_can_manage_role(guild, role):
            manageable.append((role, min_bal))
    if not manageable: return

    async with bot.db.execute("SELECT balance FROM wallet WHERE guild_id=? AND user_id=?", (guild.id, member.id)) as cur:
        rr = await cur.fetchone()
    bal = rr[0] if rr else 0

    want_ids = {r.id for (r, minimum) in manageable if bal >= minimum}
    tier_ids = {r.id for (r, _) in manageable}
    have_ids = {r.id for r in member.roles}

    to_add = [guild.get_role(rid) for rid in (want_ids - have_ids) if guild.get_role(rid)]
    to_remove = [guild.get_role(rid) for rid in ((tier_ids - want_ids) & have_ids) if guild.get_role(rid)]

    if to_add:
        try: await member.add_roles(*to_add, reason="Wordle World tier sync")
        except Exception as e: log.warning(f"add_roles failed: {e}")
    if to_remove:
        try: await member.remove_roles(*to_remove, reason="Wordle World tier sync")
        except Exception as e: log.warning(f"remove_roles failed: {e}")

async def _sync_member_roles_after_balance_change(gid: int, uid: int, channel_id: Optional[int]):
    guild = discord.utils.get(bot.guilds, id=gid)
    if not guild: return
    try:
        member = guild.get_member(uid) or await guild.fetch_member(uid)
    except Exception:
        return
    await sync_member_role_tiers(guild, member)

async def ensure_default_tiers(guild: discord.Guild):
    for idx, (name, min_bal) in enumerate(DEFAULT_TIERS):
        role = discord.utils.find(lambda r: r.name.lower()==name.lower(), guild.roles)
        if role is None:
            if not guild.me or not guild.me.guild_permissions.manage_roles:
                log.warning(f"[tiers] Missing Manage Roles in guild {guild.id}")
                return
            role = await guild.create_role(name=name, reason="Wordle World auto tier")
        await bot.db.execute("""
          INSERT INTO role_tier(guild_id,role_id,min_balance) VALUES(?,?,?)
          ON CONFLICT(guild_id,role_id) DO UPDATE SET min_balance=excluded.min_balance
        """, (guild.id, role.id, int(min_bal)))
    await bot.db.commit()


# -------------------- Roles admin --------------------
@tree.command(name="role_maketier", description="(Admin) Create a role and bind it to a Shekel minimum.")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(name="Role name", min="Minimum Shekels")
async def role_maketier(inter: discord.Interaction, name: str, min: int):
    if not inter.guild: return await inter.response.send_message("Server only.", ephemeral=True)
    guild = inter.guild
    if min < 0: return await inter.response.send_message("Min must be ≥0.", ephemeral=True)
    if not guild.me or not guild.me.guild_permissions.manage_roles:
        return await inter.response.send_message("I need **Manage Roles**.", ephemeral=True)
    role = discord.utils.find(lambda r: r.name.lower()==name.lower(), guild.roles)
    if role is None:
        role = await guild.create_role(name=name, reason="Create Wordle World tier")
    await bot.db.execute("""
      INSERT INTO role_tier(guild_id,role_id,min_balance) VALUES(?,?,?)
      ON CONFLICT(guild_id,role_id) DO UPDATE SET min_balance=excluded.min_balance
    """, (guild.id, role.id, min))
    await bot.db.commit()
    await inter.response.send_message(f"✅ Created/bound tier: {role.mention} at **{min}**.")

@tree.command(name="role_addtier", description="(Admin) Bind an existing role to a Shekel minimum.")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(role="Role", min="Minimum Shekels")
async def role_addtier(inter: discord.Interaction, role: discord.Role, min: int):
    if not inter.guild: return await inter.response.send_message("Server only.", ephemeral=True)
    if min < 0: return await inter.response.send_message("Min must be ≥0.", ephemeral=True)
    if not bot_can_manage_role(inter.guild, role):
        return await inter.response.send_message("I can't manage that role. Move my role above it & grant **Manage Roles**.", ephemeral=True)
    await bot.db.execute("""
      INSERT INTO role_tier(guild_id,role_id,min_balance) VALUES(?,?,?)
      ON CONFLICT(guild_id,role_id) DO UPDATE SET min_balance=excluded.min_balance
    """, (inter.guild.id, role.id, min))
    await bot.db.commit()
    await inter.response.send_message(f"✅ Bound tier: **{role.name}** at **{min}**.")

@tree.command(name="role_removetier", description="(Admin) Remove a tier mapping.")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(role="Role")
async def role_removetier(inter: discord.Interaction, role: discord.Role):
    if not inter.guild: return await inter.response.send_message("Server only.", ephemeral=True)
    await bot.db.execute("DELETE FROM role_tier WHERE guild_id=? AND role_id=?", (inter.guild.id, role.id))
    await bot.db.commit()
    await inter.response.send_message(f"🗑️ Removed tier for **{role.name}**.")

@tree.command(name="role_tiers", description="List tier roles.")
async def role_tiers(inter: discord.Interaction):
    if not await guard_worldler_inter(inter): return
    async with bot.db.execute("SELECT role_id,min_balance FROM role_tier WHERE guild_id=? ORDER BY min_balance ASC",(inter.guild.id,)) as cur:
        rows = await cur.fetchall()
    if not rows: return await inter.response.send_message("No tiers configured.")
    lines = ["🏷️ **Role Tiers** (balance ≥ min):"]
    for rid, min_bal in rows:
        role = inter.guild.get_role(rid)
        lines.append(f"• {role.mention if role else '(missing role)'} — **{min_bal}**")
    await inter.response.send_message("\n".join(lines))

@tree.command(name="role_sync", description="(Admin) Resync tier roles for everyone I know.")
@app_commands.default_permissions(administrator=True)
async def role_sync(inter: discord.Interaction):
    if not inter.guild: return await inter.response.send_message("Server only.", ephemeral=True)
    await inter.response.send_message("⏳ Syncing…", ephemeral=True)
    async with bot.db.execute("SELECT user_id FROM wallet WHERE guild_id=?", (inter.guild.id,)) as cur:
        ids = [r[0] for r in await cur.fetchall()]
    for uid in ids:
        try:
            member = inter.guild.get_member(uid) or await inter.guild.fetch_member(uid)
            await sync_member_role_tiers(inter.guild, member)
        except Exception:
            continue
    await inter.followup.send("✅ Done.")
