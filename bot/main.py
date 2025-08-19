"""Entry point for the Discord bot.

This module exposes a small configuration loader, creates a
:class:`commands.Bot` instance with intents, dynamically loads any cogs
found in the :mod:`commands` package, and runs the bot.  The script can
be started via ``python -m bot.main``.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List

import discord
from discord.ext import commands

# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------

@dataclass
class Config:
    """Basic configuration for the bot."""

    token: str
    guild_ids: List[int]


def load_config() -> Config:
    """Load configuration from environment variables or a ``.env`` file.

    The ``.env`` file, if present in the repository root, should contain
    ``DISCORD_TOKEN`` and optionally ``GUILD_IDS`` (comma separated).
    Environment variables take precedence over values in the file.
    """

    root = Path(__file__).resolve().parent.parent
    env_path = root / ".env"
    file_vars: dict[str, str] = {}

    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            file_vars[key.strip()] = value.strip()

    token = os.getenv("DISCORD_TOKEN", file_vars.get("DISCORD_TOKEN", ""))
    guilds_raw = os.getenv("GUILD_IDS", file_vars.get("GUILD_IDS", ""))
    guild_ids = [int(g.strip()) for g in guilds_raw.split(",") if g.strip()]

    return Config(token=token, guild_ids=guild_ids)


# ---------------------------------------------------------------------------
# bot setup
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True  # enable privileged intent for commands

bot = commands.Bot(command_prefix="!", intents=intents)


async def load_cogs() -> None:
    """Load all cogs from the :mod:`commands` package."""

    root = Path(__file__).resolve().parent.parent
    cogs_dir = root / "commands"
    if not cogs_dir.exists():
        return

    for path in cogs_dir.glob("*.py"):
        if path.name == "__init__.py":
            continue
        try:
            await bot.load_extension(f"commands.{path.stem}")
            logging.getLogger(__name__).info("Loaded cog %s", path.stem)
        except Exception as exc:  # pragma: no cover - logging for visibility
            logging.getLogger(__name__).warning(
                "Failed to load cog %s: %s", path.stem, exc
            )


@bot.event
async def on_ready() -> None:
    logging.getLogger(__name__).info("Logged in as %s", bot.user)


async def main() -> None:
    """Entrypoint used by ``python -m bot.main``."""

    cfg = load_config()
    if not cfg.token:
        raise SystemExit("DISCORD_TOKEN is not set.")

    await load_cogs()
    await bot.start(cfg.token)


if __name__ == "__main__":  # pragma: no cover - manual execution only
    asyncio.run(main())
