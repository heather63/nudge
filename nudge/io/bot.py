"""discord.py client. DM in, DM out — the only user interface.

Every offer carries four buttons. They are a persistent view (fixed custom IDs,
no timeout), so they keep working after the bot restarts.
"""
from __future__ import annotations

import logging

import discord
from discord.ext import tasks

from .. import config
from ..engine.loop import NagEngine
from . import templates
from .commands import Command, parse

log = logging.getLogger(__name__)

# Typed replies that answer the offer, so its buttons should go.
_ANSWERS = {"done", "log_minutes", "set_percent", "not_now", "snooze",
            "something_else", "busy"}


class OfferButtons(discord.ui.View):
    def __init__(self, bot: NudgeBot) -> None:
        super().__init__(timeout=None)
        self.bot = bot

    async def _press(self, interaction: discord.Interaction, command: Command) -> None:
        if interaction.user.id != config.DISCORD_USER_ID:
            await interaction.response.defer()
            return
        await interaction.response.edit_message(view=None)
        reply = await self.bot.run_command(command)
        if reply:
            await interaction.followup.send(reply)

    @discord.ui.button(label="Did it", style=discord.ButtonStyle.success,
                       custom_id="nudge:done")
    async def done(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._press(interaction, Command("done"))

    @discord.ui.button(label="Something else", style=discord.ButtonStyle.primary,
                       custom_id="nudge:other")
    async def other(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._press(interaction, Command("something_else"))

    @discord.ui.button(label="Not now", style=discord.ButtonStyle.secondary,
                       custom_id="nudge:not_now")
    async def not_now(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._press(interaction, Command("not_now"))

    @discord.ui.button(label="Snooze till tomorrow", style=discord.ButtonStyle.secondary,
                       custom_id="nudge:snooze")
    async def snooze(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._press(interaction, Command("snooze", "tomorrow"))


class NudgeBot(discord.Client):
    def __init__(self, build_engine) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.engine: NagEngine = build_engine(self.send_dm)
        self._recipient: discord.User | None = None
        self._last_offer: discord.Message | None = None

    # --- outbound ------------------------------------------------------
    async def send_dm(self, text: str, *, offer: bool = False) -> None:
        if self._recipient is None:
            self._recipient = await self.fetch_user(config.DISCORD_USER_ID)
        if not offer:
            await self._recipient.send(text)
            return
        await self._clear_buttons()
        self._last_offer = await self._recipient.send(text, view=OfferButtons(self))

    async def _clear_buttons(self) -> None:
        """Only the latest offer keeps its buttons, so an old one can't be pressed."""
        if self._last_offer is None:
            return
        try:
            await self._last_offer.edit(view=None)
        except discord.HTTPException:
            pass
        self._last_offer = None

    async def run_command(self, command: Command) -> str | None:
        try:
            return await self.engine.handle(command)
        except Exception:  # noqa: BLE001
            log.exception("command %s failed", command.name)
            return "That blew up — check the logs."

    # --- lifecycle -----------------------------------------------------
    async def setup_hook(self) -> None:
        self.add_view(OfferButtons(self))
        self.tick_task.start()

    async def on_ready(self) -> None:
        log.info("connected as %s, nagging user %s", self.user, config.DISCORD_USER_ID)

    @tasks.loop(minutes=config.TICK_INTERVAL)
    async def tick_task(self) -> None:
        try:
            await self.engine.tick()
        except Exception:  # noqa: BLE001 - a bad tick must not kill the loop
            log.exception("tick failed")

    @tick_task.before_loop
    async def _before_tick(self) -> None:
        await self.wait_until_ready()

    # --- inbound -------------------------------------------------------
    async def on_message(self, message: discord.Message) -> None:
        if message.author.id != config.DISCORD_USER_ID:
            return
        if not isinstance(message.channel, discord.DMChannel):
            return
        command = parse(message.content)
        if command is None:
            await message.channel.send("Didn't catch that.\n" + templates.HELP)
            return
        if command.name in _ANSWERS:
            await self._clear_buttons()
        reply = await self.run_command(command)
        if reply:
            await message.channel.send(reply)


def run(build_engine) -> None:
    if not config.DISCORD_TOKEN or not config.DISCORD_USER_ID:
        raise SystemExit(
            "Set NUDGE_DISCORD_TOKEN and NUDGE_DISCORD_USER_ID (see .env.example)."
        )
    NudgeBot(build_engine).run(config.DISCORD_TOKEN, log_handler=None)
