from __future__ import annotations

import discord
from discord import app_commands
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    import bot as bot_module

bot: bot_module | None = None  # will be assigned in setup

# -------------------- Economy / items --------------------
PRICE_STONE = 1
PRICE_BADGE = 5
PRICE_CHICK = 2
PRICE_SNIPER = 100
SNIPER_SNIPE_COST = 1  # price per snipe shot

# NEW: Tier 3 Dungeon Ticket is purchasable (T2/T1 are loot-only)
SHOP_ITEMS: dict[str, dict] = {}

def _init_shop_items():
    global SHOP_ITEMS
    SHOP_ITEMS = {
        "stone": {
            "label": f"{bot.EMO_STONE()} Stone",
            "price": PRICE_STONE,
            "desc": "Throw with /stone. 49% drop chance per stone (bulk supported).",
        },
        "badge": {
            "label": f"{bot.EMO_BADGE()} Bounty Hunter Badge",
            "price": PRICE_BADGE,
            "desc": "Grants the Bounty Hunter role. Bounties ping that role.",
        },
        "chicken": {
            "label": f"{bot.EMO_CHICKEN()} Fried Chicken",
            "price": PRICE_CHICK,
            "desc": "Use /eat to gain 1h immunity from stones.",
        },
        "sniper": {
            "label": f"{bot.EMO_SNIPER()} Sniper",
            "price": PRICE_SNIPER,
            "desc": f"Lets you `/snipe` other players' solo Wordle (costs {SNIPER_SNIPE_COST} shekel per shot). One-time purchase.",
        },
        "ticket_t3": {
            "label": f"{bot.EMO_DUNGEON()} Dungeon Ticket (Tier 3)",
            "price": 5,
            "desc": "Opens a Tier 3 Worldle Dungeon. Use `/worldle_dungeon tier:Tier 3`.",
        },
    }

# Controls shop item order & /buy choices
SHOP_ORDER = ["stone", "badge", "chicken", "sniper", "ticket_t3"]

SHOP_CHOICES = [
    app_commands.Choice(name="Stone", value="stone"),
    app_commands.Choice(name="Bounty Hunter Badge", value="badge"),
    app_commands.Choice(name="Fried Chicken", value="chicken"),
    app_commands.Choice(name="Sniper", value="sniper"),
    app_commands.Choice(name="Dungeon Ticket (Tier 3)", value="ticket_t3"),
]


async def _build_shop_embed(gid: int, uid: int) -> discord.Embed:
    bal = await bot.get_balance(gid, uid)
    shek = bot.EMO_SHEKEL()
    lines = [
        "🛒 **Shop** — buy with the buttons, or sell with the **💸 Sell** button (or `/sell`).",
        "",
        f"**Your balance:** **{bal} {shek}**",
        "",
    ]
    for key in SHOP_ORDER:
        item = SHOP_ITEMS[key]
        price = item["price"]
        lines.append(
            f"• **{item['label']}** — {price} {shek}{'' if price==1 else 's'}\n  _{item['desc']}_"
        )
    lines.append("\nTip: For bulk buying via slash command, use `/buy item:{name} amount:{n}`.")
    return bot.make_panel("Shop", "\n".join(lines), icon="🛒")


async def _build_sell_embed(gid: int, uid: int, owned: list[dict]) -> discord.Embed:
    bal = await bot.get_balance(gid, uid)
    shek = bot.EMO_SHEKEL()

    if not owned:
        desc = f"**Your balance:** **{bal} {shek}**\n\nYou currently have **nothing** you can sell."
        return bot.make_panel("Sell Items", desc, icon="🛍️")

    lines = [f"**Your balance:** **{bal} {shek}**", "", "You can sell:"]
    for it in owned:
        lines.append(
            f"• {it['emoji']} **{it['label']}** — you own **{it['count']}** · sell price **{it['price_each']} {shek}** each"
        )

    return bot.make_panel(
        title="Sell Items",
        description="Pick an item from the selector below:",
        fields=[("Details", "\n".join(lines), False)],
        icon="🛍️",
    )


