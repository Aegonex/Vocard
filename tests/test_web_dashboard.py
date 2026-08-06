import unittest

from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from voicelink.web_dashboard import WebDashboard


class WebDashboardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        guild = SimpleNamespace(
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
            guilds=[guild],
            health_status=lambda: {
                "status": "ok",
                "discord": True,
                "mongodb": True,
                "lavalink": True,
            },
            get_guild=lambda guild_id: guild if guild_id == 123 else None,
        )
        app = web.Application()
        WebDashboard(self.bot, "a-secure-dashboard-key").register(app)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()

    async def test_index_is_available_without_exposing_bot_state(self) -> None:
        response = await self.client.get("/")
        self.assertEqual(response.status, 200)
        self.assertIn("ควบคุมบอท", await response.text())
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")

    async def test_api_requires_the_instance_access_key(self) -> None:
        response = await self.client.get("/api/state")
        self.assertEqual(response.status, 401)
        self.assertEqual((await response.json())["code"], "unauthorized")

    async def test_authorized_state_is_scoped_to_one_bot(self) -> None:
        response = await self.client.get(
            "/api/state",
            headers={"Authorization": "Bearer a-secure-dashboard-key"},
        )
        payload = await response.json()

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["bot"]["name"], "Test Vocard")
        self.assertEqual(payload["selectedGuildId"], "123")
        self.assertEqual([guild["id"] for guild in payload["guilds"]], ["123"])


if __name__ == "__main__":
    unittest.main()
