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

        payload = self.dashboard._state_payload(self.bot.guilds[0])["player"]

        self.assertEqual(payload["audioMode"], "nightcore")
        self.assertEqual(payload["historyCount"], 1)
        self.assertEqual(payload["activeFilters"], ["nightcore"])
        self.assertEqual(payload["capabilities"], {
            "canPause": True,
            "canSkip": True,
            "canPrevious": True,
            "canShuffle": True,
            "canClear": True,
            "canSeek": True,
        })


if __name__ == "__main__":
    unittest.main()
