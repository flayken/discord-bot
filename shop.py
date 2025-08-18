# worldle_bot/features/shop.py
"""
Shop feature:
 - /shop interactive panel (buttons -> quantity modals)
 - /buy and /sell slash commands
 - Public success messages; validation errors are ephemeral
"""

from __future__ import annotations

import asyncio
import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from ..core.utils import safe_send, send_boxed, make_panel, log
from ..core.config import (
    EMO_SHEKEL,
    EMO_STONE,
    EMO_CHICKEN,
    EMO_BADGE,
    EMO_SNIPER,
    EMO_DUNGEON,  # used for ticket icon
)
from ..core.db import (
    get_balance,
    change_balance,
    get_stones,
    change_stones,
    get_badge,
    set_badge,
    get_chickens,
    change_chickens,
    get_sniper,
    set_sniper,
    get_dungeon_tickets_t3,
    change_dungeon_tickets_t3,
    get_cfg,
)
from .roles import ensure_bounty_role, bot_can_manage_role

# injected by register()
bot: commands.Bot | None = None
tree: app_commands.CommandTree | None = None

# -------------------- Prices / Catalog --------------------
PRICE_STONE = 1
PRICE_BADGE = 5
PRICE_CHICK = 2
PRICE_SNIPER = 100

SHOP_ITEMS = {
    "stone": {
        "label": "Stone",
        "price": PRICE_STONE,
        "desc": "Throw with /stone. 49% drop chance per stone (bulk supported).",
        "emoji": EMO_STONE,
    },
    "badge": {
        "label": "Bounty Hunter Badge",
        "price": PRICE_BADGE,
        "desc": "Grants the Bounty Hunter role. Bounties ping that role.",
        "emoji": EMO_BADGE,
    },
    "chicken": {
        "label": "Fried Chicken",
        "price": PRICE_CHICK,
        "desc": "Use /eat to gain 1h immunity from stones.",
        "emoji": EMO_CHICKEN,
    },
    "sniper": {
        "label": "Sniper",
        "price": PRICE_SNIPER,
        "desc": "Lets you /snipe other players' solo Wordle (1 shekel per shot). One-time purchase.",
        "emoji": EMO_SNIPER,
    },
    "ticket_t3": {
        "label": "Dungeon Ticket (Tier 3)",
        "price": 5,
        "desc": "Opens a Tier 3 Worldle Dungeon. Use /worldle_dungeon tier:Tier 3.",
        "emoji": EMO_DUNGEON,
    },
}

SHOP_ORDER = ["stone", "badge", "chicken", "sniper", "ticket_t3"]


# -------------------- Helpers for panels --------------------
async def _get_owned_sellables(gid: int, uid: int) -> list[dict]:
    stones = await get_stones(gid, uid)
    chickens = await get_chickens(gid, uid)
    badge = 1 if (await get_badge(gid, uid)) >= 1 else 0
    sniper = 1 if (await get_sniper(gid, uid)) >= 1 else 0
    t3 = await get_dungeon_tickets_t3(gid, uid)

    out: list[dict] = []
    if stones > 0:
        out.append({
            "key": "stone", "label": "Stone", "count": stones,
            "price_each": SHOP_ITEMS["stone"]["price"], "emoji": EMO_STONE()
        })
    if badge > 0:
        out.append({
            "key": "badge", "label": "Bounty Hunter Badge", "count": badge,
            "price_each": SHOP_ITEMS["badge"]["price"], "emoji": EMO_BADGE()
        })
    if chickens > 0:
        out.append({
            "key": "chicken", "label": "Fried Chicken", "count": chickens,
            "price_each": SHOP_ITEMS["chicken"]["price"], "emoji": EMO_CHICKEN()
        })
    if sniper > 0:
        out.append({
            "key": "sniper", "label": "Sniper", "count": sniper,
            "price_each": SHOP_ITEMS["sniper"]["price"], "emoji": EMO_SNIPER()
        })
    if t3 > 0:
        out.append({
            "key": "ticket_t3", "label": "Dungeon Ticket (Tier 3)", "count": t3,
            "price_each": SHOP_ITEMS["ticket_t3"]["price"], "emoji": EMO_DUNGEON()
        })
    return out


