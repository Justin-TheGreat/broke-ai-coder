"""Thin discord.py adapter. All logic lives in app/bot/commands.py and format.py."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any

import discord
from discord import app_commands

from app.bot.auth import authorize
from app.bot.backend import ControllerBackend
from app.bot.commands import CommandContext, run_command
from app.bot.format import format_event
from app.config.models import AppConfig
from app.config.secrets import resolve_secret
from app.redaction import SecretRedactor
from app.runtime.daemon import AgentController, ControllerEvent
from app.runtime.diagnostics import RuntimeProbes
from app.runtime.interfaces import ChatFrontend, NullFrontend

logger = logging.getLogger(__name__)

_MESSAGE_CACHE_MAX = 100
_STOP_TIMEOUT_S = 10.0


def create_frontend(
    config: AppConfig,
    *,
    redactor: SecretRedactor | None,
    environ: Mapping[str, str] | None = None,
    probes: RuntimeProbes | None = None,
) -> ChatFrontend:
    env_name = config.discord.bot_token_env
    if resolve_secret(env_name, environ) is None:
        logger.warning("discord frontend disabled: env %s is not set", env_name)
        return NullFrontend()
    return DiscordFrontend(config, redactor=redactor, environ=environ, probes=probes)


class _Client(discord.Client):
    def __init__(self, frontend: DiscordFrontend, *, intents: discord.Intents) -> None:
        super().__init__(intents=intents)
        self._frontend = frontend
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        for gid in self._frontend._config.discord.allowed_guild_ids:
            guild = discord.Object(id=gid)
            try:
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
            except Exception as e:
                logger.warning("discord command sync failed: %s", type(e).__name__)


class DiscordFrontend:
    def __init__(
        self,
        config: AppConfig,
        *,
        redactor: SecretRedactor | None,
        environ: Mapping[str, str] | None = None,
        probes: RuntimeProbes | None = None,
    ) -> None:
        self._config = config
        self._probes = probes
        self._redactor = redactor
        # A closure, not an attribute, so the environment (and the token in it) is never
        # reachable through repr()/vars() of this object.
        name = config.discord.bot_token_env
        self._resolve_token = lambda: resolve_secret(name, environ)
        self._client: Any = None
        self._backend: ControllerBackend | None = None
        self._task: asyncio.Task[None] | None = None
        self._messages: OrderedDict[str, Any] = OrderedDict()

    def __repr__(self) -> str:
        return "DiscordFrontend()"

    # ---- client ----------------------------------------------------

    def build_client(self) -> tuple[discord.Client, app_commands.CommandTree]:
        intents = discord.Intents.none()
        intents.guilds = True
        client = _Client(self, intents=intents)
        tree = client.tree

        @tree.command(name="code", description="Run a coding task with OpenCode")
        @app_commands.describe(
            prompt="What to do",
            project="Project name (default: the default project)",
            session="Continue an existing session (id from /sessions)",
        )
        async def code(
            interaction: discord.Interaction,
            prompt: app_commands.Range[str, 1, 4000],
            project: str | None = None,
            session: str | None = None,
        ) -> None:
            await self._handle(
                interaction, "code", {"prompt": prompt, "project": project, "session": session}
            )

        @tree.command(name="status", description="Show controller and OpenCode status")
        async def status(interaction: discord.Interaction) -> None:
            await self._handle(interaction, "status", {})

        @tree.command(name="usage", description="Show free quota, cooldowns and usage")
        async def usage(interaction: discord.Interaction) -> None:
            await self._handle(interaction, "usage", {})

        @tree.command(name="models", description="Show allowlisted and discovered models")
        async def models(interaction: discord.Interaction) -> None:
            await self._handle(interaction, "models", {})

        @tree.command(
            name="health", description="Controller diagnostics (daemon only; excludes OpenCode)"
        )
        async def health(interaction: discord.Interaction) -> None:
            await self._handle(interaction, "health", {})

        @tree.command(name="sessions", description="List recent sessions")
        async def sessions(interaction: discord.Interaction) -> None:
            await self._handle(interaction, "sessions", {})

        @tree.command(name="cancel", description="Cancel a task")
        @app_commands.describe(task="Task id (or unique prefix of at least 6 characters)")
        async def cancel(interaction: discord.Interaction, task: str) -> None:
            await self._handle(interaction, "cancel", {"task": task})

        return client, tree

    async def _handle(
        self, interaction: discord.Interaction, name: str, options: Mapping[str, str | None]
    ) -> None:
        ctx = CommandContext(
            user_id=interaction.user.id,
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
        )
        decision = authorize(
            self._config.discord,
            user_id=ctx.user_id,
            guild_id=ctx.guild_id,
            channel_id=ctx.channel_id,
        )
        if not decision.allowed:
            logger.warning(
                "discord auth denied command=%s user=%s reason=%s",
                name,
                ctx.user_id,
                decision.reason,
            )
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=name != "code", thinking=True)
        backend = self._backend
        if backend is None:
            await interaction.followup.send(
                "Controller is not ready.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        reply = await run_command(
            name,
            cfg=self._config.discord,
            backend=backend,
            ctx=ctx,
            redactor=self._redactor,
            options=options,
        )
        await interaction.followup.send(
            reply.content,
            ephemeral=reply.ephemeral,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # ---- lifecycle -------------------------------------------------

    async def start(self, controller: AgentController) -> None:
        token = self._resolve_token()
        if token is None:
            logger.warning("discord frontend disabled: token is not set")
            return
        self._backend = ControllerBackend(controller, self._config, probes=self._probes)
        client, _tree = self.build_client()
        self._client = client
        self._task = asyncio.create_task(client.start(token), name="discord-client")
        self._task.add_done_callback(self._on_done)

    @staticmethod
    def _on_done(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.warning("discord frontend stopped: %s", type(exc).__name__)

    async def stop(self) -> None:
        client, task = self._client, self._task
        self._client = None
        self._task = None
        if client is not None:
            try:
                await client.close()
            except Exception as e:
                logger.warning("discord close failed: %s", type(e).__name__)
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=_STOP_TIMEOUT_S)
            except (TimeoutError, asyncio.CancelledError):
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
            except Exception:
                pass  # already logged by the done callback

    # ---- events ----------------------------------------------------

    def _remember(self, key: str, message: Any) -> None:
        self._messages[key] = message
        self._messages.move_to_end(key)
        while len(self._messages) > _MESSAGE_CACHE_MAX:
            self._messages.popitem(last=False)

    async def on_event(self, event: ControllerEvent) -> None:
        try:
            text = format_event(event, redactor=self._redactor)
            channel_id = event.fields.get("channel_id")
            if text is None or channel_id is None:
                return
            client = self._client
            channel = client.get_channel(int(channel_id)) if client is not None else None
            if channel is None:
                logger.debug("discord channel not available for event %s", event.name)
                return
            mentions = discord.AllowedMentions.none()
            key = event.task_id or ""
            if event.name == "progress":
                existing = self._messages.get(key)
                if existing is not None:
                    self._messages.move_to_end(key)
                    await existing.edit(content=text, allowed_mentions=mentions)
                else:
                    message = await channel.send(text, allowed_mentions=mentions)
                    self._remember(key, message)
                return
            await channel.send(text, allowed_mentions=mentions)
            if event.name == "task_finished":
                self._messages.pop(key, None)
        except Exception as e:
            logger.warning("discord event delivery failed: %s", type(e).__name__)
