# -------------------- lifecycle --------------------
@bot.event
async def on_ready():
    log_deps_health()
    await db_init()
    for g in bot.guilds:
        try:
            await ensure_worldler_role(g)
            await ensure_bounty_role(g)
            if DEFAULT_TIERS:
                await ensure_default_tiers(g)
            # ensure casino pot row exists
            await get_casino_pot(g.id)
        except Exception as e:
            log.warning(f"guild init {g.id} failed: {e}")
    build_emoji_lookup()
    try:
        await tree.sync()
        print("Global slash commands synced.")
    except Exception as e:
        log.warning(f"global sync failed: {e}")
    if not bounty_loop.is_running():
        bounty_loop.start()
    me = bot.user
    print(f"Logged in as {me} ({me.id})")

@bot.event
async def on_guild_join(guild: discord.Guild):
    await ensure_worldler_role(guild)
    await ensure_bounty_role(guild)
    if DEFAULT_TIERS:
        await ensure_default_tiers(guild)
    await get_casino_pot(guild.id)