async def _build_shop_embed(guild_id: int, user_id: int) -> discord.Embed:
    bal = await get_balance(guild_id, user_id)

    # counts
    stones = await get_stones(guild_id, user_id)
    chickens = await get_chickens(guild_id, user_id)
    badge = 1 if (await get_badge(guild_id, user_id)) >= 1 else 0
    sniper = 1 if (await get_sniper(guild_id, user_id)) >= 1 else 0
    t3 = await get_dungeon_tickets_t3(guild_id, user_id)

    em = discord.Embed(title="🛒 Shop", color=0x2B2D31)
    em.description = f"Balance: **{bal} {EMO_SHEKEL()}**"
    for key in SHOP_ORDER:
        it = SHOP_ITEMS[key]
        e = it["emoji"]()
        owned_txt = ""
        if key == "stone":
            owned_txt = f" · Owned: {stones}"
        elif key == "chicken":
            owned_txt = f" · Owned: {chickens}"
        elif key == "badge":
            owned_txt = f" · Owned: {'yes' if badge else 'no'}"
        elif key == "sniper":
            owned_txt = f" · Owned: {'yes' if sniper else 'no'}"
        elif key == "ticket_t3":
            owned_txt = f" · Owned: {t3}"

        em.add_field(
            name=f"{e} {it['label']} — {it['price']} {EMO_SHEKEL()}",
            value=it["desc"] + owned_txt,
            inline=False
        )
    em.set_footer(text="Use the buttons below or /buy and /sell.")
    return em


# -------------------- Core buy/sell helpers --------------------
async def _shop_perform_buy(i: discord.Interaction, key: str, qty: int, *, _from_modal: bool = False):
    """
    Performs the shop purchase and replies:
      • PUBLIC message on success
      • Ephemeral message on validation / balance errors
    """
    async def _send(msg: str, *, ephemeral: bool = False):
        return await send_boxed(
            i,
            title="Shop",
            description=msg,
            icon="🛒",
            ephemeral=ephemeral,
        )

    if not i.guild or not i.channel:
        return await _send("Run this in a server.", ephemeral=True)

    gid, cid, uid = i.guild.id, i.channel.id, i.user.id

    # Canonical catalog for buy prices (must match SHOP_ITEMS)
    CATALOG = {
        "stone":     {"label": "Stone",                    "price": SHOP_ITEMS["stone"]["price"]},
        "badge":     {"label": "Bounty Hunter Badge",      "price": SHOP_ITEMS["badge"]["price"]},
        "chicken":   {"label": "Fried Chicken",            "price": SHOP_ITEMS["chicken"]["price"]},
        "sniper":    {"label": "Sniper",                   "price": SHOP_ITEMS["sniper"]["price"]},
        "ticket_t3": {"label": "Dungeon Ticket (Tier 3)",  "price": SHOP_ITEMS["ticket_t3"]["price"]},
    }
    # Accept a few friendly aliases
    ALIASES = {
        "stones": "stone",
        "bounty_hunter_badge": "badge",
        "fried_chicken": "chicken",
        "sniper_rifle": "sniper",
        "tier3": "ticket_t3",
        "tier_3": "ticket_t3",
        "t3": "ticket_t3",
        "t3_ticket": "ticket_t3",
        "dungeon_ticket": "ticket_t3",
        "ticket_t3": "ticket_t3",
    }

    key_norm = str(key).strip().lower().replace(" ", "_")
    key_norm = ALIASES.get(key_norm, key_norm)

    # Validate item & quantity
    item = CATALOG.get(key_norm)
    if not item:
        return await _send(f"Unknown shop item: `{key}`.", ephemeral=True)
    if qty <= 0:
        return await _send("Quantity must be at least 1.", ephemeral=True)
    if qty > 100_000:
        return await _send("That quantity is too large. Try something smaller.", ephemeral=True)

    # One-time items must be bought singly
    if key_norm in ("badge", "sniper") and qty != 1:
        return await _send("That item can only be bought one at a time.", ephemeral=True)

    price = int(item["price"])
    cost = price * qty

    # Economy glue → your DB helpers
    async def _get_balance(uid_: int) -> int:
        return await get_balance(gid, uid_)

    async def _add_balance(uid_: int, delta: int) -> None:
        await change_balance(gid, uid_, delta, announce_channel_id=cid)

    async def _inv_add(uid_: int, item_key: str, amount: int) -> None:
        if item_key == "stone":
            await change_stones(gid, uid_, amount)
        elif item_key == "chicken":
            await change_chickens(gid, uid_, amount)
        elif item_key == "badge":
            if await get_badge(gid, uid_) >= 1:
                raise RuntimeError("You already own the Bounty Hunter Badge.")
            await set_badge(gid, uid_, 1)
            # Grant bounty role if we can
            try:
                rid = await ensure_bounty_role(i.guild)
                role = i.guild.get_role(rid) if rid else None
                member = i.guild.get_member(uid_) or await i.guild.fetch_member(uid_)
                if role and member and bot_can_manage_role(i.guild, role):
                    await member.add_roles(role, reason="Bought Bounty Hunter Badge")
            except Exception as e:
                log.warning(f"grant bounty role failed: {e}")
        elif item_key == "sniper":
            if await get_sniper(gid, uid_) >= 1:
                raise RuntimeError("You already own the Sniper.")
            await set_sniper(gid, uid_, 1)
        elif item_key == "ticket_t3":
            await change_dungeon_tickets_t3(gid, uid_, amount)
        else:
            raise RuntimeError(f"Unhandled inventory item: {item_key}")

    balance = await _get_balance(uid)
    if balance < cost:
        short = cost - balance
        return await _send(
            f"Not enough shekels: need **{short}** more (cost **{cost}**, you have **{balance}**).",
            ephemeral=True,
        )

    # Charge and deliver
    try:
        await _add_balance(uid, -cost)
        await _inv_add(uid, key_norm, qty)
    except RuntimeError as e:
        # Refund if inventory op failed for a known reason
        try:
            await _add_balance(uid, +cost)
        except Exception:
            pass
        return await _send(str(e), ephemeral=True)

    new_balance = await _get_balance(uid)
    label = item["label"]

    # PUBLIC success message
    await _send(
        f"**{i.user.mention}** bought **{qty}× {label}** for **{cost}** {EMO_SHEKEL()}. "
        f"New balance: **{new_balance}**.",
        ephemeral=False,
    )


