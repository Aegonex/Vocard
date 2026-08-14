"""MIT License

Copyright (c) 2023 - present Vocard Development

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

import aiohttp
import asyncio
import discord
import logging
import os
import signal
import sys
import update
import function as func

from aiohttp import web
from contextlib import suppress
from discord.ext import commands
from logging.handlers import TimedRotatingFileHandler
from voicelink import (
    Config,
    IPCClient,
    LangHandler,
    MongoDBHandler,
    NodePool,
    VoicelinkException,
)
from voicelink.web_dashboard import WebDashboard
from voicelink.utils import dispatch_message

DISCORD_SHUTDOWN_TIMEOUT = 5
RESOURCE_SHUTDOWN_TIMEOUT = 20


class Translator(discord.app_commands.Translator):
    MISSING_TRANSLATOR: dict[str, list[str]] = {}

    async def load(self):
        func.logger.info("Loaded Translator")
        
    async def unload(self):
        func.logger.info("Unload Translator")
        
    async def translate(
        self,
        string: discord.app_commands.locale_str,
        locale: discord.Locale,
        context: discord.app_commands.TranslationContext
    ) -> str | None:
        locale_key = str(locale).upper()
        local_translations = LangHandler._local_langs.get(locale_key)
        if not local_translations:
            return None

        translated_text = local_translations.get(string.message)
        if translated_text is not None:
            return translated_text

        missing = self.MISSING_TRANSLATOR.setdefault(locale_key, [])
        if string.message not in missing:
            missing.append(string.message)

        return None

class Vocard(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.ipc_client: IPCClient | None = None
        self.web_dashboard: WebDashboard | None = None
        self._health_runner: web.AppRunner | None = None
        self._vocard_closing = False

    def health_status(self) -> dict[str, bool | str]:
        """Return a deployment/readiness snapshot without exposing secrets."""
        discord_ready = (
            self.is_ready() and not self.is_closed() and not self._vocard_closing
        )
        mongodb_ready = MongoDBHandler.is_ready()
        lavalink_ready = any(node.is_available for node in NodePool().nodes.values())
        healthy = discord_ready and mongodb_ready and lavalink_ready
        return {
            "status": "ok" if healthy else "starting",
            "discord": discord_ready,
            "mongodb": mongodb_ready,
            "lavalink": lavalink_ready,
        }

    async def _health_check(self, _: web.Request) -> web.Response:
        await MongoDBHandler.ping(
            timeout_seconds=min(bot_config.dependency_connect_timeout, 5)
        )
        status = self.health_status()
        return web.json_response(status, status=200 if status["status"] == "ok" else 503)

    async def _start_health_server(self) -> None:
        port_value = os.getenv("PORT")
        if not port_value:
            func.logger.info("PORT is not set; the deployment health server is disabled.")
            return

        try:
            port = int(port_value)
        except ValueError as exc:
            raise ValueError("PORT must be a valid integer.") from exc
        if not 1 <= port <= 65535:
            raise ValueError("PORT must be between 1 and 65535.")

        app = web.Application(client_max_size=16 * 1024)
        app.router.add_get("/health", self._health_check)
        admin_password = (
            os.getenv("ADMIN_PASSWORD")
            or os.getenv("WEB_DASHBOARD_KEY", "")
        )
        self.web_dashboard = WebDashboard(
            self,
            admin_password,
            commands_enabled=bot_config.discord_commands_enabled,
        )
        self.web_dashboard.register(app)
        if not self.web_dashboard.configured:
            func.logger.warning(
                "ADMIN_PASSWORD (or legacy WEB_DASHBOARD_KEY) is missing; "
                "the control panel API is locked."
            )
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, host="0.0.0.0", port=port).start()
        except BaseException:
            await runner.cleanup()
            raise

        self._health_runner = runner
        func.logger.info(
            "Web control panel and health server listening on 0.0.0.0:%s", port
        )

    async def _connect_mongodb(self) -> None:
        for attempt in range(1, bot_config.dependency_startup_retries + 1):
            try:
                await MongoDBHandler.init(
                    bot_config.mongodb_url,
                    bot_config.mongodb_name,
                    timeout_seconds=bot_config.dependency_connect_timeout,
                )
                return
            except Exception as exc:
                func.logger.warning(
                    "MongoDB connection attempt %s/%s failed: %s",
                    attempt,
                    bot_config.dependency_startup_retries,
                    exc,
                )
                if attempt == bot_config.dependency_startup_retries:
                    raise RuntimeError(
                        "MongoDB did not become available during startup."
                    ) from exc
                await asyncio.sleep(bot_config.dependency_retry_delay)

    async def close(self) -> None:
        """Close the gateway promptly and bound all remaining resource cleanup."""
        if self._vocard_closing:
            return
        self._vocard_closing = True

        try:
            await asyncio.wait_for(
                super().close(), timeout=DISCORD_SHUTDOWN_TIMEOUT
            )
        except Exception as exc:
            func.logger.warning("Discord shutdown did not finish cleanly: %s", exc)

        cleanup_coroutines = []
        if self.ipc_client is not None:
            cleanup_coroutines.append(self.ipc_client.disconnect())

        cleanup_coroutines.extend(
            node.disconnect(remove_from_pool=True)
            for node in list(NodePool().nodes.values())
        )
        cleanup_coroutines.append(MongoDBHandler.close())

        if self._health_runner is not None:
            health_runner, self._health_runner = self._health_runner, None
            cleanup_coroutines.append(health_runner.cleanup())

        try:
            results = await asyncio.wait_for(
                asyncio.gather(*cleanup_coroutines, return_exceptions=True),
                timeout=RESOURCE_SHUTDOWN_TIMEOUT,
            )
        except TimeoutError:
            func.logger.warning(
                "Resource cleanup exceeded the %s-second shutdown budget.",
                RESOURCE_SHUTDOWN_TIMEOUT,
            )
        else:
            for result in results:
                if isinstance(result, Exception):
                    func.logger.warning("Resource cleanup failed: %s", result)

    async def on_message(self, message: discord.Message, /) -> None:
        # Ignore messages from bots or DMs
        if message.author.bot or not message.guild:
            return False

        # Web-only mode: every message-driven command path (prefix commands,
        # the bare-mention reply, and the music request channel) is disabled.
        if not bot_config.discord_commands_enabled:
            return

        # Check if the bot is directly mentioned
        if message.content.strip() == self.user.mention and not message.mention_everyone:
            prefix = await self.command_prefix(self, message)
            if not prefix:
                return await message.channel.send("I don't have a bot prefix set.")
            return await message.channel.send(f"My prefix is `{prefix}`")

        # Fetch guild settings and check if the mesage is in the music request channel
        settings = await MongoDBHandler.get_settings(message.guild.id)
        if settings and (request_channel := settings.get("music_request_channel")):
            if message.channel.id == request_channel.get("text_channel_id"):
                ctx = await self.get_context(message)    
                try:
                    if not ctx.prefix:
                        cmd = self.get_command("play")
                        if message.content:
                            await cmd(ctx, query=message.content)

                        elif message.attachments:
                            for attachment in message.attachments:
                                await cmd(ctx, query=attachment.url)

                except Exception as e:
                    await dispatch_message(ctx, str(e), ephemeral=True)

                finally:
                    await message.delete()

        await self.process_commands(message)

    async def setup_hook(self) -> None:
        # Start the Railway-compatible health endpoint before waiting for dependencies.
        await self._start_health_server()
        await self._setup_application()

    async def _setup_application(self) -> None:
        """Initialize required services within the deployment startup deadline."""
        # Connecting to MongoDB with bounded retries handles service startup ordering.
        await self._connect_mongodb()

        # Set translator
        await self.tree.set_translator(Translator())

        # Loading all the module in `cogs` folder
        failed_extensions: list[str] = []
        for module in sorted(os.listdir(func.ROOT_DIR + '/cogs')):
            if module.endswith('.py'):
                try:
                    await self.load_extension(f"cogs.{module[:-3]}")
                    func.logger.info(f"Loaded {module[:-3]}")
                except Exception:
                    failed_extensions.append(module[:-3])
                    func.logger.exception(
                        "Something went wrong while loading %s cog.", module[:-3]
                    )

        if failed_extensions:
            raise RuntimeError(
                "Required cogs failed to load: " + ", ".join(failed_extensions)
            )

        self.ipc_client = IPCClient(self, **bot_config.ipc_client)
        if bot_config.ipc_client.get("enable", False):
            for attempt in range(1, bot_config.dependency_startup_retries + 1):
                try:
                    await asyncio.wait_for(
                        self.ipc_client.connect(),
                        timeout=bot_config.dependency_connect_timeout,
                    )
                    if not self.ipc_client.is_connected:
                        raise ConnectionError("Dashboard connection did not become ready.")
                    break
                except Exception as exc:
                    func.logger.warning(
                        "Dashboard connection attempt %s/%s failed: %s",
                        attempt,
                        bot_config.dependency_startup_retries,
                        exc,
                    )
                    if attempt == bot_config.dependency_startup_retries:
                        raise RuntimeError(
                            "Dashboard IPC is enabled but unavailable."
                        ) from exc
                    await asyncio.sleep(bot_config.dependency_retry_delay)

        # Web-only mode: publish an empty command tree so slash commands
        # disappear from Discord instead of failing silently when invoked.
        if not bot_config.discord_commands_enabled:
            self.tree.clear_commands(guild=None)

        # Keep application commands current without writing runtime state to settings.json.
        if bot_config.sync_commands_on_startup:
            for attempt in range(1, bot_config.dependency_startup_retries + 1):
                try:
                    await asyncio.wait_for(
                        self.tree.sync(),
                        timeout=bot_config.dependency_connect_timeout,
                    )
                    break
                except (discord.HTTPException, TimeoutError) as exc:
                    func.logger.warning(
                        "Application command sync attempt %s/%s failed: %s",
                        attempt,
                        bot_config.dependency_startup_retries,
                        exc,
                    )
                    if attempt == bot_config.dependency_startup_retries:
                        raise RuntimeError(
                            "Application commands could not be synchronized."
                        ) from exc
                    await asyncio.sleep(bot_config.dependency_retry_delay)

            for locale_key, values in self.tree.translator.MISSING_TRANSLATOR.items():
                func.logger.warning(f'Missing translation for "{", ".join(values)}" in "{locale_key}"')
            self.tree.translator.MISSING_TRANSLATOR.clear()

    async def on_ready(self):
        func.logger.info("------------------")
        func.logger.info(f"Logging As {self.user}")
        func.logger.info(f"Bot ID: {self.user.id}")
        func.logger.info("------------------")
        func.logger.info(f"Vocard Version: {update.__version__}")
        func.logger.info(f"Discord Version: {discord.__version__}")
        func.logger.info(f"Python Version: {sys.version}")
        func.logger.info("------------------")

        bot_config.client_id = self.user.id
        LangHandler._local_langs.clear()

    async def on_command_error(self, ctx: commands.Context, exception, /) -> None:
        error = getattr(exception, 'original', exception)
        if ctx.interaction:
            error = getattr(error, 'original', error)

        if isinstance(error, (commands.CommandNotFound, aiohttp.client_exceptions.ClientOSError, discord.errors.NotFound)):
            return

        elif isinstance(error, (commands.CommandOnCooldown, commands.MissingPermissions, commands.RangeError, commands.BadArgument)):
            pass

        elif isinstance(error, (commands.MissingRequiredArgument, commands.MissingRequiredAttachment)):
            command = f"{ctx.prefix}" + (f"{ctx.command.parent.qualified_name} " if ctx.command.parent else "") + f"{ctx.command.name} {ctx.command.signature}"
            position = command.find(f"<{ctx.current_parameter.name}>") + 1
            description = f"**Correct Usage:**\n```{command}\n" + " " * position + "^" * len(ctx.current_parameter.name) + "```\n"
            if ctx.command.aliases:
                description += f"**Aliases:**\n`{', '.join([f'{ctx.prefix}{alias}' for alias in ctx.command.aliases])}`\n\n"
            description += f"**Description:**\n{ctx.command.help}\n\u200b"

            embed = discord.Embed(description=description, color=bot_config.embed_color)
            embed.set_footer(icon_url=ctx.me.display_avatar.url, text=f"More Help: {bot_config.invite_link}")
            return await ctx.reply(embed=embed)

        elif (
            isinstance(error, discord.app_commands.TransformerError)
            and ctx.command is not None
            and ctx.command.qualified_name == "connect"
            and error.type is discord.AppCommandOptionType.channel
        ):
            error = await Lang_handler.get_lang(
                ctx.guild.id,
                "voice.connection.noChannel",
            )

        elif not issubclass(error.__class__, VoicelinkException):
            error = await Lang_handler.get_lang(ctx.guild.id, "common.errors.unknown") + bot_config.invite_link
            func.logger.error(f"An unexpected error occurred in the {ctx.command.name} command on the {ctx.guild.name}({ctx.guild.id}).", exc_info=exception)

        try:
            return await ctx.reply(error, ephemeral=True)
        except discord.HTTPException:
            pass

class CommandCheck(discord.app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        if interaction.type == discord.InteractionType.application_command:
            # Web-only mode: block commands that Discord may still have cached.
            if not bot_config.discord_commands_enabled:
                await interaction.response.send_message(
                    "Commands are disabled on this bot. Use the web control panel instead.",
                    ephemeral=True,
                )
                return False
            if not interaction.guild:
                await interaction.response.send_message("This command can only be used in guilds!")
                return False

            channel_perm = interaction.channel.permissions_for(interaction.guild.me)
            if not channel_perm.read_messages or not channel_perm.send_messages:
                await interaction.response.send_message("I don't have permission to read or send messages in this channel.", ephemeral=True)
                return False
            
        return True

async def get_prefix(bot: commands.Bot, message: discord.Message) -> str:
    settings = await MongoDBHandler.get_settings(message.guild.id)
    prefix = settings.get("prefix", bot_config.bot_prefix)
    return prefix if prefix is not None else ""

# Load bundled defaults, optional settings.json overrides, and environment variables.
bot_config = Config.load()
bot_config.validate()
bot_config.version = update.__version__
Lang_handler = LangHandler.init()

LOG_SETTINGS = bot_config.logging
if (LOG_FILE := LOG_SETTINGS.get("file", {})).get("enable", True):
    log_path = os.path.abspath(LOG_FILE.get("path", "./logs"))
    if not os.path.exists(log_path):
        os.makedirs(log_path)

    file_handler = TimedRotatingFileHandler(filename=f'{log_path}/vocard.log', encoding="utf-8", backupCount=LOG_SETTINGS.get("max_history", 30), when="d")
    file_handler.namer = lambda name: name.replace(".log", "") + ".log"
    file_handler.setFormatter(logging.Formatter('{asctime} [{levelname:<8}] {name}: {message}', '%Y-%m-%d %H:%M:%S', style='{'))
    logging.getLogger().addHandler(file_handler)

for log_name, log_level in LOG_SETTINGS.get("level", {}).items():
    _logger = logging.getLogger(log_name)
    _logger.setLevel(log_level)

# Setup the bot object
intents = discord.Intents.default()
intents.message_content = False if bot_config.bot_prefix is None else True
intents.members = bot_config.ipc_client.get("enable", False)
intents.voice_states = True
intents.presences = False

bot = Vocard(
    command_prefix=get_prefix,
    help_command=None,
    tree_cls=CommandCheck,
    chunk_guilds_at_startup=False,
    activity=discord.Activity(type=discord.ActivityType.listening, name="Starting..."),
    case_insensitive=True,
    intents=intents
)


async def run_bot(client: commands.Bot, token: str) -> None:
    """Run Discord until completion or an orchestrator shutdown signal arrives."""
    loop = asyncio.get_running_loop()
    shutdown_requested = asyncio.Event()
    registered_signals: list[signal.Signals] = []

    def request_shutdown(received_signal: signal.Signals) -> None:
        if not shutdown_requested.is_set():
            func.logger.info("Received %s; shutting down gracefully.", received_signal.name)
            shutdown_requested.set()

    for received_signal in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(
                received_signal, request_shutdown, received_signal
            )
        except (NotImplementedError, RuntimeError):
            continue
        registered_signals.append(received_signal)

    try:
        async with client:
            client_task = asyncio.create_task(
                client.start(token, reconnect=True), name="discord-client"
            )
            ready_task = asyncio.create_task(
                client.wait_until_ready(), name="discord-ready"
            )
            shutdown_task = asyncio.create_task(
                shutdown_requested.wait(), name="shutdown-signal"
            )
            try:
                done, _ = await asyncio.wait(
                    {client_task, ready_task, shutdown_task},
                    timeout=bot_config.startup_timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    raise RuntimeError(
                        f"Startup exceeded the {bot_config.startup_timeout}-second "
                        "deadline before Discord became ready."
                    )
                if client_task in done:
                    await client_task
                elif shutdown_task in done:
                    client_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await client_task
                else:
                    await ready_task
                    done, _ = await asyncio.wait(
                        {client_task, shutdown_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if client_task in done:
                        await client_task
                    else:
                        client_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await client_task
            finally:
                for task in (ready_task, shutdown_task, client_task):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(
                    ready_task,
                    shutdown_task,
                    client_task,
                    return_exceptions=True,
                )
    finally:
        for received_signal in registered_signals:
            loop.remove_signal_handler(received_signal)


if __name__ == "__main__":
    if bot_config.check_for_updates_on_startup:
        update.check_version(with_msg=True)
    discord.utils.setup_logging(root=True)
    asyncio.run(run_bot(bot, bot_config.token))
