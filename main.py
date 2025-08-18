# -------------------- imports --------------------
from bot import bot, TOKEN, worldle_bounty_guess, log
import discord
from casino import casino_start_word_pot, casino_games, casino_guess
from duels import duels, _duel_in_channel, worldle_duel_guess
from solo import solo_start, solo_guess, _key
from dungeon import dungeon_games, dungeon_guess
from drops import maybe_drop_shekel_on_message
from db import send_boxed
from utils import WORLDLER_ROLE_NAME, guard_worldler_msg
from config import bounty_games

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