async def _shop_perform_sell(i: discord.Interaction, key: str, qty: int):
    """Performs a SELL and replies PUBLICLY (non-ephemeral) on both success and errors."""
    async def _send(msg: str):
        return await send_boxed(i, "Shop — Sell", msg, icon="🛍️", ephemeral=False)

    if not i.guild or not i.channel:
        return await _send("Run this in a server.")

    gid, uid, cid = i.guild.id, i.user.id, i.channel.id
    key = str(key).strip().lower()

    if qty <= 0:
        return await _send("Quantity must be at least 1.")

    # Stones
    if key == "stone":
        have = await get_stones(gid, uid)
        if have < qty:
            return await _send("You don't have that many stones to sell.")
        await change_stones(gid, uid, -qty)
        refund = SHOP_ITEMS["stone"]["price"] * qty
        await change_balance(gid, uid, refund, announce_channel_id=cid)
        bal = await get_balance(gid, uid)
        return await _send(f"Sold **{qty}× {EMO_STONE()} Stone** for **{refund} {EMO_SHEKEL()}**. New balance: **{bal}**.")

    # Badge (one-time)
    if key == "badge":
        have = 1 if (await get_badge(gid, uid)) >= 1 else 0
        if have < qty:
            return await _send("You don't have that many badges to sell.")
        # selling badge removes it
        await set_badge(gid, uid, 0)
        refund = SHOP_ITEMS["badge"]["price"] * qty
        await change_balance(gid, uid, refund, announce_channel_id=cid)
        bal = await get_balance(gid, uid)
        return await _send(f"Sold **{qty}× {EMO_BADGE()} Badge** for **{refund} {EMO_SHEKEL()}**. New balance: **{bal}**.")

    # Chicken
    if key == "chicken":
        have = await get_chickens(gid, uid)
        if have < qty:
            return await _send("You don't have that many chickens to sell.")
        await change_chickens(gid, uid, -qty)
        refund = SHOP_ITEMS["chicken"]["price"] * qty
        await change_balance(gid, uid, refund, announce_channel_id=cid)
        bal = await get_balance(gid, uid)
        return await _send(f"Sold **{qty}× {EMO_CHICKEN()} Chicken** for **{refund} {EMO_SHEKEL()}**. New balance: **{bal}**.")

    # Sniper
    if key == "sniper":
        have = 1 if (await get_sniper(gid, uid)) >= 1 else 0
        if have < qty:
            return await _send("You don't have that many snipers to sell.")
        await set_sniper(gid, uid, 0)
        refund = SHOP_ITEMS["sniper"]["price"] * qty
        await change_balance(gid, uid, refund, announce_channel_id=cid)
        bal = await get_balance(gid, uid)
        return await _send(f"Sold **{qty}× {EMO_SNIPER()} Sniper** for **{refund} {EMO_SHEKEL()}**. New balance: **{bal}**.")

    # Tier-3 Ticket
    if key == "ticket_t3":
        have = await get_dungeon_tickets_t3(gid, uid)
        if have < qty:
            return await _send("You don't have that many Tier-3 tickets to sell.")
        await change_dungeon_tickets_t3(gid, uid, -qty)
        refund = SHOP_ITEMS["ticket_t3"]["price"] * qty
        await change_balance(gid, uid, refund, announce_channel_id=cid)
        bal = await get_balance(gid, uid)
        return await _send(f"Sold **{qty}× {EMO_DUNGEON()} Tier-3 Ticket** for **{refund} {EMO_SHEKEL()}**. New balance: **{bal}**.")

    return await _send("Can't sell that item here.")


