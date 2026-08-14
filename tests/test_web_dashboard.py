import unittest

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from voicelink.enums import LoopType
from voicelink.filters import Filters
from voicelink.web_dashboard import AUDIO_MODE_LABELS, DashboardError, WebDashboard


class WebDashboardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.guild = SimpleNamespace(
            id=123,
            name="Music Room",
            icon=None,
            member_count=42,
            me=SimpleNamespace(),
            voice_channels=[],
            stage_channels=[],
            voice_client=None,
        )
        self.bot = SimpleNamespace(
            user=SimpleNamespace(
                id=999,
                display_name="Test Vocard",
                display_avatar=SimpleNamespace(url="https://example.com/avatar.png"),
            ),
            latency=0.025,
            guilds=[self.guild],
            health_status=lambda: {
                "status": "ok",
                "discord": True,
                "mongodb": True,
                "lavalink": True,
            },
            get_guild=lambda guild_id: self.guild if guild_id == 123 else None,
        )
        app = web.Application()
        self.dashboard = WebDashboard(self.bot, "admin")
        self.dashboard.register(app)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()

    async def test_index_is_available_without_exposing_bot_state(self) -> None:
        response = await self.client.get("/")
        self.assertEqual(response.status, 200)
        html = await response.text()
        self.assertIn("ควบคุมบอท", html)
        self.assertIn("ADMIN PASSWORD", html)
        self.assertIn('data-audio-mode="nightcore"', html)
        self.assertIn('data-audio-mode="8d"', html)
        self.assertNotIn('id="channelHint"', html)
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")

    async def test_api_requires_the_admin_password(self) -> None:
        response = await self.client.get("/api/state")
        self.assertEqual(response.status, 401)
        self.assertEqual((await response.json())["code"], "unauthorized")

    async def test_authorized_state_is_scoped_to_one_bot(self) -> None:
        response = await self.client.get(
            "/api/state",
            headers={"Authorization": "Bearer admin"},
        )
        payload = await response.json()

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["bot"]["name"], "Test Vocard")
        self.assertEqual(payload["selectedGuildId"], "123")
        self.assertEqual([guild["id"] for guild in payload["guilds"]], ["123"])

    async def test_empty_voice_channel_is_marked_ready_for_use(self) -> None:
        permissions_for = MagicMock(
            return_value=SimpleNamespace(view_channel=True, connect=True)
        )
        self.guild.voice_channels = [
            SimpleNamespace(
                id=456,
                name="Empty Room",
                members=[SimpleNamespace(bot=True)],
                permissions_for=permissions_for,
            ),
            SimpleNamespace(
                id=789,
                name="Occupied Room",
                members=[SimpleNamespace(bot=False)],
                permissions_for=permissions_for,
            ),
        ]

        response = await self.client.get(
            "/api/state",
            headers={"Authorization": "Bearer admin"},
        )
        channels = (await response.json())["guilds"][0]["voiceChannels"]

        self.assertEqual(channels[0]["listeners"], 0)
        self.assertEqual(channels[0]["status"], "empty")
        self.assertFalse(channels[0]["isBotConnected"])
        self.assertEqual(channels[1]["listeners"], 1)
        self.assertEqual(channels[1]["status"], "available")
        self.assertFalse(channels[1]["isBotConnected"])

    async def test_dashboard_script_uses_voice_channel_select_status(self) -> None:
        response = await self.client.get("/assets/app.js")
        script = await response.text()

        self.assertEqual(response.status, 200)
        self.assertIn("เลือกห้องที่ต้องการเชื่อมต่อ", script)
        self.assertIn("กำลังเล่นเพลงอยู่ห้อง", script)
        self.assertIn("/api/state", script)
        self.assertIn("/api/playlists", script)
        self.assertIn("audio_mode", script)
        self.assertNotIn("channelHint", script)

    async def test_action_validation_does_not_report_a_guild_id_error(self) -> None:
        self.guild.voice_client = SimpleNamespace()
        response = await self.client.post(
            "/api/action",
            headers={"Authorization": "Bearer admin"},
            json={"action": "volume", "guildId": "123", "volume": "loud"},
        )
        payload = await response.json()

        self.assertEqual(response.status, 400)
        self.assertEqual(payload["message"], "Volume must be a whole number.")


class DashboardMusicActionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.requester = SimpleNamespace(display_name="Dashboard")
        self.track = SimpleNamespace(
            track_id="track-1",
            title="Current song",
            author="Artist",
            uri="https://example.com/song",
            thumbnail=None,
            length=180_000,
            is_stream=False,
            source="youtube",
            requester=self.requester,
        )
        self.upcoming = [SimpleNamespace(
            track_id=f"next-{index}",
            title=f"Next song {index}",
            author="Artist",
            uri=f"https://example.com/next-{index}",
            thumbnail=None,
            length=180_000,
            is_stream=False,
            source="youtube",
            requester=self.requester,
        ) for index in range(3)]
        self.history = [SimpleNamespace(track_id="previous-1")]
        self.queue = SimpleNamespace(
            _repeat=SimpleNamespace(mode=LoopType.OFF),
            repeat="Off",
            tracks=MagicMock(return_value=self.upcoming),
            history=MagicMock(return_value=self.history),
            backto=MagicMock(),
        )
        self.player = SimpleNamespace(
            current=self.track,
            queue=self.queue,
            channel=SimpleNamespace(id=456, name="Music", members=[]),
            position=20_000,
            is_playing=True,
            is_paused=False,
            volume=100,
            settings={"autoplay": False},
            filters=Filters(),
            set_pause=AsyncMock(),
            set_repeat=AsyncMock(return_value=LoopType.TRACK),
            stop=AsyncMock(),
            do_next=AsyncMock(),
            set_volume=AsyncMock(),
            seek=AsyncMock(),
            shuffle=AsyncMock(),
            clear_queue=AsyncMock(),
            remove_track=AsyncMock(return_value={1: self.upcoming[0]}),
            reset_filter=AsyncMock(),
            add_filter=AsyncMock(),
            teardown=AsyncMock(),
        )
        self.guild = SimpleNamespace(id=123, me=self.requester, voice_client=self.player)
        self.bot = SimpleNamespace(
            user=SimpleNamespace(
                id=999,
                display_name="Test Vocard",
                display_avatar=SimpleNamespace(url="https://example.com/avatar.png"),
            ),
            latency=0.01,
            guilds=[SimpleNamespace(
                id=123,
                name="Music Room",
                icon=None,
                member_count=10,
                me=self.requester,
                voice_channels=[],
                stage_channels=[],
                voice_client=self.player,
            )],
            health_status=lambda: {
                "status": "ok",
                "discord": True,
                "mongodb": True,
                "lavalink": True,
            },
        )
        self.dashboard = WebDashboard(self.bot, "admin")

    async def test_transport_and_queue_buttons_call_player_actions(self) -> None:
        self.queue._repeat.mode = LoopType.TRACK
        self.assertEqual(
            await self.dashboard._run_action(self.guild, {"action": "pause", "pause": True}),
            "Playback paused.",
        )
        await self.dashboard._run_action(self.guild, {"action": "skip"})
        await self.dashboard._run_action(self.guild, {"action": "previous"})
        await self.dashboard._run_action(self.guild, {"action": "repeat", "mode": "track"})
        await self.dashboard._run_action(self.guild, {"action": "shuffle"})
        await self.dashboard._run_action(self.guild, {"action": "clear"})
        await self.dashboard._run_action(self.guild, {"action": "remove", "index": 1})

        self.player.set_pause.assert_awaited_once_with(True, self.requester)
        self.player.set_repeat.assert_any_await(LoopType.OFF, self.requester)
        self.player.set_repeat.assert_any_await(LoopType.TRACK, self.requester)
        self.player.stop.assert_awaited()
        self.queue.backto.assert_called_once_with(2)
        self.player.shuffle.assert_awaited_once_with("queue", self.requester)
        self.player.clear_queue.assert_awaited_once_with("queue", self.requester)
        self.player.remove_track.assert_awaited_once_with(1, requester=self.requester)

    async def test_connect_play_and_disconnect_buttons_call_player_actions(self) -> None:
        channel = SimpleNamespace(id=456, name="Music")
        node = SimpleNamespace(
            is_available=True,
            get_tracks=AsyncMock(return_value=[self.track]),
        )
        self.player.add_track = AsyncMock(return_value=1)
        self.player.is_playing = False

        with (
            patch.object(self.dashboard, "_voice_channel", return_value=channel),
            patch.object(self.dashboard, "_ensure_player", new=AsyncMock(return_value=self.player)),
            patch("voicelink.web_dashboard.NodePool.get_node", return_value=node),
        ):
            connected = await self.dashboard._run_action(
                self.guild, {"action": "connect", "voiceChannelId": "456"}
            )
            played = await self.dashboard._run_action(
                self.guild,
                {"action": "play", "voiceChannelId": "456", "query": "test song"},
            )
            disconnected = await self.dashboard._run_action(
                self.guild, {"action": "disconnect"}
            )

        self.assertEqual(connected, "Connected to Music.")
        self.assertEqual(played, "Added 1 track(s).")
        self.assertEqual(disconnected, "Disconnected from voice.")
        node.get_tracks.assert_awaited_once_with(query="test song", requester=self.requester)
        self.player.add_track.assert_awaited_once_with([self.track])
        self.player.do_next.assert_awaited_once()
        self.player.teardown.assert_awaited_once()

    async def test_mixer_seek_and_autoplay_actions(self) -> None:
        with patch(
            "voicelink.web_dashboard.MongoDBHandler.update_settings",
            new_callable=AsyncMock,
        ) as update_settings:
            await self.dashboard._run_action(
                self.guild, {"action": "volume", "volume": 175}
            )
            await self.dashboard._run_action(
                self.guild, {"action": "seek", "position": 45_000}
            )
            await self.dashboard._run_action(
                self.guild, {"action": "autoplay", "enabled": True}
            )

        self.player.set_volume.assert_awaited_once_with(175, self.requester)
        self.player.seek.assert_awaited_once_with(45_000, self.requester)
        self.assertTrue(self.player.settings["autoplay"])
        self.assertEqual(update_settings.await_count, 2)

    async def test_every_music_mode_uses_a_lavalink_filter(self) -> None:
        for mode in Filters.get_available_filters():
            with self.subTest(mode=mode):
                self.player.filters = Filters()
                self.player.add_filter.reset_mock()
                message = await self.dashboard._run_action(
                    self.guild, {"action": "audio_mode", "mode": mode}
                )

                applied = self.player.add_filter.await_args.args[0]
                self.assertEqual(applied.tag, mode)
                self.assertEqual(message, f"Music mode: {AUDIO_MODE_LABELS[mode]}.")

    async def test_normal_mode_resets_active_filters(self) -> None:
        nightcore = Filters.get_available_filters()["nightcore"]()
        self.player.filters.add_filter(filter=nightcore)

        message = await self.dashboard._run_action(
            self.guild, {"action": "audio_mode", "mode": "normal"}
        )

        self.assertEqual(message, "Music mode: Normal.")
        self.player.reset_filter.assert_awaited_once_with(requester=self.requester)
        self.player.add_filter.assert_not_awaited()

    async def test_invalid_button_states_return_clear_errors(self) -> None:
        self.player.current = None
        self.queue.tracks.return_value = []
        self.queue.history.return_value = []

        cases = [
            ({"action": "pause", "pause": True}, "Nothing is playing right now."),
            ({"action": "skip"}, "There is no track to skip."),
            ({"action": "previous"}, "There is no previous track."),
            ({"action": "shuffle"}, "Add at least 3 upcoming tracks before shuffling."),
        ]
        for data, expected in cases:
            with self.subTest(action=data["action"]):
                with self.assertRaisesRegex(DashboardError, expected):
                    await self.dashboard._run_action(self.guild, data)

    def test_state_exposes_music_mode_and_button_capabilities(self) -> None:
        nightcore = Filters.get_available_filters()["nightcore"]()
        self.player.filters.add_filter(filter=nightcore)
        self.bot.guilds[0].voice_channels = [SimpleNamespace(
            id=456,
            name="Music",
            members=[SimpleNamespace(bot=False), SimpleNamespace(bot=True)],
            permissions_for=lambda _: SimpleNamespace(view_channel=False, connect=False),
        )]

        state = self.dashboard._state_payload(self.bot.guilds[0])
        payload = state["player"]
        channel = state["guilds"][0]["voiceChannels"][0]

        self.assertEqual(payload["audioMode"], "nightcore")
        self.assertEqual(payload["historyCount"], 1)
        self.assertEqual(payload["activeFilters"], ["nightcore"])
        self.assertEqual(channel["listeners"], 1)
        self.assertEqual(channel["status"], "connected")
        self.assertTrue(channel["isBotConnected"])
        self.assertEqual(payload["capabilities"], {
            "canPause": True,
            "canSkip": True,
            "canPrevious": True,
            "canShuffle": True,
            "canClear": True,
            "canSeek": True,
        })


