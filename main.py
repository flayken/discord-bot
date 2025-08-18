# worldle_bot/main.py
import os
import discord
from discord.ext import commands

from core import config, lifecycle

# Intents: adjust as needed (right now includes reactions + members)
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.reactions = True
intents.guilds = True

# Create bot
bot = commands.Bot(command_prefix="!", intents=intents)

# Expose globally if you still need legacy access
# (but ideally pass bot around to features instead of global)
import builtins
builtins.bot = bot

# Load features/extensions
EXTENSIONS = [
    "features.shop",
    "features.economy",
    "features.casino",
    "features.dailies",
    "features.bounty",
    "features.dungeon",
    "features.duels",
    "features.solo",
    "features.help",
    "features.admin",
    "events.on_message",
    "events.reactions",
]

@bot.event
async def on_ready():
    await lifecycle.on_ready(bot)

@bot.event
async def on_guild_join(guild: discord.Guild):
    await lifecycle.on_guild_join(bot, guild)


def main():
    token = os.getenv("DISCORD_TOKEN", config.TOKEN)
    if not token:
        raise SystemExit("❌ Missing DISCORD_TOKEN in environment or config.")
    for ext in EXTENSIONS:
        try:
            bot.load_extension(ext)
            print(f"✅ Loaded extension: {ext}")
        except Exception as e:
            print(f"⚠️ Failed to load {ext}: {e}")
    bot.run(token)


if __name__ == "__main__":
    main()