# -------------------- UI: Modals & Views --------------------
class QuantityModal(discord.ui.Modal):
    def __init__(
        self,
        key: str,
        label: str,
        owner_id: int,
        panel_channel_id: int,
        panel_message_id: int,
    ):
        super().__init__(title=f"Buy {label}")
        self.key = key
        self.label = label
        self.owner_id = int(owner_id)
        self.panel_channel_id = int(panel_channel_id)
        self.panel_message_id = int(panel_message_id)

        self.amount = discord.ui.TextInput(
            label="Quantity to buy",
            placeholder="Enter a positive whole number",
            required=True,
            min_length=1,
            max_length=6,
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner_id:
            return await send_boxed(
                interaction, "Shop", "This shop view isn’t yours. Use **/shop** to open your own.", icon="🛒"
            )

        # validate
        try:
            qty = int(str(self.amount.value).strip())
        except ValueError:
            return await send_boxed(interaction, "Shop", "Enter a whole number.", icon="🛒")
        if qty <= 0:
            return await send_boxed(interaction, "Shop", "Enter a quantity greater than **0**.", icon="🛒")

        # don’t block UI
        try:
            await interaction.response.defer(thinking=False)
        except Exception:
            pass

        # do the buy (public confirmation)
        await _shop_perform_buy(interaction, self.key, qty)

        # refresh the original Shop message so the balance updates
        try:
            gid, uid = interaction.guild.id, interaction.user.id
            emb = await _build_shop_embed(gid, uid)

            channel = interaction.client.get_channel(self.panel_channel_id) or await interaction.client.fetch_channel(self.panel_channel_id)
            msg = await channel.fetch_message(self.panel_message_id)

            view = ShopView(interaction)
            view.message = msg
            await msg.edit(embed=emb, view=view)
        except Exception:
            # swallow refresh errors to avoid breaking the purchase flow
            pass


class SellQuantityModal(discord.ui.Modal):
    def __init__(
        self,
        key: str,
        label: str,
        max_qty: int,
        price_each: int,
        owner_id: int | None,
        panel_channel_id: int,
        panel_message_id: int,
    ):
        super().__init__(title=f"Sell {label}")
        self.key = key
        self.label = label
        self.max_qty = int(max_qty)
        self.price_each = int(price_each)
        self.owner_id = owner_id
        self.panel_channel_id = int(panel_channel_id)
        self.panel_message_id = int(panel_message_id)

        hint = f"You own {self.max_qty}. Price each: {self.price_each} {EMO_SHEKEL()}."
        self.amount = discord.ui.TextInput(
            label="Quantity to sell",
            placeholder=f"Enter 1–{self.max_qty}  ({hint})",
            required=True,
            min_length=1,
            max_length=6,
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if self.owner_id and interaction.user.id != int(self.owner_id):
            return await send_boxed(interaction, "Shop — Sell", "This sell panel isn’t yours. Use **/shop** to open your own.", icon="🛍️")

        try:
            qty = int(str(self.amount.value).strip())
        except ValueError:
            return await send_boxed(interaction, "Shop — Sell", "Enter a whole number.", icon="🛍️")
        if qty <= 0 or qty > self.max_qty:
            return await send_boxed(interaction, "Shop — Sell", f"Enter **1–{self.max_qty}**.", icon="🛍️")

        try:
            await interaction.response.defer(thinking=False)
        except Exception:
            pass

        # Perform the sale (public confirmation message)
        await _shop_perform_sell(interaction, self.key, qty)

        # Refresh the Sell panel (balance + counts)
        try:
            gid, uid = interaction.guild.id, interaction.user.id
            owned = await _get_owned_sellables(gid, uid)
            emb = await _build_sell_embed(gid, uid, owned)

            channel = interaction.client.get_channel(self.panel_channel_id) or await interaction.client.fetch_channel(self.panel_channel_id)
            msg = await channel.fetch_message(self.panel_message_id)

            view = SellMenuView(interaction, owned)
            view.message = msg
            await msg.edit(embed=emb, view=view)
        except Exception:
            pass


class ShopView(discord.ui.View):
    """Buttons open a quantity modal (buy) or switch to the Sell panel. Success messages are PUBLIC."""
    def __init__(self, inter: discord.Interaction, *, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.owner_id = inter.user.id
        self.guild_id = inter.guild.id
        self.message: Optional[discord.Message] = None  # set by /shop after send

        def add_buy_btn(key: str, label: str, emoji: str, style: discord.ButtonStyle):
            btn = discord.ui.Button(label=label, emoji=emoji, style=style)

            async def _cb(i: discord.Interaction, _key=key, _label=label):
                if i.user.id != self.owner_id:
                    return await send_boxed(i, "Shop", "This shop view isn’t yours. Use **/shop** to open your own.", icon="🛒")
                # pass panel ids so the modal can refresh the same message after purchase
                await i.response.send_modal(
                    QuantityModal(
                        _key,
                        _label,
                        self.owner_id,
                        panel_channel_id=i.channel.id,
                        panel_message_id=self.message.id if self.message else i.message.id,
                    )
                )

            btn.callback = _cb  # type: ignore
            self.add_item(btn)

        # Buy buttons
        add_buy_btn("stone",     "Stone",     EMO_STONE(),   discord.ButtonStyle.primary)
        add_buy_btn("badge",     "Badge",     EMO_BADGE(),   discord.ButtonStyle.secondary)
        add_buy_btn("chicken",   "Chicken",   EMO_CHICKEN(), discord.ButtonStyle.secondary)
        add_buy_btn("sniper",    "Sniper",    EMO_SNIPER(),  discord.ButtonStyle.secondary)
        add_buy_btn("ticket_t3", "T3 Ticket", EMO_DUNGEON(), discord.ButtonStyle.success)

        # SELL (red) — replaces this message with the Sell menu
        sell_btn = discord.ui.Button(label="Sell", emoji="💸", style=discord.ButtonStyle.danger)

        async def _sell_cb(i: discord.Interaction):
            if i.user.id != self.owner_id:
                return await send_boxed(i, "Shop — Sell", "This sell panel isn’t yours. Use **/shop** to open your own.", icon="🛍️")
            owned = await _get_owned_sellables(i.guild.id, i.user.id)
            emb = await _build_sell_embed(i.guild.id, i.user.id, owned)
            view = SellMenuView(i, owned)
            view.message = self.message
            await i.response.edit_message(embed=emb, view=view)

        sell_btn.callback = _sell_cb  # type: ignore
        self.add_item(sell_btn)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        try:
            if self.message:
                await self.message.edit(view=self)
        except Exception:
            pass


def _sell_line(item: dict) -> str:
    e = item["emoji"]
    return f"{e} **{item['label']}** — you own **{item['count']}** · sell price **{item['price_each']} {EMO_SHEKEL()}** each"


async def _build_sell_embed(guild_id: int, user_id: int, owned: list[dict]) -> discord.Embed:
    bal = await get_balance(guild_id, user_id)
    em = discord.Embed(title="💸 Sell to Shop", color=0x2B2D31)
    if not owned:
        em.description = f"Balance: **{bal} {EMO_SHEKEL()}**\nYou don't own any sellable items."
        return em

    lines = [_sell_line(it) for it in owned]
    em.description = f"Balance: **{bal} {EMO_SHEKEL()}**\n\n" + "\n".join(lines)
    em.set_footer(text="Pick an item from the dropdown to sell.")
    return em


class SellMenuView(discord.ui.View):
    def __init__(self, inter: discord.Interaction, owned: list[dict], *, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.owner_id = inter.user.id
        self.guild_id = inter.guild.id
        self.channel_id = inter.channel.id
        self.message: Optional[discord.Message] = None
        self.owned = owned

        # Select with owned items
        options = []
        for item in owned:
            label = f"{item['emoji']} {item['label']}"
            desc = f"Owned: {item['count']} • Sell price: {item['price_each']} each"
            options.append(discord.SelectOption(label=label, value=item["key"], description=desc))

        select = discord.ui.Select(placeholder="Pick an item to sell…", options=options)

        async def _on_pick(i: discord.Interaction):
            if i.user.id != self.owner_id:
                return await send_boxed(i, "Shop — Sell", "This sell panel isn’t yours. Use **/shop** to open your own.", icon="🛍️")

            await i.response.send_modal(
                SellQuantityModal(
                    key=select.values[0],
                    label=next(x["label"] for x in self.owned if x["key"] == select.values[0]),
                    max_qty=next(x["count"] for x in self.owned if x["key"] == select.values[0]),
                    price_each=next(x["price_each"] for x in self.owned if x["key"] == select.values[0]),
                    owner_id=self.owner_id,
                    panel_channel_id=self.channel_id,
                    panel_message_id=self.message.id if self.message else i.message.id,
                )
            )

        select.callback = _on_pick  # type: ignore
        self.add_item(select)

        # Back to Shop
        back_btn = discord.ui.Button(label="Shop", emoji="🛒", style=discord.ButtonStyle.primary)

        async def _back(i: discord.Interaction):
            if i.user.id != self.owner_id:
                return await send_boxed(i, "Shop", "This shop view isn’t yours. Use **/shop** to open your own.", icon="🛒")
            emb = await _build_shop_embed(self.guild_id, self.owner_id)
            view = ShopView(i)
            view.message = self.message
            await i.response.edit_message(embed=emb, view=view)

        back_btn.callback = _back  # type: ignore
        self.add_item(back_btn)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        try:
            if self.message:
                await self.message.edit(view=self)
        except Exception:
            pass


# -------------------- Slash commands --------------------
def _bind_commands(_tree: app_commands.CommandTree):

    @_tree.command(name="shop", description="Show the shop.")
    async def shop(inter: discord.Interaction):
        # guard is expected to be handled in your global middleware/feature
        if not inter.guild:
            return await send_boxed(inter, "Shop", "Run this in a server.", icon="🛒", ephemeral=True)

        emb = await _build_shop_embed(inter.guild.id, inter.user.id)
        view = ShopView(inter)
        await inter.response.send_message(embed=emb, view=view)

        try:
            msg = await inter.original_response()
            view.message = msg
        except Exception:
            pass

    @_tree.command(name="buy", description="Buy from the shop.")
    @app_commands.describe(item="Item", amount="How many")
    @app_commands.choices(item=[app_commands.Choice(name=f"{SHOP_ITEMS[k]['emoji']()} {SHOP_ITEMS[k]['label']}", value=k) for k in SHOP_ORDER])
    async def buy(inter: discord.Interaction, item: app_commands.Choice[str], amount: int):
        if not inter.guild:
            return await send_boxed(inter, "Shop", "Run this in a server.", icon="🛒", ephemeral=True)
        await _shop_perform_buy(inter, item.value, amount)

    @_tree.command(name="sell", description="Sell items back to the shop for the same price.")
    @app_commands.describe(item="Item", amount="How many")
    @app_commands.choices(item=[
        app_commands.Choice(name="Stone", value="stone"),
        app_commands.Choice(name="Bounty Hunter Badge", value="badge"),
        app_commands.Choice(name="Fried Chicken", value="chicken"),
        app_commands.Choice(name="Sniper", value="sniper"),
        app_commands.Choice(name="Dungeon Ticket (Tier 3)", value="ticket_t3"),
    ])
    async def sell(inter: discord.Interaction, item: app_commands.Choice[str], amount: int = 1):
        if not inter.guild:
            return await send_boxed(inter, "Shop — Sell", "Server only.", icon="🛍️", ephemeral=True)
        if amount <= 0:
            return await send_boxed(inter, "Shop — Sell", "Amount must be positive.", icon="🛍️", ephemeral=True)

        # Public reply (not ephemeral)
        try:
            await inter.response.defer(thinking=False)
        except Exception:
            pass

        await _shop_perform_sell(inter, item.value, amount)


# -------------------- Public API --------------------
def register(_bot: commands.Bot, _tree: app_commands.CommandTree) -> None:
    """
    Called from main.py to wire this feature up.
    """
    global bot, tree
    bot = _bot
    tree = _tree
    _bind_commands(_tree)