class DashboardExtendedActionTests(unittest.IsolatedAsyncioTestCase):
    """Coverage for the web-only command surface added on top of the original panel."""

    def setUp(self) -> None:
        self.requester = SimpleNamespace(id=999, display_name="Dashboard")
        self.track = SimpleNamespace(
            track_id="track-1",
            title="Current song",
            author="Artist",
            uri="https://example.com/song",
            thumbnail=None,
            length=180_000,
            is_stream=False,
            source="youtube",
            requester=self.requester,
        )
        self.upcoming = [SimpleNamespace(
            track_id=f"next-{index}",
            title=f"Next song {index}",
            author="Artist",
            uri=f"https://example.com/next-{index}",
            thumbnail=None,
            length=60_000,
            is_stream=False,
            source="youtube",
            requester=self.requester,
        ) for index in range(2)]
        self.queue = SimpleNamespace(
            _repeat=SimpleNamespace(mode=LoopType.OFF),
            repeat="Off",
            tracks=MagicMock(return_value=self.upcoming),
            history=MagicMock(return_value=[self.track]),
            backto=MagicMock(),
            _allow_duplicate=True,
        )
        self.player = SimpleNamespace(
            current=self.track,
            queue=self.queue,
            channel=SimpleNamespace(id=456, name="Music", members=[]),
            position=20_000,
            is_playing=True,
            is_paused=False,
            volume=100,
            settings={"autoplay": False},
            filters=Filters(),
            set_pause=AsyncMock(),
            set_repeat=AsyncMock(return_value=LoopType.OFF),
            stop=AsyncMock(),
            do_next=AsyncMock(),
            set_volume=AsyncMock(),
            seek=AsyncMock(),
            shuffle=AsyncMock(),
            clear_queue=AsyncMock(),
            remove_track=AsyncMock(return_value={}),
            reset_filter=AsyncMock(),
            add_filter=AsyncMock(),
            remove_filter=AsyncMock(),
            add_track=AsyncMock(return_value=1),
            teardown=AsyncMock(),
        )
        self.role = SimpleNamespace(id=777, name="DJ")
        self.guild = SimpleNamespace(
            id=123,
            name="Music Room",
            me=self.requester,
            voice_client=self.player,
            get_role=lambda role_id: self.role if role_id == 777 else None,
        )
        self.bot = SimpleNamespace(
            user=SimpleNamespace(
                id=999,
                display_name="Test Vocard",
                display_avatar=SimpleNamespace(url="https://example.com/avatar.png"),
            ),
            latency=0.01,
            guilds=[SimpleNamespace(
                id=123,
                name="Music Room",
                icon=None,
                member_count=10,
                me=self.requester,
                voice_channels=[],
                stage_channels=[],
                voice_client=self.player,
            )],
            get_user=lambda user_id: None,
            health_status=lambda: {
                "status": "ok",
                "discord": True,
                "mongodb": True,
                "lavalink": True,
            },
        )
        self.dashboard = WebDashboard(self.bot, "admin")

    async def test_play_top_and_force_modes(self) -> None:
        channel = SimpleNamespace(id=456, name="Music")
        node = SimpleNamespace(is_available=True, get_tracks=AsyncMock(return_value=[self.track]))
        with (
            patch.object(self.dashboard, "_voice_channel", return_value=channel),
            patch.object(self.dashboard, "_ensure_player", new=AsyncMock(return_value=self.player)),
            patch("voicelink.web_dashboard.NodePool.get_node", return_value=node),
        ):
            message = await self.dashboard._run_action(
                self.guild, {"action": "play", "query": "song", "mode": "top"}
            )
            self.assertEqual(message, "Added 1 track(s) to the top of the queue.")
            self.player.add_track.assert_awaited_with([self.track], at_front=True)

            self.queue._repeat.mode = LoopType.TRACK
            message = await self.dashboard._run_action(
                self.guild, {"action": "play", "query": "song", "mode": "force"}
            )
            self.assertEqual(message, "Force playing 1 track(s).")
            self.player.set_repeat.assert_awaited_with(LoopType.OFF, self.requester)
            self.player.stop.assert_awaited()

    async def test_single_video_url_is_resolved_via_search(self) -> None:
        channel = SimpleNamespace(id=456, name="Music")
        wrong = SimpleNamespace(identifier="other", title="Wrong", is_stream=False)
        right = SimpleNamespace(identifier="82-jTNka3uc", title="Right", is_stream=False)
        node = SimpleNamespace(
            is_available=True,
            get_tracks=AsyncMock(return_value=[wrong, right]),
        )
        self.player.is_playing = False
        with (
            patch.object(self.dashboard, "_voice_channel", return_value=channel),
            patch.object(self.dashboard, "_ensure_player", new=AsyncMock(return_value=self.player)),
            patch("voicelink.web_dashboard.NodePool.get_node", return_value=node),
        ):
            await self.dashboard._run_action(
                self.guild,
                {"action": "play", "query": "https://youtu.be/82-jTNka3uc?si=abc"},
            )
        # The blocked single-video load path is rerouted through ytsearch by id...
        node.get_tracks.assert_awaited_once_with(query="ytsearch:82-jTNka3uc", requester=self.requester)
        # ...and the exact-id match is queued, not merely the first search hit.
        self.player.add_track.assert_awaited_with([right])

    async def test_state_includes_history_and_settings(self) -> None:
        with patch(
            "voicelink.web_dashboard.MongoDBHandler.get_cached_settings",
            return_value={"lang": "TH", "volume": 80, "24/7": True},
        ):
            state = self.dashboard._state_payload(self.bot.guilds[0])

        self.assertEqual(len(state["player"]["history"]), 1)
        self.assertEqual(state["player"]["history"][0]["title"], "Current song")
        self.assertEqual(state["settings"]["language"], "TH")
        self.assertEqual(state["settings"]["defaultVolume"], 80)
        self.assertTrue(state["settings"]["playForever"])
        self.assertIn("commandsEnabled", state)

    async def test_relative_seek_clamps_to_track_bounds(self) -> None:
        await self.dashboard._run_action(self.guild, {"action": "forward", "offset": 10_000})
        self.player.seek.assert_awaited_with(30_000, self.requester)

        await self.dashboard._run_action(self.guild, {"action": "rewind", "offset": 60_000})
        self.player.seek.assert_awaited_with(0, self.requester)

        await self.dashboard._run_action(self.guild, {"action": "replay"})
        self.player.seek.assert_awaited_with(0, self.requester)

    async def test_effect_set_replaces_and_validates(self) -> None:
        message = await self.dashboard._run_action(
            self.guild, {"action": "effect_set", "effect": "speed", "params": {"speed": 1.5}}
        )
        self.assertEqual(message, "Effect applied: speed.")
        applied = self.player.add_filter.await_args.args[0]
        self.assertEqual(applied.tag, "speed")

        with self.assertRaisesRegex(DashboardError, "Speed must be between 0 and 2."):
            await self.dashboard._run_action(
                self.guild, {"action": "effect_set", "effect": "speed", "params": {"speed": 9}}
            )

    async def test_effect_clear_paths(self) -> None:
        message = await self.dashboard._run_action(self.guild, {"action": "effect_clear"})
        self.assertEqual(message, "No effects are active.")

        nightcore = Filters.get_available_filters()["nightcore"]()
        self.player.filters.add_filter(filter=nightcore)
        message = await self.dashboard._run_action(
            self.guild, {"action": "effect_clear", "tag": "nightcore"}
        )
        self.assertEqual(message, "Effect removed: nightcore.")
        self.player.remove_filter.assert_awaited_with("nightcore", self.requester)

    async def test_settings_update_writes_expected_keys(self) -> None:
        with patch(
            "voicelink.web_dashboard.MongoDBHandler.update_settings", new_callable=AsyncMock
        ) as update_settings:
            await self.dashboard._run_action(
                self.guild,
                {"action": "settings_update", "field": "play_forever", "value": True},
            )
            update_settings.assert_awaited_with(123, {"$set": {"24/7": True}})

            await self.dashboard._run_action(
                self.guild,
                {"action": "settings_update", "field": "dj_role", "value": 777},
            )
            update_settings.assert_awaited_with(123, {"$set": {"dj": 777}})

            await self.dashboard._run_action(
                self.guild,
                {"action": "settings_update", "field": "default_volume", "value": 120},
            )
            update_settings.assert_awaited_with(123, {"$set": {"volume": 120}})
            self.player.set_volume.assert_awaited_with(120, self.requester)

            with self.assertRaisesRegex(DashboardError, "Queue mode must be"):
                await self.dashboard._run_action(
                    self.guild,
                    {"action": "settings_update", "field": "queue_mode", "value": "stack"},
                )

    async def test_settings_reset_applies_side_effects(self) -> None:
        with patch(
            "voicelink.web_dashboard.MongoDBHandler.update_settings", new_callable=AsyncMock
        ) as update_settings:
            await self.dashboard._run_action(
                self.guild, {"action": "settings_reset", "field": "default_volume"}
            )
            update_settings.assert_awaited_with(123, {"$unset": {"volume": None}})
            self.player.set_volume.assert_awaited_with(100, self.requester)

    async def test_queue_export_returns_track_ids(self) -> None:
        self.queue.tracks = MagicMock(return_value=[self.track, *self.upcoming])
        message, data = await self.dashboard._run_action(self.guild, {"action": "queue_export"})
        self.assertEqual(message, "Queue exported.")
        self.assertEqual([item["id"] for item in data["tracks"]],
                         ["track-1", "next-0", "next-1"])

    async def test_queue_import_builds_tracks(self) -> None:
        channel = SimpleNamespace(id=456, name="Music")
        decoded = {
            "identifier": "abc", "title": "Imported", "author": "Someone",
            "length": 1000, "uri": "https://example.com", "sourceName": "youtube",
            "isStream": False, "isSeekable": True, "position": 0, "artworkUrl": None,
        }
        with (
            patch.object(self.dashboard, "_voice_channel", return_value=channel),
            patch.object(self.dashboard, "_ensure_player", new=AsyncMock(return_value=self.player)),
            patch("voicelink.web_dashboard.Track.decode", return_value=decoded),
        ):
            message = await self.dashboard._run_action(
                self.guild, {"action": "queue_import", "trackIds": ["id-1", "id-2"]}
            )
        self.assertEqual(message, "Imported 2 track(s).")
        tracks = self.player.add_track.await_args.args[0]
        self.assertEqual(len(tracks), 2)
        self.assertEqual(tracks[0].title, "Imported")

    async def test_playlist_create_and_add(self) -> None:
        playlists = {"200": {"tracks": [], "perms": {"read": [], "write": [], "remove": []},
                             "name": "Favourite", "type": "playlist"}}
        node = SimpleNamespace(is_available=True, get_tracks=AsyncMock(return_value=[self.track]))
        with (
            patch("voicelink.web_dashboard.MongoDBHandler.get_user",
                  new=AsyncMock(return_value=playlists)),
            patch("voicelink.web_dashboard.MongoDBHandler.update_user",
                  new_callable=AsyncMock) as update_user,
            patch("voicelink.web_dashboard.NodePool.get_node", return_value=node),
            patch("voicelink.web_dashboard._playlist_limits", return_value=(5, 500, "Favourite")),
        ):
            message = await self.dashboard._run_action(
                self.guild,
                {"action": "playlist_create", "userId": "42", "name": "Chill"},
            )
            self.assertEqual(message, "Playlist Chill created.")
            update_user.assert_awaited_with(42, {"$set": {"playlist.201": {
                "tracks": [], "perms": {"read": [], "write": [], "remove": []},
                "name": "Chill", "type": "playlist",
            }}})

            message = await self.dashboard._run_action(
                self.guild,
                {"action": "playlist_add", "userId": "42", "playlistId": "200", "query": "song"},
            )
            self.assertEqual(message, "Added Current song to the playlist.")
            update_user.assert_awaited_with(
                42, {"$push": {"playlist.200.tracks": "track-1"}}
            )

    async def test_playlist_share_and_inbox_accept(self) -> None:
        playlists = {"200": {"tracks": ["track-1"],
                             "perms": {"read": [], "write": [], "remove": []},
                             "name": "Favourite", "type": "playlist"}}
        mail = {"sender": 42, "referId": "200", "time": 1_000_000.0,
                "title": "Playlist invitation from 42", "description": "", "type": "invite"}

        async def fake_get_user(user_id, *, d_type=None, **kwargs):
            if d_type == "inbox":
                return [mail] if user_id == 77 else []
            return dict(playlists) if user_id in (42, 77) else {}

        with (
            patch("voicelink.web_dashboard.MongoDBHandler.get_user", new=AsyncMock(side_effect=fake_get_user)),
            patch("voicelink.web_dashboard.MongoDBHandler.update_user", new_callable=AsyncMock) as update_user,
            patch("voicelink.web_dashboard._playlist_limits", return_value=(5, 500, "Favourite")),
        ):
            message = await self.dashboard._run_action(
                self.guild,
                {"action": "playlist_share", "userId": "42",
                 "playlistId": "200", "targetUserId": 55},
            )
            self.assertEqual(message, "Invitation sent.")
            push = update_user.await_args.args[1]["$push"]["inbox"]
            self.assertEqual(push["sender"], 42)
            self.assertEqual(push["referId"], "200")

            message = await self.dashboard._run_action(
                self.guild, {"action": "inbox_accept", "userId": "77", "index": 0}
            )
            self.assertEqual(message, "Invitation accepted.")
            set_data = update_user.await_args.args[1]["$set"]
            share_entry = set_data["playlist.201"]
            self.assertEqual(share_entry["user"], 42)
            self.assertEqual(share_entry["referId"], "200")
            self.assertEqual(set_data["inbox"], [])


if __name__ == "__main__":
    unittest.main()
