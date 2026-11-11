"""discord.py client. DM in, DM out — the only user interface."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import discord
from discord.ext import tasks

from .. import config
from ..engine.gaps import GapSource
from ..engine.loop import NagEngine
from ..store.repo import Repo
from . import templates
from .commands import parse

log = logging.getLogger(__name__)


class NudgeBot(discord.Client):
    def __init__(self, repo: Repo, gap_source: GapSource, mirror=None) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.repo = repo
        self.engine = NagEngine(repo, gap_source, self.send_dm, mirror=mirror)
        self._recipient: discord.User | None = None
        self._ingest_queue: asyncio.Queue[Path] = asyncio.Queue()
        self._watcher = None
        self._consumer: asyncio.Task | None = None

    # --- outbound ------------------------------------------------------
    async def send_dm(self, text: str) -> None:
        user = await self.recipient()
        await user.send(text)

    async def recipient(self) -> discord.User:
        if self._recipient is None:
            self._recipient = await self.fetch_user(config.DISCORD_USER_ID)
        return self._recipient

    # --- lifecycle -----------------------------------------------------
    async def setup_hook(self) -> None:
        self.tick_task.start()
        self._consumer = asyncio.create_task(self._drain_ingest())
        self._start_watcher()

    # --- assignment folder watching -------------------------------------
    def _start_watcher(self) -> None:
        if not config.WATCH_ENABLED:
            return
        try:
            from ..ingest.watcher import AssignmentWatcher
        except ImportError:
            log.warning("watchdog not installed; folder ingestion is off")
            return
        loop = asyncio.get_running_loop()

        def on_folder(folder: Path) -> None:
            # Called from a watchdog thread — hop back onto the event loop.
            loop.call_soon_threadsafe(self._ingest_queue.put_nowait, folder)

        self._watcher = AssignmentWatcher(on_folder)
        self._watcher.start()

    async def _drain_ingest(self) -> None:
        """One folder at a time: decomposition is slow and the DMs must not race."""
        while True:
            folder = await self._ingest_queue.get()
            try:
                await self.engine.ingest(folder)
            except Exception:  # noqa: BLE001
                log.exception("ingest failed for %s", folder)
            finally:
                self._ingest_queue.task_done()

    async def close(self) -> None:
        if self._watcher is not None:
            self._watcher.stop()
        if self._consumer is not None:
            self._consumer.cancel()
        await super().close()

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
            await message.channel.send("Didn't parse that.\n" + templates.HELP)
            return
        try:
            reply = await self.engine.handle(command)
        except Exception:  # noqa: BLE001
            log.exception("command %s failed", command.name)
            await message.channel.send("That blew up — check the logs.")
            return
        if reply:
            await message.channel.send(reply)


def run(repo: Repo, gap_source: GapSource, mirror=None) -> None:
    if not config.DISCORD_TOKEN or not config.DISCORD_USER_ID:
        raise SystemExit(
            "Set NUDGE_DISCORD_TOKEN and NUDGE_DISCORD_USER_ID (see .env.example)."
        )
    NudgeBot(repo, gap_source, mirror=mirror).run(config.DISCORD_TOKEN, log_handler=None)