async def _get_owned_sellables(gid: int, uid: int) -> list[dict]:
    stones = await bot.get_stones(gid, uid)
    badge = await bot.get_badge(gid, uid)
    chickens = await bot.get_chickens(gid, uid)
    sniper = await bot.get_sniper(gid, uid)
    t3 = await bot.get_dungeon_tickets_t3(gid, uid)

    catalog = []

    def _add(key: str, count: int, label: str, price_each: int, emoji: str):
        if count and count > 0:
            catalog.append({
                "key": key,
                "label": label,
                "count": int(count),
                "price_each": int(price_each),
                "emoji": emoji,
            })

    _add("stone", stones, "Stone", SHOP_ITEMS["stone"]["price"], bot.EMO_STONE())
    _add("badge", badge, "Bounty Hunter Badge", SHOP_ITEMS["badge"]["price"], bot.EMO_BADGE())
    _add("chicken", chickens, "Fried Chicken", SHOP_ITEMS["chicken"]["price"], bot.EMO_CHICKEN())
    _add("sniper", sniper, "Sniper", SHOP_ITEMS["sniper"]["price"], bot.EMO_SNIPER())
    _add("ticket_t3", t3, "Dungeon Ticket (Tier 3)", SHOP_ITEMS["ticket_t3"]["price"], bot.EMO_GATE_SCROLL(3))
    return catalog


async def _shop_perform_sell(i: discord.Interaction, key: str, qty: int):
    """Performs a SELL and replies PUBLICLY (non-ephemeral) on both success and errors.
       Mirrors the logic of the /sell command, but as a reusable helper for UI.
    """

    async def _send(msg: str):
        return await bot.send_boxed(i, "Shop — Sell", msg, icon="🛍️", ephemeral=False)

    if not i.guild or not i.channel:
        return await _send("Run this in a server.")

    gid, uid, cid = i.guild.id, i.user.id, i.channel.id
    key = str(key).strip().lower()

    if qty <= 0:
        return await _send("Quantity must be at least 1.")

    # Stones
    if key == "stone":
        have = await bot.get_stones(gid, uid)
        if have < qty:
            return await _send("You don't have that many stones to sell.")
        await bot.change_stones(gid, uid, -qty)
        refund = SHOP_ITEMS["stone"]["price"] * qty
        await bot.change_balance(gid, uid, refund, announce_channel_id=cid)
        bal = await bot.get_balance(gid, uid)
        return await _send(f"Sold **{qty}× {bot.EMO_STONE()} Stone** for **{refund} {bot.EMO_SHEKEL()}**. New balance: **{bal}**.")

    # Badge (one-time)
    if key == "badge":
        have = await bot.get_badge(gid, uid)
        if have < qty:
            return await _send("You don't have that many badges to sell.")
        await bot.change_badge(gid, uid, -qty)
        refund = SHOP_ITEMS["badge"]["price"] * qty
        await bot.change_balance(gid, uid, refund, announce_channel_id=cid)
        bal = await bot.get_balance(gid, uid)
        return await _send(f"Sold **{qty}× {bot.EMO_BADGE()} Badge** for **{refund} {bot.EMO_SHEKEL()}**. New balance: **{bal}**.")

    # Chicken
    if key == "chicken":
        have = await bot.get_chickens(gid, uid)
        if have < qty:
            return await _send("You don't have that many chickens to sell.")
        await bot.change_chickens(gid, uid, -qty)
        refund = SHOP_ITEMS["chicken"]["price"] * qty
        await bot.change_balance(gid, uid, refund, announce_channel_id=cid)
        bal = await bot.get_balance(gid, uid)
        return await _send(f"Sold **{qty}× {bot.EMO_CHICKEN()} Chicken** for **{refund} {bot.EMO_SHEKEL()}**. New balance: **{bal}**.")

    # Sniper
    if key == "sniper":
        have = await bot.get_sniper(gid, uid)
        if have < qty:
            return await _send("You don't have that many snipers to sell.")
        await bot.change_sniper(gid, uid, -qty)
        refund = SHOP_ITEMS["sniper"]["price"] * qty
        await bot.change_balance(gid, uid, refund, announce_channel_id=cid)
        bal = await bot.get_balance(gid, uid)
        return await _send(f"Sold **{qty}× {bot.EMO_SNIPER()} Sniper** for **{refund} {bot.EMO_SHEKEL()}**. New balance: **{bal}**.")

    # Tier-3 Ticket
    if key == "ticket_t3":
        have = await bot.get_dungeon_tickets_t3(gid, uid)
        if have < qty:
            return await _send("You don't have that many Tier-3 tickets to sell.")
        await bot.change_dungeon_tickets_t3(gid, uid, -qty)
        refund = SHOP_ITEMS["ticket_t3"]["price"] * qty
        await bot.change_balance(gid, uid, refund, announce_channel_id=cid)
        bal = await bot.get_balance(gid, uid)
        return await _send(f"Sold **{qty}× {bot.EMO_GATE_SCROLL(3)} Tier-3 Ticket** for **{refund} {bot.EMO_SHEKEL()}**. New balance: **{bal}**.")

    return await _send("Can't sell that item here.")


