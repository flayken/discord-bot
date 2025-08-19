"""Simple ping command cog for demonstration."""

import discord
from discord.ext import commands


class Ping(commands.Cog):
    """A minimal cog with a single ping command."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.command()
    async def ping(self, ctx: commands.Context) -> None:
        """Respond with pong."""
        await ctx.send("Pong!")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Ping(bot))
