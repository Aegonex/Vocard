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

import os
import asyncio
import discord
import voicelink
import function as func

from contextlib import suppress
from discord.ext import commands

from voicelink import MongoDBHandler, Config
from voicelink.utils import TempCtx

class Listeners(commands.Cog):
    """Music Cog."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.voicelink = voicelink.NodePool()
        self._restore_task: asyncio.Task | None = None
        self._node_supervisors: dict[str, asyncio.Task] = {}

    async def cog_load(self) -> None:
        await self.start_nodes()
        self._restore_task = asyncio.create_task(
            self.restore_last_session_players(), name="restore-last-session"
        )

    async def cog_unload(self) -> None:
        supervisor_tasks = list(self._node_supervisors.values())
        self._node_supervisors.clear()
        for task in supervisor_tasks:
            task.cancel()
        if supervisor_tasks:
            await asyncio.gather(*supervisor_tasks, return_exceptions=True)

        if self._restore_task is not None:
            self._restore_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._restore_task
            self._restore_task = None

        for node in list(self.voicelink.nodes.values()):
            with suppress(Exception):
                await node.disconnect(remove_from_pool=True)
        
    async def start_nodes(self) -> None:
        """Connect configured nodes with bounded retries during startup."""
        config = Config()
        pending = {identifier: node.copy() for identifier, node in config.nodes.items()}
        errors: dict[str, str] = {}

        for attempt in range(1, config.dependency_startup_retries + 1):
            pending_items = list(pending.items())
            results = await asyncio.gather(
                *(
                    self._connect_node(identifier, node_config, config)
                    for identifier, node_config in pending_items
                ),
                return_exceptions=True,
            )
            for (identifier, _), result in zip(pending_items, results):
                if isinstance(result, Exception):
                    exc = result
                    errors[identifier] = str(exc)
                    func.logger.warning(
                        "Lavalink node [%s] connection attempt %s/%s failed: %s",
                        identifier,
                        attempt,
                        config.dependency_startup_retries,
                        exc,
                    )
                else:
                    pending.pop(identifier, None)
                    errors.pop(identifier, None)

            if not pending:
                break
            if attempt < config.dependency_startup_retries:
                await asyncio.sleep(config.dependency_retry_delay)

        available_nodes = [
            node for node in self.voicelink.nodes.values() if node.is_available
        ]
        if not available_nodes:
            details = "; ".join(
                f"{identifier}: {reason}" for identifier, reason in errors.items()
            )
            raise RuntimeError(
                "No Lavalink node became available during startup"
                + (f" ({details})" if details else "")
            )

        if pending:
            func.logger.warning(
                "Bot is starting with %s Lavalink node(s); unavailable nodes: %s",
                len(available_nodes),
                ", ".join(sorted(pending)),
            )
            for identifier, node_config in pending.items():
                self._node_supervisors[identifier] = asyncio.create_task(
                    self._supervise_node(identifier, node_config, config),
                    name=f"lavalink-supervisor-{identifier}",
                )

    async def _connect_node(self, identifier: str, node_config: dict, config) -> None:
        existing = self.voicelink.nodes.get(identifier)
        connect = (
            existing.connect()
            if existing is not None
            else self.voicelink.create_node(
                bot=self.bot,
                connect_timeout=config.dependency_connect_timeout,
                **node_config,
            )
        )
        await asyncio.wait_for(connect, timeout=config.dependency_connect_timeout)

        node = self.voicelink.nodes.get(identifier)
        if node is None or not node.is_available:
            raise ConnectionError(f"Lavalink node [{identifier}] is not ready.")

    async def _supervise_node(
        self, identifier: str, node_config: dict, config
    ) -> None:
        """Keep retrying a secondary node that was unavailable at startup."""
        retry_delay = max(config.dependency_retry_delay, 5)
        while True:
            try:
                await self._connect_node(identifier, node_config, config)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                func.logger.warning(
                    "Lavalink node [%s] is still unavailable: %s; retrying in %ss.",
                    identifier,
                    exc,
                    retry_delay,
                )
                await asyncio.sleep(retry_delay)
            else:
                func.logger.info(
                    "Lavalink node [%s] became available in the background.",
                    identifier,
                )
                return

    async def restore_last_session_players(self) -> None:
        """Re-establish connections for players from the last session."""
        await self.bot.wait_until_ready()
        players = func.open_json(Config.LAST_SESSION_FILE_DIR)
        if not players:
            return

        for data in players:
            try:
                channel_id = data.get("channel_id")
                if not channel_id:
                    continue

                channel = self.bot.get_channel(channel_id)
                if not channel:
                    continue
                elif not any(False if member.bot or member.voice.self_deaf else True for member in channel.members):
                    continue
                    
                dj_member = channel.guild.get_member(data.get("dj"))
                if not dj_member:
                    continue

                # Get the guild settings
                settings = await MongoDBHandler.get_settings(channel.guild.id)

                # Connect to the channel and initialize the player.
                player: voicelink.Player = await channel.connect(
                    cls=voicelink.Player(self.bot, channel, TempCtx(dj_member, channel), settings)
                )

                # Restore the queue.
                queue_data = data.get("queue", {})
                for track_data in queue_data.get("tracks", []):
                    track_id = track_data.get("track_id")
                    if not track_id:
                        continue

                    decoded_track = voicelink.Track.decode(track_id)
                    requester = channel.guild.get_member(track_data.get("requester_id"))
                    track = voicelink.Track(track_id=track_id, info=decoded_track, requester=requester)
                    player.queue._queue.append(track)
                
                # Restore queue settings.
                player.queue._position = queue_data.get("position", 0) - 1
                repeat_mode = queue_data.get("repeat_mode", "OFF")
                try:
                    loop_mode = voicelink.LoopType[repeat_mode]
                except KeyError:
                    loop_mode = voicelink.LoopType.OFF
                player.queue._repeat.set_mode(loop_mode)
                player.queue._repeat_position = queue_data.get("repeat_position")

                # Restore player settings
                player.dj = dj_member
                player.settings['autoplay'] = data.get('autoplay', False)

                # Resume playback or invoke the controller based on the player's state.
                if not player.is_playing:
                    await player.do_next()

                    if is_paused := data.get("is_paused"):
                        await player.set_pause(is_paused, self.bot.user)
                    
                    if position := data.get("position"):
                        await player.seek(int(position), self.bot.user)

                await asyncio.sleep(5)

            except Exception as e:
                func.logger.error(f"Error encountered while restoring a player for channel ID {channel_id}.", exc_info=e)

        # Delete the last session file if it exists.
        try:
            if os.path.exists(Config.LAST_SESSION_FILE_DIR):
                os.remove(Config.LAST_SESSION_FILE_DIR)

        except Exception as del_error:
            func.logger.error("Failed to remove session file: %s", Config.LAST_SESSION_FILE_DIR, exc_info=del_error)

    @commands.Cog.listener()
    async def on_voicelink_track_end(self, player: voicelink.Player, track, _):
        await player.do_next()

    @commands.Cog.listener()
    async def on_voicelink_track_stuck(self, player: voicelink.Player, track, _):
        await asyncio.sleep(10)
        await player.do_next()

    @commands.Cog.listener()
    async def on_voicelink_track_exception(self, player: voicelink.Player, track, error: dict):
        try:
            player._track_is_stuck = True
            await player.context.send(f"{error['message']} The next song will begin in the next 5 seconds.", delete_after=10)
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.bot:
            return
        
        if before.channel == after.channel:
            return

        player: voicelink.Player = member.guild.voice_client
        if not player:
            return

        is_joined = True
        
        if not before.channel and after.channel:
            if after.channel.id != player.channel.id:
                return

        elif before.channel and not after.channel:
            is_joined = False
        
        elif before.channel and after.channel:
            if after.channel.id != player.channel.id:
                is_joined = False
                
        if is_joined and player.settings.get("24/7", False):
            if player.is_paused and len([m for m in player.channel.members if not m.bot or not m.voice.self_deaf]) == 1:
                await player.set_pause(False, member)
                    
        if not is_joined:
            if not player.is_paused and len([m for m in player.channel.members if not m.bot or not m.voice.self_deaf]) == 0:
                player._schedule_inactive_cleanup_timer()

        # if dj is not in the channel, find a new DJ
        if player.dj not in player.channel.members:
            for m in player.channel.members:
                if not m.bot or not m.voice.self_deaf:
                    player.dj = m
                    break
                    
        if player.is_ipc_connected:
            await player._ipc_client.send({
                "op": "updateGuild",
                "user": {
                    "userId": str(member.id),
                    "avatarUrl": member.display_avatar.url,
                    "name": member.name,
                },
                "channelName": member.voice.channel.name if is_joined else "",
                "guildId": str(member.guild.id),
                "isJoined": is_joined
            })

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Listeners(bot))