async def _shop_perform_buy(i: discord.Interaction, key: str, qty: int, *, _from_modal: bool = False):
    """
    Performs the shop purchase and replies:
      • PUBLIC message on success
      • Ephemeral message on validation / balance errors

    Works whether called from a slash command, a button, or a modal (deferred).
    """

    async def _send(msg: str, *, ephemeral: bool = False):
        return await bot.send_boxed(
            i,
            title="Shop",
            description=msg,
            icon="🛒",
            ephemeral=ephemeral,
        )

    if not i.guild or not i.channel:
        return await _send("Run this in a server.", ephemeral=True)

    gid, cid, uid = i.guild.id, i.channel.id, i.user.id

    # Canonical catalog for buy prices (must match your SHOP_ITEMS)
    CATALOG = {
        "stone": {"label": "Stone", "price": 1},
        "badge": {"label": "Bounty Hunter Badge", "price": 5},
        "chicken": {"label": "Fried Chicken", "price": 2},
        "sniper": {"label": "Sniper", "price": 100},
        "ticket_t3": {"label": "Dungeon Ticket (Tier 3)", "price": 5},
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
        return await bot.get_balance(gid, uid_)

    async def _add_balance(uid_: int, delta: int) -> None:
        await bot.change_balance(gid, uid_, delta, announce_channel_id=cid)

    async def _inv_add(uid_: int, item_key: str, amount: int) -> None:
        if item_key == "stone":
            await bot.change_stones(gid, uid_, amount)
        elif item_key == "chicken":
            await bot.change_chickens(gid, uid_, amount)
        elif item_key == "badge":
            if await bot.get_badge(gid, uid_) >= 1:
                raise RuntimeError("You already own the Bounty Hunter Badge.")
            await bot.set_badge(gid, uid_, 1)
            try:
                rid = await bot.ensure_bounty_role(i.guild)
                role = i.guild.get_role(rid) if rid else None
                member = i.guild.get_member(uid_) or await i.guild.fetch_member(uid_)
                if role and member and bot.bot_can_manage_role(i.guild, role):
                    await member.add_roles(role, reason="Bought Bounty Hunter Badge")
            except Exception as e:
                bot.log.warning(f"grant bounty role failed: {e}")
        elif item_key == "sniper":
            if await bot.get_sniper(gid, uid_) >= 1:
                raise RuntimeError("You already own the Sniper.")
            await bot.set_sniper(gid, uid_, 1)
        elif item_key == "ticket_t3":
            await bot.change_dungeon_tickets_t3(gid, uid_, amount)
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
        try:
            await _add_balance(uid, +cost)
        except Exception:
            pass
        return await _send(str(e), ephemeral=True)

    new_balance = await _get_balance(uid)
    label = item["label"]

    # PUBLIC success message
    await _send(
        f"**{i.user.mention}** bought **{qty}× {label}** for **{cost}** {bot.EMO_SHEKEL()}" ""
        f". New balance: **{new_balance}**.",
        ephemeral=False,
    )


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
            return await bot.send_boxed(
                interaction, "Shop", "This shop view isn’t yours. Use **/shop** to open your own.", icon="🛒"
            )

        try:
            qty = int(str(self.amount.value).strip())
        except ValueError:
            return await bot.send_boxed(interaction, "Shop", "Enter a whole number.", icon="🛒")
        if qty <= 0:
            return await bot.send_boxed(interaction, "Shop", "Enter a quantity greater than **0**.", icon="🛒")

        try:
            await interaction.response.defer(thinking=False)
        except Exception:
            pass

        await _shop_perform_buy(interaction, self.key, qty)

        try:
            gid, uid = interaction.guild.id, interaction.user.id
            emb = await _build_shop_embed(gid, uid)

            channel = interaction.client.get_channel(self.panel_channel_id) or await interaction.client.fetch_channel(self.panel_channel_id)
            msg = await channel.fetch_message(self.panel_message_id)

            view = ShopView(interaction)
            view.message = msg
            await msg.edit(embed=emb, view=view)
        except Exception:
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

        hint = f"You own {self.max_qty}. Price each: {self.price_each} {bot.EMO_SHEKEL()}."
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
            return await bot.send_boxed(interaction, "Shop — Sell", "This sell panel isn’t yours. Use **/shop** to open your own.", icon="🛍️")

        try:
            qty = int(str(self.amount.value).strip())
        except ValueError:
            return await bot.send_boxed(interaction, "Shop — Sell", "Enter a whole number.", icon="🛍️")
        if qty <= 0 or qty > self.max_qty:
            return await bot.send_boxed(interaction, "Shop — Sell", f"Enter **1–{self.max_qty}**.", icon="🛍️")

        try:
            await interaction.response.defer(thinking=False)
        except Exception:
            pass

        await _shop_perform_sell(interaction, self.key, qty)

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
        self.message: Optional[discord.Message] = None

        def add_buy_btn(key: str, label: str, emoji: str, style: discord.ButtonStyle):
            btn = discord.ui.Button(label=label, emoji=emoji, style=style)

            async def _cb(i: discord.Interaction, _key=key, _label=label):
                if i.user.id != self.owner_id:
                    return await bot.send_boxed(i, "Shop", "This shop view isn’t yours. Use **/shop** to open your own.", icon="🛒")
                await i.response.send_modal(
                    QuantityModal(
                        _key,
                        _label,
                        self.owner_id,
                        panel_channel_id=i.channel.id,
                        panel_message_id=self.message.id if self.message else i.message.id,
                    )
                )

            btn.callback = _cb
            self.add_item(btn)

        add_buy_btn("stone", "Stone", bot.EMO_STONE(), discord.ButtonStyle.primary)
        add_buy_btn("badge", "Badge", bot.EMO_BADGE(), discord.ButtonStyle.secondary)
        add_buy_btn("chicken", "Chicken", bot.EMO_CHICKEN(), discord.ButtonStyle.secondary)
        add_buy_btn("sniper", "Sniper", bot.EMO_SNIPER(), discord.ButtonStyle.secondary)
        add_buy_btn("ticket_t3", "T3 Ticket", bot.EMO_GATE_SCROLL(3), discord.ButtonStyle.success)

        sell_btn = discord.ui.Button(label="Sell", emoji="💸", style=discord.ButtonStyle.danger)

        async def _sell_cb(i: discord.Interaction):
            if i.user.id != self.owner_id:
                return await bot.send_boxed(i, "Shop — Sell", "This sell panel isn’t yours. Use **/shop** to open your own.", icon="🛍️")
            owned = await _get_owned_sellables(i.guild.id, i.user.id)
            emb = await _build_sell_embed(i.guild.id, i.user.id, owned)
            view = SellMenuView(i, owned)
            view.message = self.message
            await i.response.edit_message(embed=emb, view=view)

        sell_btn.callback = _sell_cb
        self.add_item(sell_btn)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        try:
            if self.message:
                await self.message.edit(view=self)
        except Exception:
            pass


class SellMenuView(discord.ui.View):
    def __init__(self, inter: discord.Interaction, owned: list[dict], *, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.owner_id = inter.user.id
        self.guild_id = inter.guild.id
        self.channel_id = inter.channel.id
        self.message: Optional[discord.Message] = None
        self.owned = owned

        options = []
        for item in owned:
            label = f"{item['emoji']} {item['label']}"
            desc = f"Owned: {item['count']} • Sell price: {item['price_each']} each"
            options.append(discord.SelectOption(label=label, value=item["key"], description=desc))

        select = discord.ui.Select(placeholder="Pick an item to sell…", options=options)

        async def _on_pick(i: discord.Interaction):
            if i.user.id != self.owner_id:
                return await bot.send_boxed(i, "Shop — Sell", "This sell panel isn’t yours. Use **/shop** to open your own.", icon="🛍️")

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

        select.callback = _on_pick
        self.add_item(select)

        back_btn = discord.ui.Button(label="Shop", emoji="🛒", style=discord.ButtonStyle.primary)

        async def _back(i: discord.Interaction):
            if i.user.id != self.owner_id:
                return await bot.send_boxed(i, "Shop", "This shop view isn’t yours. Use **/shop** to open your own.", icon="🛒")
            emb = await _build_shop_embed(self.guild_id, self.owner_id)
            view = ShopView(i)
            view.message = self.message
            await i.response.edit_message(embed=emb, view=view)

        back_btn.callback = _back
        self.add_item(back_btn)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        try:
            if self.message:
                await self.message.edit(view=self)
        except Exception:
            pass


@app_commands.describe(item="Item", amount="How many")
@app_commands.choices(item=SHOP_CHOICES)
async def buy(inter: discord.Interaction, item: app_commands.Choice[str], amount: int):
    if not await bot.guard_worldler_inter(inter):
        return
    await _shop_perform_buy(inter, item.value, amount)


async def shop(inter: discord.Interaction):
    if not await bot.guard_worldler_inter(inter):
        return

    emb = await _build_shop_embed(inter.guild.id, inter.user.id)
    view = ShopView(inter)
    await inter.response.send_message(embed=emb, view=view)

    try:
        msg = await inter.original_response()
        view.message = msg
    except Exception:
        pass


@app_commands.describe(item="Item", amount="How many")
@app_commands.choices(item=SHOP_CHOICES)
async def sell(inter: discord.Interaction, item: app_commands.Choice[str], amount: int = 1):
    if not await bot.guard_worldler_inter(inter):
        return
    if not inter.guild:
        return await bot.send_boxed(inter, "Shop — Sell", "Server only.", icon="🛍️", ephemeral=False)
    if amount <= 0:
        return await bot.send_boxed(inter, "Shop — Sell", "Amount must be positive.", icon="🛍️", ephemeral=False)

    try:
        await inter.response.defer(thinking=False)
    except Exception:
        pass

    await _shop_perform_sell(inter, item.value, amount)


def setup(tree: app_commands.CommandTree, bot_module):
    global bot
    bot = bot_module
    _init_shop_items()
    tree.command(name="shop", description="Show the shop.")(shop)
    tree.command(name="buy", description="Buy from the shop.")(buy)
    tree.command(name="sell", description="Sell items back to the shop for the same price.")(sell)
