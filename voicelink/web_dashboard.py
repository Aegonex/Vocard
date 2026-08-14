from __future__ import annotations

import asyncio
import hmac
import time

from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Optional

import discord

from aiohttp import web

from .config import Config
from .enums import LoopType
from .filters import (
    ChannelMix,
    Distortion,
    Filters,
    Karaoke,
    LowPass,
    Rotation,
    Timescale,
    Tremolo,
    Vibrato,
)
from .language import LangHandler
from .mongodb import MongoDBHandler
from .objects import Playlist, Track
from .player import Player
from .pool import NodePool
from .utils import TempCtx


WEBUI_DIR = Path(__file__).with_name("webui")
ASSET_TYPES = {
    "app.css": "text/css; charset=utf-8",
    "app.js": "application/javascript; charset=utf-8",
}
AUDIO_MODE_LABELS = {
    "normal": "Normal",
    "karaoke": "Karaoke",
    "tremolo": "Tremolo",
    "vibrato": "Vibrato",
    "rotation": "Rotation",
    "distortion": "Distortion",
    "lowpass": "Low Pass",
    "nightcore": "Nightcore",
    "vaporwave": "Vaporwave",
    "8d": "8D Audio",
}

# Slider effects mirror the ranges the Discord commands accepted, except where
# the filter class itself is stricter (tremolo frequency caps at 5 in Lavalink).
EFFECT_PARAMS = {
    "speed": {"speed": ("Speed", 0, 2, 1.0)},
    "karaoke": {
        "level": ("Level", 0, 2, 1.0),
        "mono_level": ("Mono level", 0, 2, 1.0),
        "filter_band": ("Filter band", 100, 300, 220.0),
        "filter_width": ("Filter width", 50, 150, 100.0),
    },
    "tremolo": {
        "frequency": ("Frequency", 0, 5, 2.0),
        "depth": ("Depth", 0, 1, 0.5),
    },
    "vibrato": {
        "frequency": ("Frequency", 0, 14, 2.0),
        "depth": ("Depth", 0, 1, 0.5),
    },
    "rotation": {"rotation_hertz": ("Rotation hertz", 0, 2, 0.2)},
    "lowpass": {"smoothing": ("Smoothing", 10, 30, 20.0)},
    "channelmix": {
        "left_to_left": ("Left to left", 0, 1, 1.0),
        "right_to_right": ("Right to right", 0, 1, 1.0),
        "left_to_right": ("Left to right", 0, 1, 0.0),
        "right_to_left": ("Right to left", 0, 1, 0.0),
    },
    "distortion": {},
}
EFFECT_BUILDERS = {
    "speed": lambda p: Timescale(tag="speed", speed=p["speed"]),
    "karaoke": lambda p: Karaoke(
        tag="karaoke",
        level=p["level"],
        mono_level=p["mono_level"],
        filter_band=p["filter_band"],
        filter_width=p["filter_width"],
    ),
    "tremolo": lambda p: Tremolo(tag="tremolo", frequency=p["frequency"], depth=p["depth"]),
    "vibrato": lambda p: Vibrato(tag="vibrato", frequency=p["frequency"], depth=p["depth"]),
    "rotation": lambda p: Rotation(tag="rotation", rotation_hertz=p["rotation_hertz"]),
    "lowpass": lambda p: LowPass(tag="lowpass", smoothing=p["smoothing"]),
    "channelmix": lambda p: ChannelMix(
        tag="channelmix",
        left_to_left=p["left_to_left"],
        right_to_right=p["right_to_right"],
        left_to_right=p["left_to_right"],
        right_to_left=p["right_to_left"],
    ),
    "distortion": lambda p: Distortion(tag="distortion"),
}

# field -> (mongo key, kind). Kinds drive validation in _settings_update.
SETTING_FIELDS = {
    "language": ("lang", "language"),
    "dj_role": ("dj", "role"),
    "queue_mode": ("queue_type", "queue_mode"),
    "default_volume": ("volume", "volume"),
    "play_forever": ("24/7", "bool"),
    "bypass_vote": ("disabled_vote", "bool"),
    "controller": ("controller", "bool"),
    "controller_msg": ("controller_msg", "bool"),
    "duplicate_track": ("duplicate_track", "bool"),
    "silent_msg": ("silent_msg", "bool"),
    "stage_announce_template": ("stage_announce_template", "template"),
    "music_request_channel": ("music_request_channel", "channel"),
}
HISTORY_LIMIT = 10
PLAYLIST_NAME_LIMIT = 10
INBOX_LIMIT = 10


class DashboardError(Exception):
    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _playlist_limits() -> tuple[int, int, str]:
    try:
        return Config().get_playlist_config()
    except Exception:
        return 5, 500, "Favourite"


def _languages() -> list[str]:
    try:
        return sorted(LangHandler.get_all_languages())
    except Exception:
        return []


