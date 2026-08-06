from __future__ import annotations

import asyncio
import hmac
import time

from collections import defaultdict, deque
from pathlib import Path
from typing import Any

import discord

from aiohttp import web

from .enums import LoopType
from .mongodb import MongoDBHandler
from .objects import Playlist
from .player import Player
from .pool import NodePool
from .utils import TempCtx


WEBUI_DIR = Path(__file__).with_name("webui")
ASSET_TYPES = {
    "app.css": "text/css; charset=utf-8",
    "app.js": "application/javascript; charset=utf-8",
}


class DashboardError(Exception):
    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class WebDashboard:
    """A same-process control panel for one Vocard instance."""

    def __init__(self, bot, admin_password: str) -> None:
        self.bot = bot
        self.admin_password = admin_password.strip()
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._guild_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    @property
    def configured(self) -> bool:
        return bool(self.admin_password)

    def register(self, app: web.Application) -> None:
        app.router.add_get("/", self.index)
        app.router.add_get("/assets/{filename}", self.asset)
        app.router.add_get("/api/state", self.state)
        app.router.add_post("/api/action", self.action)

    @staticmethod
    def _secure_headers(response: web.StreamResponse) -> web.StreamResponse:
        response.headers.update({
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'self'; img-src 'self' https: data:; "
                "style-src 'self'; script-src 'self'; connect-src 'self'; "
                "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
            ),
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
        })
        return response

    def _json(self, payload: dict[str, Any], *, status: int = 200) -> web.Response:
        return self._secure_headers(web.json_response(payload, status=status))

    def _auth_error(self, request: web.Request) -> web.Response | None:
        if not self.configured:
            return self._json({
                "ok": False,
                "code": "not_configured",
                "message": "Set ADMIN_PASSWORD or WEB_DASHBOARD_KEY first.",
            }, status=503)

        authorization = request.headers.get("Authorization", "")
        scheme, _, supplied = authorization.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            supplied.encode(), self.admin_password.encode()
        ):
            return self._json({
                "ok": False,
                "code": "unauthorized",
                "message": "Admin password is invalid.",
            }, status=401)

        remote = request.remote or "unknown"
        now = time.monotonic()
        recent = self._requests[remote]
        while recent and recent[0] < now - 60:
            recent.popleft()
        if len(recent) >= 120:
            return self._json({
                "ok": False,
                "code": "rate_limited",
                "message": "Too many dashboard requests. Try again shortly.",
            }, status=429)
        recent.append(now)
        return None

    async def index(self, _: web.Request) -> web.StreamResponse:
        return self._secure_headers(web.FileResponse(WEBUI_DIR / "index.html"))

    async def asset(self, request: web.Request) -> web.StreamResponse:
        filename = request.match_info["filename"]
        content_type = ASSET_TYPES.get(filename)
        if not content_type:
            raise web.HTTPNotFound()
        response = web.FileResponse(WEBUI_DIR / filename)
        response.content_type = content_type.split(";", 1)[0]
        response.charset = "utf-8"
        return self._secure_headers(response)

    async def state(self, request: web.Request) -> web.Response:
        if (error := self._auth_error(request)) is not None:
            return error

        requested_id = request.query.get("guildId")
        guild = self._select_guild(requested_id)
        return self._json(self._state_payload(guild))

    async def action(self, request: web.Request) -> web.Response:
        if (error := self._auth_error(request)) is not None:
            return error

        try:
            data = await request.json()
        except Exception:
            return self._json({"ok": False, "message": "Invalid JSON body."}, status=400)

        try:
            guild_id = int(data.get("guildId"))
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                raise DashboardError("This bot is not in the selected server.", status=404)

            async with self._guild_locks[guild_id]:
                message = await self._run_action(guild, data)
        except (TypeError, ValueError):
            return self._json({"ok": False, "message": "A valid guildId is required."}, status=400)
        except DashboardError as exc:
            return self._json({"ok": False, "message": str(exc)}, status=exc.status)
        except Exception as exc:
            return self._json({"ok": False, "message": str(exc)}, status=400)

        return self._json({
            "ok": True,
            "message": message,
            "state": self._state_payload(guild),
        })

    def _select_guild(self, requested_id: str | None):
        if requested_id:
            try:
                guild = self.bot.get_guild(int(requested_id))
            except ValueError:
                guild = None
            if guild is not None:
                return guild

        return next(
            (guild for guild in self.bot.guilds if guild.voice_client),
            self.bot.guilds[0] if self.bot.guilds else None,
        )

    @staticmethod
    def _track_payload(track) -> dict[str, Any] | None:
        if track is None:
            return None
        requester = getattr(track, "requester", None)
        return {
            "id": track.track_id,
            "title": track.title,
            "author": track.author,
            "uri": track.uri,
            "thumbnail": track.thumbnail,
            "length": track.length,
            "isStream": track.is_stream,
            "source": track.source,
            "requester": getattr(requester, "display_name", "Web Dashboard"),
        }

    def _state_payload(self, selected_guild) -> dict[str, Any]:
        status = self.bot.health_status()
        guilds = []
        for guild in sorted(self.bot.guilds, key=lambda item: item.name.lower()):
            bot_member = guild.me
            channels = []
            for channel in [*guild.voice_channels, *guild.stage_channels]:
                permissions = channel.permissions_for(bot_member)
                if permissions.view_channel and permissions.connect:
                    channels.append({
                        "id": str(channel.id),
                        "name": channel.name,
                        "kind": "stage" if isinstance(channel, discord.StageChannel) else "voice",
                        "listeners": len([member for member in channel.members if not member.bot]),
                    })

            player = guild.voice_client
            guilds.append({
                "id": str(guild.id),
                "name": guild.name,
                "iconUrl": guild.icon.url if guild.icon else None,
                "memberCount": guild.member_count,
                "hasPlayer": bool(player),
                "playerChannelId": str(player.channel.id) if player and player.channel else None,
                "voiceChannels": channels,
            })

        player_payload = None
        if selected_guild and (player := selected_guild.voice_client):
            player_payload = {
                "channelId": str(player.channel.id) if player.channel else None,
                "channelName": player.channel.name if player.channel else None,
                "current": self._track_payload(player.current),
                "queue": [self._track_payload(track) for track in player.queue.tracks()],
                "position": player.position if player.is_playing else 0,
                "isPlaying": player.is_playing,
                "isPaused": player.is_paused,
                "volume": player.volume,
                "repeatMode": player.queue.repeat.lower(),
                "autoplay": player.settings.get("autoplay", False),
                "listeners": len([
                    member for member in (player.channel.members if player.channel else [])
                    if not member.bot
                ]),
            }

        return {
            "ok": True,
            "bot": {
                "id": str(self.bot.user.id),
                "name": self.bot.user.display_name,
                "avatarUrl": self.bot.user.display_avatar.url,
                "latencyMs": round(self.bot.latency * 1000),
                "guildCount": len(self.bot.guilds),
            },
            "services": status,
            "guilds": guilds,
            "selectedGuildId": str(selected_guild.id) if selected_guild else None,
            "player": player_payload,
        }

    def _voice_channel(self, guild, raw_channel_id):
        try:
            channel = guild.get_channel(int(raw_channel_id))
        except (TypeError, ValueError):
            channel = None
        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            raise DashboardError("Select a valid voice or stage channel.")

        permissions = channel.permissions_for(guild.me)
        if not permissions.view_channel or not permissions.connect:
            raise DashboardError("The bot cannot connect to that channel.", status=403)
        return channel

    async def _ensure_player(self, guild, channel) -> Player:
        player = guild.voice_client
        if player and player.channel and player.channel.id == channel.id:
            return player
        if player:
            await player.teardown()

        settings = await MongoDBHandler.get_settings(guild.id)
        context = TempCtx(guild.me, channel)
        player = await channel.connect(cls=Player(self.bot, channel, context, settings))
        if not player:
            raise DashboardError("The bot could not connect to the voice channel.")
        return player

    @staticmethod
    def _require_player(guild) -> Player:
        player = guild.voice_client
        if player is None:
            raise DashboardError("The bot is not connected in this server.")
        return player

    async def _run_action(self, guild, data: dict[str, Any]) -> str:
        action = str(data.get("action", "")).lower()
        requester = guild.me

        if action == "connect":
            channel = self._voice_channel(guild, data.get("voiceChannelId"))
            await self._ensure_player(guild, channel)
            return f"Connected to {channel.name}."

        if action == "play":
            query = str(data.get("query", "")).strip()
            if not query or len(query) > 500:
                raise DashboardError("Enter a song name or URL (maximum 500 characters).")
            player = guild.voice_client
            channel_id = data.get("voiceChannelId") or (
                player.channel.id if player and player.channel else None
            )
            channel = self._voice_channel(guild, channel_id)
            player = await self._ensure_player(guild, channel)
            node = NodePool.get_node()
            if node is None or not node.is_available:
                raise DashboardError("Lavalink is not ready.", status=503)
            result = await node.get_tracks(query=query, requester=requester)
            if not result:
                raise DashboardError("No tracks were found.", status=404)
            tracks = result.tracks if isinstance(result, Playlist) else result[:1]
            added = await player.add_track(tracks)
            if not player.is_playing:
                await player.do_next()
            return f"Added {added or len(tracks)} track(s)."

        player = self._require_player(guild)

        if action == "pause":
            pause = bool(data.get("pause", True))
            await player.set_pause(pause, requester)
            return "Playback paused." if pause else "Playback resumed."
        if action == "skip":
            if player.queue._repeat.mode == LoopType.TRACK:
                await player.set_repeat(LoopType.OFF, requester)
            await player.stop()
            return "Skipped to the next track."
        if action == "previous":
            player.queue.backto(2 if player.is_playing else 1)
            if player.is_playing:
                await player.stop()
            else:
                await player.do_next()
            return "Returned to the previous track."
        if action == "volume":
            volume = max(0, min(int(data.get("volume", 100)), 200))
            await MongoDBHandler.update_settings(guild.id, {"$set": {"volume": volume}})
            await player.set_volume(volume, requester)
            return f"Volume set to {volume}%."
        if action == "seek":
            position = int(data.get("position", 0))
            await player.seek(position, requester)
            return "Playback position updated."
        if action == "repeat":
            raw_mode = str(data.get("mode", "")).upper()
            mode = LoopType.__members__.get(raw_mode) if raw_mode else None
            selected = await player.set_repeat(mode, requester)
            return f"Repeat mode: {selected.name.lower()}."
        if action == "autoplay":
            enabled = bool(data.get("enabled", False))
            player.settings["autoplay"] = enabled
            await MongoDBHandler.update_settings(guild.id, {"$set": {"autoplay": enabled}})
            if enabled and not player.is_playing:
                await player.do_next()
            return "Autoplay enabled." if enabled else "Autoplay disabled."
        if action == "shuffle":
            await player.shuffle("queue", requester)
            return "Queue shuffled."
        if action == "clear":
            await player.clear_queue("queue", requester)
            return "Upcoming queue cleared."
        if action == "remove":
            index = int(data.get("index"))
            if index < 1:
                raise DashboardError("Queue index must be positive.")
            removed = await player.remove_track(index, requester=requester)
            if not removed:
                raise DashboardError("That queue item no longer exists.", status=404)
            return "Track removed from the queue."
        if action == "disconnect":
            await player.teardown()
            return "Disconnected from voice."

        raise DashboardError("Unsupported dashboard action.", status=404)