class WebDashboard:
    """A same-process control panel for one Vocard instance."""

    def __init__(self, bot, admin_password: str, *, commands_enabled: bool = True) -> None:
        self.bot = bot
        self.admin_password = admin_password.strip()
        self.commands_enabled = commands_enabled
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._guild_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    @property
    def configured(self) -> bool:
        return bool(self.admin_password)

    def register(self, app: web.Application) -> None:
        app.router.add_get("/", self.index)
        app.router.add_get("/assets/{filename}", self.asset)
        app.router.add_get("/api/state", self.state)
        app.router.add_get("/api/playlists", self.playlists)
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

    async def _warm_settings(self, guild_id: int) -> None:
        """Best-effort cache warm so the sync state payload sees settings."""
        try:
            await MongoDBHandler.get_settings(guild_id)
        except Exception:
            pass

    async def state(self, request: web.Request) -> web.Response:
        if (error := self._auth_error(request)) is not None:
            return error

        requested_id = request.query.get("guildId")
        guild = self._select_guild(requested_id)
        if guild is not None:
            await self._warm_settings(guild.id)
        return self._json(self._state_payload(guild))

    async def playlists(self, request: web.Request) -> web.Response:
        if (error := self._auth_error(request)) is not None:
            return error

        try:
            user_id = int(request.query.get("userId", ""))
        except ValueError:
            return self._json({"ok": False, "message": "A valid Discord user id is required."}, status=400)

        try:
            payload = await self._playlists_payload(user_id, request.query.get("playlistId"))
        except DashboardError as exc:
            return self._json({"ok": False, "message": str(exc)}, status=exc.status)
        except Exception as exc:
            return self._json({"ok": False, "message": str(exc)}, status=400)
        return self._json(payload)

    async def action(self, request: web.Request) -> web.Response:
        if (error := self._auth_error(request)) is not None:
            return error

        try:
            data = await request.json()
        except Exception:
            return self._json({"ok": False, "message": "Invalid JSON body."}, status=400)

        try:
            guild_id = int(data.get("guildId"))
        except (TypeError, ValueError):
            return self._json({"ok": False, "message": "A valid guildId is required."}, status=400)

        try:
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                raise DashboardError("This bot is not in the selected server.", status=404)

            async with self._guild_locks[guild_id]:
                result = await self._run_action(guild, data)
        except DashboardError as exc:
            return self._json({"ok": False, "message": str(exc)}, status=exc.status)
        except Exception as exc:
            return self._json({"ok": False, "message": str(exc)}, status=400)

        message, extra = result if isinstance(result, tuple) else (result, None)
        payload = {
            "ok": True,
            "message": message,
            "state": self._state_payload(guild),
        }
        if extra is not None:
            payload["data"] = extra
        return self._json(payload)

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
            "id": getattr(track, "track_id", None),
            "title": getattr(track, "title", "Unknown"),
            "author": getattr(track, "author", "Unknown"),
            "uri": getattr(track, "uri", None),
            "thumbnail": getattr(track, "thumbnail", None),
            "length": getattr(track, "length", 0),
            "isStream": getattr(track, "is_stream", False),
            "source": getattr(track, "source", "unknown"),
            "requester": getattr(requester, "display_name", "Web Dashboard"),
        }

    @staticmethod
    def _settings_payload(settings: dict[str, Any]) -> dict[str, Any]:
        request_channel = settings.get("music_request_channel") or {}
        dj_role = settings.get("dj")
        return {
            "language": settings.get("lang", "EN"),
            "djRoleId": str(dj_role) if dj_role else None,
            "queueMode": str(settings.get("queue_type", "queue")).lower(),
            "defaultVolume": settings.get("volume", 100),
            "playForever": bool(settings.get("24/7", False)),
            "bypassVote": bool(settings.get("disabled_vote", True)),
            "controller": bool(settings.get("controller", True)),
            "controllerMsg": bool(settings.get("controller_msg", True)),
            "duplicateTrack": bool(settings.get("duplicate_track", True)),
            "silentMsg": bool(settings.get("silent_msg", False)),
            "stageAnnounceTemplate": settings.get("stage_announce_template") or "",
            "requestChannelId": (
                str(request_channel.get("text_channel_id"))
                if request_channel.get("text_channel_id") else None
            ),
        }

    def _state_payload(self, selected_guild) -> dict[str, Any]:
        status = self.bot.health_status()
        guilds = []
        for guild in sorted(self.bot.guilds, key=lambda item: item.name.lower()):
            bot_member = guild.me
            player = guild.voice_client
            channels = []
            for channel in [*guild.voice_channels, *guild.stage_channels]:
                permissions = channel.permissions_for(bot_member)
                is_bot_connected = bool(
                    player and player.channel and player.channel.id == channel.id
                )
                can_connect = permissions.view_channel and permissions.connect
                if is_bot_connected or can_connect:
                    listeners = len([member for member in channel.members if not member.bot])
                    channels.append({
                        "id": str(channel.id),
                        "name": channel.name,
                        "kind": "stage" if isinstance(channel, discord.StageChannel) else "voice",
                        "listeners": listeners,
                        "status": (
                            "connected" if is_bot_connected
                            else "empty" if listeners == 0
                            else "available"
                        ),
                        "isBotConnected": is_bot_connected,
                    })

            text_channels = []
            for channel in getattr(guild, "text_channels", []):
                try:
                    permissions = channel.permissions_for(bot_member)
                    if permissions.view_channel and permissions.send_messages:
                        text_channels.append({"id": str(channel.id), "name": channel.name})
                except Exception:
                    continue

            roles = []
            for role in getattr(guild, "roles", []):
                try:
                    if role.is_default() or role.managed:
                        continue
                    roles.append({"id": str(role.id), "name": role.name})
                except Exception:
                    continue
            roles.reverse()

            guilds.append({
                "id": str(guild.id),
                "name": guild.name,
                "iconUrl": guild.icon.url if guild.icon else None,
                "memberCount": guild.member_count,
                "hasPlayer": bool(player),
                "playerChannelId": str(player.channel.id) if player and player.channel else None,
                "voiceChannels": channels,
                "textChannels": text_channels,
                "roles": roles,
            })

        player_payload = None
        settings_payload = None
        if selected_guild is not None:
            try:
                settings_payload = self._settings_payload(
                    MongoDBHandler.get_cached_settings(selected_guild.id)
                )
            except Exception:
                settings_payload = self._settings_payload({})

        if selected_guild and (player := selected_guild.voice_client):
            queue = player.queue.tracks()
            history = player.queue.history()
            current = player.current
            active_filters = [item.tag.lower() for item in player.filters.get_filters()]
            audio_mode = (
                "normal" if not active_filters
                else active_filters[0] if len(active_filters) == 1 and active_filters[0] in AUDIO_MODE_LABELS
                else "custom"
            )
            player_payload = {
                "channelId": str(player.channel.id) if player.channel else None,
                "channelName": player.channel.name if player.channel else None,
                "current": self._track_payload(current),
                "queue": [self._track_payload(track) for track in queue],
                "history": [
                    self._track_payload(track)
                    for track in reversed(history[-HISTORY_LIMIT:])
                ],
                "historyCount": len(history),
                "position": player.position if player.is_playing else 0,
                "isPlaying": player.is_playing,
                "isPaused": player.is_paused,
                "volume": player.volume,
                "repeatMode": player.queue.repeat.lower(),
                "autoplay": player.settings.get("autoplay", False),
                "audioMode": audio_mode,
                "activeFilters": active_filters,
                "capabilities": {
                    "canPause": bool(current),
                    "canSkip": bool(current or queue),
                    "canPrevious": bool(history),
                    "canShuffle": len(queue) >= 3,
                    "canClear": bool(queue),
                    "canSeek": bool(current and not current.is_stream),
                },
                "listeners": len([
                    member for member in (player.channel.members if player.channel else [])
                    if not member.bot
                ]),
            }

        max_playlists, max_tracks, _ = _playlist_limits()
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
            "commandsEnabled": self.commands_enabled,
            "languages": _languages(),
            "playlistLimits": {"maxPlaylists": max_playlists, "maxTracks": max_tracks},
            "guilds": guilds,
            "selectedGuildId": str(selected_guild.id) if selected_guild else None,
            "settings": settings_payload,
            "player": player_payload,
        }

    # ------------------------------------------------------------------ helpers

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

    async def _player_for_playback(self, guild, data: dict[str, Any]) -> Player:
        player = guild.voice_client
        channel_id = data.get("voiceChannelId") or (
            player.channel.id if player and player.channel else None
        )
        channel = self._voice_channel(guild, channel_id)
        return await self._ensure_player(guild, channel)

    @staticmethod
    def _require_player(guild) -> Player:
        player = guild.voice_client
        if player is None:
            raise DashboardError("The bot is not connected in this server.")
        return player

    @staticmethod
    def _integer(value: Any, label: str) -> int:
        if isinstance(value, bool):
            raise DashboardError(f"{label} must be a whole number.")
        try:
            return int(value)
        except (TypeError, ValueError):
            raise DashboardError(f"{label} must be a whole number.")

    @staticmethod
    def _boolean(value: Any, label: str) -> bool:
        if not isinstance(value, bool):
            raise DashboardError(f"{label} must be true or false.")
        return value

    @staticmethod
    def _number(value: Any, label: str, minimum: float, maximum: float) -> float:
        if isinstance(value, bool):
            raise DashboardError(f"{label} must be a number.")
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise DashboardError(f"{label} must be a number.")
        if number < minimum or number > maximum:
            raise DashboardError(f"{label} must be between {minimum:g} and {maximum:g}.")
        return number

    @staticmethod
    def _user_id(data: dict[str, Any]) -> int:
        try:
            user_id = int(data.get("userId"))
        except (TypeError, ValueError):
            raise DashboardError("A valid Discord user id is required.")
        if user_id <= 0:
            raise DashboardError("A valid Discord user id is required.")
        return user_id

    @staticmethod
    def _play_mode(data: dict[str, Any]) -> str:
        mode = str(data.get("mode", "append")).lower()
        if mode not in {"append", "top", "force"}:
            raise DashboardError("Play mode must be append, top, or force.")
        return mode

    @staticmethod
    def _decode_stored_track(track_id: str, requester) -> Track:
        try:
            info = Track.decode(track_id)
        except Exception:
            raise DashboardError("A stored track could not be decoded.")
        return Track(track_id=track_id, info=info, requester=requester)

    async def _start_playback(self, player: Player, mode: str, requester) -> None:
        if mode == "force":
            if player.queue._repeat.mode == LoopType.TRACK:
                await player.set_repeat(LoopType.OFF, requester)
            if player.is_playing:
                await player.stop()
            else:
                await player.do_next()
        elif not player.is_playing:
            await player.do_next()

    # ------------------------------------------------------------------ actions

    async def _run_action(self, guild, data: dict[str, Any]):
        action = str(data.get("action", "")).lower()
        requester = guild.me

        if action == "connect":
            channel = self._voice_channel(guild, data.get("voiceChannelId"))
            await self._ensure_player(guild, channel)
            return f"Connected to {channel.name}."

        if action == "play":
            return await self._action_play(guild, data, requester)
        if action == "play_encoded":
            return await self._action_play_encoded(guild, data, requester)
        if action == "queue_import":
            return await self._action_queue_import(guild, data, requester)
        if action.startswith("playlist_") or action.startswith("inbox_"):
            return await self._playlist_action(guild, action, data, requester)

        player = self._require_player(guild)

        if action == "pause":
            if player.current is None:
                raise DashboardError("Nothing is playing right now.")
            pause = data.get("pause", True)
            if not isinstance(pause, bool):
                raise DashboardError("Pause state must be true or false.")
            await player.set_pause(pause, requester)
            return "Playback paused." if pause else "Playback resumed."
        if action == "skip":
            if player.current is None and not player.queue.tracks():
                raise DashboardError("There is no track to skip.")
            if player.queue._repeat.mode == LoopType.TRACK:
                await player.set_repeat(LoopType.OFF, requester)
            if player.current is not None:
                await player.stop()
            else:
                await player.do_next()
            return "Skipped to the next track."
        if action == "previous":
            if not player.queue.history():
                raise DashboardError("There is no previous track.")
            player.queue.backto(2 if player.is_playing else 1)
            if player.is_playing:
                await player.stop()
            else:
                await player.do_next()
            return "Returned to the previous track."
        if action == "volume":
            volume = self._integer(data.get("volume", 100), "Volume")
            if not 0 <= volume <= 200:
                raise DashboardError("Volume must be between 0 and 200.")
            await MongoDBHandler.update_settings(guild.id, {"$set": {"volume": volume}})
            await player.set_volume(volume, requester)
            return f"Volume set to {volume}%."
        if action == "seek":
            if player.current is None:
                raise DashboardError("Nothing is playing right now.")
            if player.current.is_stream:
                raise DashboardError("Live streams cannot be seeked.")
            position = self._integer(data.get("position", 0), "Position")
            await player.seek(position, requester)
            return "Playback position updated."
        if action in {"forward", "rewind"}:
            if player.current is None:
                raise DashboardError("Nothing is playing right now.")
            if player.current.is_stream:
                raise DashboardError("Live streams cannot be seeked.")
            offset = self._integer(data.get("offset", 10_000), "Offset")
            if offset <= 0:
                raise DashboardError("Offset must be positive.")
            if action == "rewind":
                offset = -offset
            position = max(0, min(player.position + offset, player.current.length - 1))
            await player.seek(position, requester)
            return "Playback position updated."
        if action == "replay":
            if player.current is None:
                raise DashboardError("Nothing is playing right now.")
            if player.current.is_stream:
                raise DashboardError("Live streams cannot be seeked.")
            await player.seek(0, requester)
            return "Replaying the current track."
        if action == "repeat":
            raw_mode = str(data.get("mode", "")).upper()
            mode = LoopType.__members__.get(raw_mode) if raw_mode else None
            if raw_mode and mode is None:
                raise DashboardError("Repeat mode must be off, track, or queue.")
            selected = await player.set_repeat(mode, requester)
            return f"Repeat mode: {selected.name.lower()}."
        if action == "autoplay":
            enabled = data.get("enabled", False)
            if not isinstance(enabled, bool):
                raise DashboardError("Autoplay state must be true or false.")
            player.settings["autoplay"] = enabled
            await MongoDBHandler.update_settings(guild.id, {"$set": {"autoplay": enabled}})
            if enabled and not player.is_playing:
                await player.do_next()
            return "Autoplay enabled." if enabled else "Autoplay disabled."
        if action == "shuffle":
            if len(player.queue.tracks()) < 3:
                raise DashboardError("Add at least 3 upcoming tracks before shuffling.")
            await player.shuffle("queue", requester)
            return "Queue shuffled."
        if action == "clear":
            if not player.queue.tracks():
                return "The upcoming queue is already empty."
            await player.clear_queue("queue", requester)
            return "Upcoming queue cleared."
        if action == "remove":
            index = self._integer(data.get("index"), "Queue index")
            if index < 1:
                raise DashboardError("Queue index must be positive.")
            removed = await player.remove_track(index, requester=requester)
            if not removed:
                raise DashboardError("That queue item no longer exists.", status=404)
            return "Track removed from the queue."
        if action == "queue_export":
            tracks = player.queue.tracks(True)
            if not tracks:
                raise DashboardError("There is nothing to export.")
            return "Queue exported.", {
                "guildName": guild.name,
                "tracks": [
                    {
                        "id": track.track_id,
                        "title": track.title,
                        "length": track.length,
                    }
                    for track in tracks
                ],
            }
        if action == "audio_mode":
            mode = str(data.get("mode", "")).strip().lower()
            factory = Filters.get_available_filters().get(mode)
            if mode != "normal" and factory is None:
                raise DashboardError("Select a valid music mode.")
            if player.filters.get_filters():
                await player.reset_filter(requester=requester)
            if factory is not None:
                await player.add_filter(factory(), requester)
            return f"Music mode: {AUDIO_MODE_LABELS[mode]}."
        if action == "effect_set":
            return await self._action_effect_set(player, data, requester)
        if action == "effect_clear":
            return await self._action_effect_clear(player, data, requester)
        if action == "settings_update":
            return await self._settings_update(guild, player, data, requester)
        if action == "settings_reset":
            return await self._settings_reset(guild, player, data, requester)
        if action == "disconnect":
            await player.teardown()
            return "Disconnected from voice."

        raise DashboardError("Unsupported dashboard action.", status=404)

    async def _action_play(self, guild, data: dict[str, Any], requester):
        query = str(data.get("query", "")).strip()
        if not query or len(query) > 500:
            raise DashboardError("Enter a song name or URL (maximum 500 characters).")
        mode = self._play_mode(data)
        player = await self._player_for_playback(guild, data)
        node = NodePool.get_node()
        if node is None or not node.is_available:
            raise DashboardError("Lavalink is not ready.", status=503)
        result = await node.get_tracks(query=query, requester=requester)
        if not result:
            raise DashboardError("No tracks were found.", status=404)
        tracks = result.tracks if isinstance(result, Playlist) else result[:1]
        if mode == "append":
            added = await player.add_track(tracks)
        else:
            added = await player.add_track(tracks, at_front=True)
        await self._start_playback(player, mode, requester)
        # Single adds return a queue position, list adds return a count.
        count = (added or 0) if isinstance(result, Playlist) else (1 if added else 0)
        if mode == "top":
            return f"Added {count} track(s) to the top of the queue."
        if mode == "force":
            return f"Force playing {count} track(s)."
        return f"Added {count} track(s)."

    async def _action_play_encoded(self, guild, data: dict[str, Any], requester):
        track_id = str(data.get("trackId", "")).strip()
        if not track_id:
            raise DashboardError("A track id is required.")
        mode = self._play_mode(data)
        player = await self._player_for_playback(guild, data)
        track = self._decode_stored_track(track_id, requester)
        if mode == "append":
            await player.add_track(track)
        else:
            await player.add_track(track, at_front=True)
        await self._start_playback(player, mode, requester)
        return f"Added {track.title} to the queue."

    async def _action_queue_import(self, guild, data: dict[str, Any], requester):
        raw = data.get("trackIds")
        if isinstance(raw, str):
            raw = [item for item in raw.strip().split(",") if item]
        if not isinstance(raw, list) or not raw:
            raise DashboardError("No tracks were found in that file.")
        if len(raw) > 1000:
            raise DashboardError("That file contains too many tracks.")
        player = await self._player_for_playback(guild, data)
        tracks = [self._decode_stored_track(str(item).strip(), requester) for item in raw]
        await player.add_track(tracks)
        if not player.is_playing:
            await player.do_next()
        return f"Imported {len(tracks)} track(s)."

    async def _action_effect_set(self, player: Player, data: dict[str, Any], requester):
        effect = str(data.get("effect", "")).strip().lower()
        spec = EFFECT_PARAMS.get(effect)
        if spec is None:
            raise DashboardError("Select a valid effect.")
        raw_params = data.get("params") or {}
        if not isinstance(raw_params, dict):
            raise DashboardError("Effect parameters must be an object.")
        params = {}
        for key, (label, minimum, maximum, default) in spec.items():
            value = raw_params.get(key, default)
            params[key] = self._number(value, label, minimum, maximum)
        built = EFFECT_BUILDERS[effect](params)
        if player.filters.has_filter(filter_tag=built.tag):
            player.filters.remove_filter(filter_tag=built.tag)
        await player.add_filter(built, requester)
        return f"Effect applied: {built.tag}."

    async def _action_effect_clear(self, player: Player, data: dict[str, Any], requester):
        tag = str(data.get("tag", "")).strip().lower()
        if tag:
            if not player.filters.has_filter(filter_tag=tag):
                raise DashboardError("That effect is not active.")
            await player.remove_filter(tag, requester)
            return f"Effect removed: {tag}."
        if not player.filters.get_filters():
            return "No effects are active."
        await player.reset_filter(requester=requester)
        return "All effects cleared."

    # ------------------------------------------------------------------ settings

    async def _settings_update(self, guild, player: Optional[Player], data: dict[str, Any], requester):
        field = str(data.get("field", "")).strip().lower()
        entry = SETTING_FIELDS.get(field)
        if entry is None:
            raise DashboardError("Unknown setting.")
        key, kind = entry
        value = data.get("value")

        if kind == "bool":
            parsed = self._boolean(value, "Value")
            await MongoDBHandler.update_settings(guild.id, {"$set": {key: parsed}})
            if key == "duplicate_track" and player is not None:
                player.queue._allow_duplicate = parsed
            return "Setting updated."

        if kind == "language":
            language = str(value or "").strip().upper()
            languages = _languages()
            if not language or (languages and language not in languages):
                raise DashboardError("Select a valid language.")
            await MongoDBHandler.update_settings(guild.id, {"$set": {key: language}})
            return "Setting updated."

        if kind == "role":
            if value in (None, "", "none"):
                await MongoDBHandler.update_settings(guild.id, {"$unset": {key: None}})
                return "DJ role cleared."
            role_id = self._integer(value, "Role id")
            role = guild.get_role(role_id)
            if role is None:
                raise DashboardError("Select a valid role.")
            await MongoDBHandler.update_settings(guild.id, {"$set": {key: role.id}})
            return "Setting updated."

        if kind == "queue_mode":
            mode = str(value or "").strip().lower()
            if mode not in {"queue", "fairqueue"}:
                raise DashboardError("Queue mode must be queue or fairqueue.")
            await MongoDBHandler.update_settings(guild.id, {"$set": {key: mode}})
            return "Setting updated. New queue mode applies after the next connect."

        if kind == "volume":
            volume = self._integer(value, "Volume")
            if not 1 <= volume <= 150:
                raise DashboardError("Volume must be between 1 and 150.")
            await MongoDBHandler.update_settings(guild.id, {"$set": {key: volume}})
            if player is not None:
                await player.set_volume(volume, requester)
            return "Setting updated."

        if kind == "template":
            if value in (None, ""):
                await MongoDBHandler.update_settings(guild.id, {"$unset": {key: None}})
                return "Stage announce template cleared."
            template = str(value)
            if len(template) > 500:
                raise DashboardError("Template must be 500 characters or fewer.")
            await MongoDBHandler.update_settings(guild.id, {"$set": {key: template}})
            return "Setting updated."

        if kind == "channel":
            if value in (None, "", "none"):
                await MongoDBHandler.update_settings(guild.id, {"$unset": {key: None}})
                return "Music request channel cleared."
            channel_id = self._integer(value, "Channel id")
            channel = guild.get_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                raise DashboardError("Select a valid text channel.")
            permissions = channel.permissions_for(guild.me)
            if not permissions.view_channel or not permissions.send_messages:
                raise DashboardError("The bot cannot use that channel.", status=403)
            await MongoDBHandler.update_settings(
                guild.id,
                {"$set": {key: {"text_channel_id": channel.id}}},
            )
            return "Music request channel updated."

        raise DashboardError("Unknown setting.")

    async def _settings_reset(self, guild, player: Optional[Player], data: dict[str, Any], requester):
        field = str(data.get("field", "")).strip().lower()
        entry = SETTING_FIELDS.get(field)
        if entry is None:
            raise DashboardError("Unknown setting.")
        key, _ = entry
        await MongoDBHandler.update_settings(guild.id, {"$unset": {key: None}})
        if key == "volume" and player is not None:
            await player.set_volume(100, requester)
        if key == "duplicate_track" and player is not None:
            player.queue._allow_duplicate = True
        return "Setting reset to default."

    # ------------------------------------------------------------------ playlists

    async def _playlists_payload(self, user_id: int, playlist_id: str | None) -> dict[str, Any]:
        max_playlists, max_tracks, _ = _playlist_limits()
        playlists = await MongoDBHandler.get_user(user_id, d_type="playlist")
        inbox = await MongoDBHandler.get_user(user_id, d_type="inbox")

        items = []
        for index, (pid, playlist) in enumerate(playlists.items(), start=1):
            entry = {
                "id": pid,
                "name": playlist.get("name", pid),
                "type": playlist.get("type", "playlist"),
                "locked": index > max_playlists,
                "trackCount": None,
                "broken": False,
            }
            if playlist.get("type") == "share":
                resolved, error = await self._resolve_share(user_id, playlist)
                if error:
                    entry["broken"] = True
                else:
                    entry["trackCount"] = len(resolved.get("tracks", []))
            elif playlist.get("type") == "link":
                entry["trackCount"] = None
            else:
                entry["trackCount"] = len(playlist.get("tracks", []))
            items.append(entry)

        detail = None
        if playlist_id:
            playlist = playlists.get(playlist_id)
            if playlist is None:
                raise DashboardError("Playlist not found.", status=404)
            resolved = playlist
            broken = False
            if playlist.get("type") == "share":
                resolved, error = await self._resolve_share(user_id, playlist)
                if error:
                    broken = True
                    resolved = {}
            tracks = []
            if not broken and resolved.get("type", "playlist") == "playlist":
                for track_id in resolved.get("tracks", [])[:max_tracks]:
                    try:
                        info = Track.decode(track_id)
                    except Exception:
                        continue
                    tracks.append({
                        "id": track_id,
                        "title": info.get("title", "Unknown"),
                        "author": info.get("author", "Unknown"),
                        "length": info.get("length", 0),
                    })
            detail = {
                "id": playlist_id,
                "name": playlist.get("name", playlist_id),
                "type": playlist.get("type", "playlist"),
                "broken": broken,
                "uri": resolved.get("uri") if resolved else None,
                "tracks": tracks,
                "perms": resolved.get("perms", {}) if resolved else {},
            }

        inbox_items = []
        for index, mail in enumerate(inbox):
            sender_id = mail.get("sender")
            sender = self.bot.get_user(sender_id) if sender_id else None
            inbox_items.append({
                "index": index,
                "title": mail.get("title", "Invitation"),
                "description": mail.get("description", ""),
                "senderId": str(sender_id) if sender_id else None,
                "senderName": getattr(sender, "display_name", None) or str(sender_id),
                "time": mail.get("time"),
            })

        return {
            "ok": True,
            "userId": str(user_id),
            "playlists": items,
            "detail": detail,
            "inbox": inbox_items,
            "limits": {"maxPlaylists": max_playlists, "maxTracks": max_tracks},
        }

    async def _resolve_share(self, user_id: int, playlist: dict[str, Any]):
        owner_id = playlist.get("user")
        refer_id = playlist.get("referId")
        if not owner_id or not refer_id:
            return None, "not_found"
        try:
            owner_playlists = await MongoDBHandler.get_user(owner_id, d_type="playlist")
        except Exception:
            return None, "not_found"
        target = owner_playlists.get(refer_id)
        if target is None:
            return None, "not_found"
        perms = target.get("perms", {})
        if user_id not in perms.get("read", []):
            return None, "no_read"
        return target, None

    async def _playlist_owner_ref(
        self, user_id: int, playlist_id: str, *, required_perm: str = "read"
    ) -> tuple[int, str, dict[str, Any]]:
        """Resolve a playlist entry to (owner_id, owner_playlist_id, playlist)."""
        playlists = await MongoDBHandler.get_user(user_id, d_type="playlist")
        playlist = playlists.get(playlist_id)
        if playlist is None:
            raise DashboardError("Playlist not found.", status=404)

        if playlist.get("type") == "share":
            target, error = await self._resolve_share(user_id, playlist)
            if error:
                raise DashboardError("The shared playlist is no longer available.", status=404)
            if required_perm != "read" and user_id not in target.get("perms", {}).get(required_perm, []):
                raise DashboardError("You do not have permission to modify this playlist.", status=403)
            return playlist["user"], playlist["referId"], target

        return user_id, playlist_id, playlist

    async def _playlist_action(self, guild, action: str, data: dict[str, Any], requester):
        user_id = self._user_id(data)
        max_playlists, max_tracks, _ = _playlist_limits()

        if action == "playlist_play":
            playlist_id = str(data.get("playlistId", "")).strip()
            owner_id, owner_pid, playlist = await self._playlist_owner_ref(user_id, playlist_id)
            player = await self._player_for_playback(guild, data)

            if playlist.get("type") == "link":
                node = NodePool.get_node()
                if node is None or not node.is_available:
                    raise DashboardError("Lavalink is not ready.", status=503)
                result = await node.get_tracks(query=playlist.get("uri", ""), requester=requester)
                if not isinstance(result, Playlist) or not result.tracks:
                    raise DashboardError("That playlist link could not be loaded.", status=404)
                tracks = result.tracks[:max_tracks]
            else:
                stored = playlist.get("tracks", [])[:max_tracks]
                if not stored:
                    raise DashboardError("This playlist is empty.")
                tracks = [self._decode_stored_track(track_id, requester) for track_id in stored]

            await player.add_track(tracks)
            if not player.is_playing:
                await player.do_next()
            return f"Queued {len(tracks)} track(s) from {playlist.get('name', playlist_id)}."

        if action == "playlist_create":
            name = str(data.get("name", "")).strip()
            if not name or len(name) > PLAYLIST_NAME_LIMIT:
                raise DashboardError(
                    f"Playlist names must be 1-{PLAYLIST_NAME_LIMIT} characters."
                )
            playlists = await MongoDBHandler.get_user(user_id, d_type="playlist")
            if len(playlists) >= max_playlists:
                raise DashboardError(f"You can keep at most {max_playlists} playlists.")
            if any(p.get("name", "").lower() == name.lower() for p in playlists.values()):
                raise DashboardError("A playlist with that name already exists.")
            new_id = next(
                (str(i) for i in range(200, 210) if str(i) not in playlists), None
            )
            if new_id is None:
                raise DashboardError("No playlist slots are available.")

            link = str(data.get("link", "")).strip()
            if link:
                node = NodePool.get_node()
                if node is None or not node.is_available:
                    raise DashboardError("Lavalink is not ready.", status=503)
                result = await node.get_tracks(query=link, requester=requester)
                if not isinstance(result, Playlist):
                    raise DashboardError("That link is not a playlist.")
                entry = {"uri": link, "perms": {"read": []}, "name": name, "type": "link"}
            else:
                entry = {
                    "tracks": [],
                    "perms": {"read": [], "write": [], "remove": []},
                    "name": name,
                    "type": "playlist",
                }
            await MongoDBHandler.update_user(user_id, {"$set": {f"playlist.{new_id}": entry}})
            return f"Playlist {name} created."

        if action == "playlist_delete":
            playlist_id = str(data.get("playlistId", "")).strip()
            if playlist_id == "200":
                raise DashboardError("The default playlist cannot be deleted.")
            playlists = await MongoDBHandler.get_user(user_id, d_type="playlist")
            playlist = playlists.get(playlist_id)
            if playlist is None:
                raise DashboardError("Playlist not found.", status=404)
            if playlist.get("type") == "share":
                await MongoDBHandler.update_user(
                    playlist["user"],
                    {"$pull": {f"playlist.{playlist['referId']}.perms.read": user_id}},
                )
            await MongoDBHandler.update_user(user_id, {"$unset": {f"playlist.{playlist_id}": 1}})
            return "Playlist deleted."

        if action == "playlist_rename":
            playlist_id = str(data.get("playlistId", "")).strip()
            name = str(data.get("name", "")).strip()
            if not name or len(name) > PLAYLIST_NAME_LIMIT:
                raise DashboardError(
                    f"Playlist names must be 1-{PLAYLIST_NAME_LIMIT} characters."
                )
            playlists = await MongoDBHandler.get_user(user_id, d_type="playlist")
            if playlist_id not in playlists:
                raise DashboardError("Playlist not found.", status=404)
            if any(
                pid != playlist_id and p.get("name", "").lower() == name.lower()
                for pid, p in playlists.items()
            ):
                raise DashboardError("A playlist with that name already exists.")
            await MongoDBHandler.update_user(
                user_id, {"$set": {f"playlist.{playlist_id}.name": name}}
            )
            return "Playlist renamed."

        if action == "playlist_add":
            playlist_id = str(data.get("playlistId", "")).strip()
            query = str(data.get("query", "")).strip()
            if not query or len(query) > 500:
                raise DashboardError("Enter a song name or URL (maximum 500 characters).")
            owner_id, owner_pid, playlist = await self._playlist_owner_ref(
                user_id, playlist_id, required_perm="write"
            )
            if playlist.get("type") == "link":
                raise DashboardError("Link playlists cannot be edited.")
            if len(playlist.get("tracks", [])) >= max_tracks:
                raise DashboardError(f"This playlist is full ({max_tracks} tracks).")
            node = NodePool.get_node()
            if node is None or not node.is_available:
                raise DashboardError("Lavalink is not ready.", status=503)
            result = await node.get_tracks(query=query, requester=requester)
            if not result:
                raise DashboardError("No tracks were found.", status=404)
            if isinstance(result, Playlist):
                raise DashboardError("Playlist links cannot be added as a single track.")
            track = result[0]
            if track.is_stream:
                raise DashboardError("Live streams cannot be saved to playlists.")
            await MongoDBHandler.update_user(
                owner_id, {"$push": {f"playlist.{owner_pid}.tracks": track.track_id}}
            )
            return f"Added {track.title} to the playlist."

        if action == "playlist_remove_track":
            playlist_id = str(data.get("playlistId", "")).strip()
            position = self._integer(data.get("position"), "Track position")
            owner_id, owner_pid, playlist = await self._playlist_owner_ref(
                user_id, playlist_id, required_perm="remove"
            )
            if playlist.get("type") == "link":
                raise DashboardError("Link playlists cannot be edited.")
            tracks = playlist.get("tracks", [])
            if not 1 <= position <= len(tracks):
                raise DashboardError("That track position does not exist.", status=404)
            await MongoDBHandler.update_user(
                owner_id,
                {"$pull": {f"playlist.{owner_pid}.tracks": tracks[position - 1]}},
            )
            return "Track removed from the playlist."

        if action == "playlist_clear":
            playlist_id = str(data.get("playlistId", "")).strip()
            owner_id, owner_pid, playlist = await self._playlist_owner_ref(
                user_id, playlist_id, required_perm="remove"
            )
            if playlist.get("type") == "link":
                raise DashboardError("Link playlists cannot be edited.")
            await MongoDBHandler.update_user(
                owner_id, {"$set": {f"playlist.{owner_pid}.tracks": []}}
            )
            return "Playlist cleared."

        if action == "playlist_share":
            playlist_id = str(data.get("playlistId", "")).strip()
            target_id = self._integer(data.get("targetUserId"), "Target user id")
            if target_id == user_id:
                raise DashboardError("You cannot share a playlist with yourself.")
            target_user = self.bot.get_user(target_id)
            if target_user is not None and getattr(target_user, "bot", False):
                raise DashboardError("Bots cannot receive playlists.")
            playlists = await MongoDBHandler.get_user(user_id, d_type="playlist")
            playlist = playlists.get(playlist_id)
            if playlist is None:
                raise DashboardError("Playlist not found.", status=404)
            if playlist.get("type") == "share":
                raise DashboardError("Shared playlists cannot be re-shared.")
            if target_id in playlist.get("perms", {}).get("read", []):
                raise DashboardError("That user already has access to this playlist.")
            inbox = await MongoDBHandler.get_user(target_id, d_type="inbox")
            if len(inbox) >= INBOX_LIMIT:
                raise DashboardError("The recipient's inbox is full.")
            if any(
                mail.get("sender") == user_id and mail.get("referId") == playlist_id
                for mail in inbox
            ):
                raise DashboardError("An invitation is already pending.")
            sender = self.bot.get_user(user_id)
            sender_name = getattr(sender, "display_name", None) or str(user_id)
            await MongoDBHandler.update_user(target_id, {"$push": {"inbox": {
                "sender": user_id,
                "referId": playlist_id,
                "time": time.time(),
                "title": f"Playlist invitation from {sender_name}",
                "description": (
                    "You are invited to use this playlist.\n"
                    f"Playlist Name: {playlist.get('name', playlist_id)}\n"
                    f"Playlist type: {playlist.get('type', 'playlist')}"
                ),
                "type": "invite",
            }}})
            return "Invitation sent."

        if action == "inbox_accept":
            index = self._integer(data.get("index"), "Inbox index")
            inbox = await MongoDBHandler.get_user(user_id, d_type="inbox")
            if not 0 <= index < len(inbox):
                raise DashboardError("That invitation no longer exists.", status=404)
            mail = inbox[index]
            playlists = await MongoDBHandler.get_user(user_id, d_type="playlist")
            if len(playlists) >= max_playlists:
                raise DashboardError(f"You can keep at most {max_playlists} playlists.")
            new_id = next(
                (str(i) for i in range(200, 210) if str(i) not in playlists), None
            )
            if new_id is None:
                raise DashboardError("No playlist slots are available.")
            await MongoDBHandler.update_user(
                mail["sender"],
                {"$push": {f"playlist.{mail['referId']}.perms.read": user_id}},
            )
            remaining = [m for i, m in enumerate(inbox) if i != index]
            await MongoDBHandler.update_user(user_id, {"$set": {
                f"playlist.{new_id}": {
                    "user": mail["sender"],
                    "referId": mail["referId"],
                    "name": f"Share{time.strftime('%M%S', time.gmtime(int(mail.get('time', 0))))}",
                    "type": "share",
                },
                "inbox": remaining,
            }})
            return "Invitation accepted."

        if action == "inbox_decline":
            index = self._integer(data.get("index"), "Inbox index")
            inbox = await MongoDBHandler.get_user(user_id, d_type="inbox")
            if not 0 <= index < len(inbox):
                raise DashboardError("That invitation no longer exists.", status=404)
            remaining = [m for i, m in enumerate(inbox) if i != index]
            await MongoDBHandler.update_user(user_id, {"$set": {"inbox": remaining}})
            return "Invitation declined."

        if action == "playlist_permission":
            playlist_id = str(data.get("playlistId", "")).strip()
            permission = str(data.get("permission", "")).strip().lower()
            perm_action = str(data.get("permAction", "")).strip().lower()
            target_id = self._integer(data.get("targetUserId"), "Target user id")
            if permission not in {"read", "write", "remove"}:
                raise DashboardError("Permission must be read, write, or remove.")
            if perm_action not in {"grant", "revoke"}:
                raise DashboardError("Permission action must be grant or revoke.")
            if target_id == user_id:
                raise DashboardError("You cannot change your own permissions.")
            playlists = await MongoDBHandler.get_user(user_id, d_type="playlist")
            playlist = playlists.get(playlist_id)
            if playlist is None:
                raise DashboardError("Playlist not found.", status=404)
            if playlist.get("type") != "playlist":
                raise DashboardError("Permissions only apply to your own playlists.")
            perms = playlist.get("perms", {})
            if perm_action == "grant":
                if target_id in perms.get(permission, []):
                    raise DashboardError("That user already has this permission.")
                if permission != "read" and target_id not in perms.get("read", []):
                    raise DashboardError("Grant read access first.")
                await MongoDBHandler.update_user(
                    user_id,
                    {"$push": {f"playlist.{playlist_id}.perms.{permission}": target_id}},
                )
                return "Permission granted."
            if target_id not in perms.get(permission, []):
                raise DashboardError("That user does not have this permission.")
            await MongoDBHandler.update_user(
                user_id,
                {"$pull": {f"playlist.{playlist_id}.perms.{permission}": target_id}},
            )
            if permission == "read":
                await MongoDBHandler.update_user(
                    user_id,
                    {"$pull": {f"playlist.{playlist_id}.perms.write": target_id}},
                )
                await MongoDBHandler.update_user(
                    user_id,
                    {"$pull": {f"playlist.{playlist_id}.perms.remove": target_id}},
                )
            return "Permission revoked."

        raise DashboardError("Unsupported dashboard action.", status=404)
