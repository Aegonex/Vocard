import asyncio
import aiohttp
import importlib
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from cogs.listeners import Listeners
from voicelink.ipc.client import IPCClient
from voicelink.mongodb import MongoDBHandler
from voicelink.player import Player
from voicelink.pool import Node, NodePool
from voicelink.transformer import DEFAULT_DECODER_MAPPING, decode_lavasrc_fields


def import_main_with_test_env():
    env = {
        "DISCORD_TOKEN": "test-token",
        "MONGODB_URL": "mongodb://localhost:27017",
        "LAVALINK_HOST": "localhost",
        "LAVALINK_PASSWORD": "secret",
        "BOT_PREFIX": "null",
        "LOGGING_JSON": '{"file":{"enable":false}}',
    }
    with patch.dict(os.environ, env, clear=True):
        return importlib.import_module("main")


class RuntimeReliabilityTests(unittest.IsolatedAsyncioTestCase):
    def test_all_lavasrc_sources_use_the_extended_track_decoder(self) -> None:
        for source in (
            "spotify",
            "applemusic",
            "deezer",
            "yandexmusic",
            "vkmusic",
            "tidal",
            "qobuz",
            "jiosaavn",
        ):
            self.assertIs(DEFAULT_DECODER_MAPPING[source], decode_lavasrc_fields)

    async def test_failed_node_creation_closes_its_session(self) -> None:
        session = MagicMock()
        session.closed = False
        session.close = AsyncMock()
        bot = MagicMock()
        bot.user.id = 123
        NodePool._nodes.clear()

        with (
            patch("voicelink.pool.aiohttp.ClientSession", return_value=session),
            patch.object(Node, "connect", AsyncMock(side_effect=RuntimeError("offline"))),
        ):
            with self.assertRaisesRegex(RuntimeError, "offline"):
                await NodePool.create_node(
                    bot=bot,
                    host="lavalink",
                    port=2333,
                    password="secret",
                    identifier="DEFAULT",
                )

        session.close.assert_awaited_once()
        self.assertNotIn("DEFAULT", NodePool._nodes)

    async def test_listener_fails_startup_when_all_nodes_are_offline(self) -> None:
        listener = object.__new__(Listeners)
        listener.bot = MagicMock()
        listener.voicelink = MagicMock()
        listener.voicelink.nodes = {}
        listener.voicelink.create_node = AsyncMock(
            side_effect=ConnectionError("offline")
        )
        config = SimpleNamespace(
            nodes={
                "DEFAULT": {
                    "host": "lavalink",
                    "port": 2333,
                    "password": "secret",
                    "identifier": "DEFAULT",
                }
            },
            dependency_startup_retries=2,
            dependency_retry_delay=0,
            dependency_connect_timeout=1,
        )

        with patch("cogs.listeners.Config", return_value=config):
            with self.assertRaisesRegex(RuntimeError, "No Lavalink node"):
                await listener.start_nodes()

        self.assertEqual(listener.voicelink.create_node.await_count, 2)

    async def test_listener_recovers_when_node_succeeds_on_retry(self) -> None:
        listener = object.__new__(Listeners)
        listener.bot = MagicMock()
        listener.voicelink = MagicMock()
        listener.voicelink.nodes = {}
        listener._node_supervisors = {}
        ready_node = SimpleNamespace(is_available=True)

        async def create_node(**kwargs):
            if listener.voicelink.create_node.await_count == 1:
                raise ConnectionError("not ready")
            listener.voicelink.nodes[kwargs["identifier"]] = ready_node
            return ready_node

        listener.voicelink.create_node = AsyncMock(side_effect=create_node)
        config = SimpleNamespace(
            nodes={
                "DEFAULT": {
                    "host": "lavalink",
                    "port": 2333,
                    "password": "secret",
                    "identifier": "DEFAULT",
                }
            },
            dependency_startup_retries=2,
            dependency_retry_delay=0,
            dependency_connect_timeout=1,
        )

        with patch("cogs.listeners.Config", return_value=config):
            await listener.start_nodes()

        self.assertEqual(listener.voicelink.create_node.await_count, 2)
        self.assertEqual(listener._node_supervisors, {})

    async def test_unavailable_secondary_node_gets_background_supervisor(self) -> None:
        listener = object.__new__(Listeners)
        listener.bot = MagicMock()
        listener.voicelink = MagicMock()
        listener.voicelink.nodes = {}
        listener._node_supervisors = {}
        attempts = {"PRIMARY": 0, "SECONDARY": 0}

        async def create_node(**kwargs):
            identifier = kwargs["identifier"]
            attempts[identifier] += 1
            if identifier == "SECONDARY" and attempts[identifier] <= 2:
                raise ConnectionError("offline")
            node = SimpleNamespace(is_available=True)
            listener.voicelink.nodes[identifier] = node
            return node

        listener.voicelink.create_node = AsyncMock(side_effect=create_node)
        base_node = {"host": "lavalink", "port": 2333, "password": "secret"}
        config = SimpleNamespace(
            nodes={
                "PRIMARY": {**base_node, "identifier": "PRIMARY"},
                "SECONDARY": {**base_node, "identifier": "SECONDARY"},
            },
            dependency_startup_retries=2,
            dependency_retry_delay=0,
            dependency_connect_timeout=1,
        )

        with patch("cogs.listeners.Config", return_value=config):
            await listener.start_nodes()
            task = listener._node_supervisors["SECONDARY"]
            await asyncio.wait_for(task, timeout=1)

        self.assertTrue(listener.voicelink.nodes["SECONDARY"].is_available)
        self.assertEqual(attempts["SECONDARY"], 3)

    async def test_mongodb_uses_timeout_and_closes_cleanly(self) -> None:
        client = MagicMock()
        client.server_info = AsyncMock(return_value={"ok": 1})
        client.admin.command = AsyncMock(return_value={"ok": 1})
        client.__getitem__.return_value = MagicMock()
        MongoDBHandler._lock = asyncio.Lock()
        await MongoDBHandler.close()

        with patch("voicelink.mongodb.AsyncIOMotorClient", return_value=client) as factory:
            await MongoDBHandler.init(
                "mongodb://mongo:27017", "vocard", timeout_seconds=7
            )

        self.assertTrue(MongoDBHandler.is_ready())
        self.assertEqual(factory.call_args.kwargs["serverSelectionTimeoutMS"], 7000)
        self.assertEqual(factory.call_args.kwargs["connectTimeoutMS"], 7000)
        self.assertEqual(factory.call_args.kwargs["socketTimeoutMS"], 7000)
        self.assertEqual(factory.call_args.kwargs["waitQueueTimeoutMS"], 7000)
        self.assertTrue(await MongoDBHandler.ping(timeout_seconds=1))

        client.admin.command.side_effect = asyncio.TimeoutError
        self.assertFalse(await MongoDBHandler.ping(timeout_seconds=1))
        self.assertFalse(MongoDBHandler.is_ready())

        await MongoDBHandler.close()
        self.assertFalse(MongoDBHandler.is_ready())
        client.close.assert_called_once()

    async def test_ipc_disconnect_is_safe_before_connect(self) -> None:
        bot = MagicMock()
        bot.user.id = 123
        client = IPCClient(bot, "localhost", 8000, "secret")

        await client.disconnect()

        self.assertFalse(client.is_connected)

    async def test_ipc_listener_reconnects_after_closed_websocket(self) -> None:
        bot = MagicMock()
        bot.user.id = 123
        client = IPCClient(bot, "localhost", 8000, "secret")
        closed_message = SimpleNamespace(type=1)
        closed_message.type = aiohttp.WSMsgType.CLOSED
        client._websocket = MagicMock()
        receive_blocker = asyncio.Event()
        receive_calls = 0

        async def receive():
            nonlocal receive_calls
            receive_calls += 1
            if receive_calls == 1:
                return closed_message
            await receive_blocker.wait()

        client._websocket.receive = AsyncMock(side_effect=receive)
        reconnected = asyncio.Event()

        async def reopen() -> None:
            client._is_connected = True
            reconnected.set()

        with (
            patch.object(
                client, "_open_websocket", AsyncMock(side_effect=reopen)
            ) as open_websocket,
            patch("voicelink.ipc.client.asyncio.sleep", AsyncMock()),
        ):
            task = asyncio.create_task(client._listen())
            await asyncio.wait_for(reconnected.wait(), timeout=1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        open_websocket.assert_awaited()

    def test_node_pool_never_routes_to_connected_but_unavailable_node(self) -> None:
        unavailable = MagicMock(is_available=False)
        unavailable.players = {}
        available = MagicMock(is_available=True)
        available.players = {1: MagicMock()}
        NodePool._nodes = {"offline": unavailable, "ready": available}
        try:
            self.assertIs(NodePool.get_node(), available)
        finally:
            NodePool._nodes.clear()

    async def test_health_requires_discord_mongo_and_lavalink(self) -> None:
        env = {
            "DISCORD_TOKEN": "test-token",
            "MONGODB_URL": "mongodb://localhost:27017",
            "LAVALINK_HOST": "localhost",
            "LAVALINK_PASSWORD": "secret",
            "BOT_PREFIX": "null",
            "LOGGING_JSON": '{"file":{"enable":false}}',
        }
        with patch.dict(os.environ, env, clear=True):
            main = importlib.import_module("main")

        ready_node = SimpleNamespace(is_available=True)
        with (
            patch.object(main.Vocard, "is_ready", return_value=True),
            patch.object(main.Vocard, "is_closed", return_value=False),
            patch.object(main.MongoDBHandler, "is_ready", return_value=True),
            patch.object(main.NodePool, "_nodes", {"DEFAULT": ready_node}),
        ):
            self.assertEqual(main.bot.health_status()["status"], "ok")

        with patch.object(main.MongoDBHandler, "is_ready", return_value=False):
            self.assertEqual(main.bot.health_status()["status"], "starting")

    async def test_health_handler_refreshes_mongodb_status(self) -> None:
        env = {
            "DISCORD_TOKEN": "test-token",
            "MONGODB_URL": "mongodb://localhost:27017",
            "LAVALINK_HOST": "localhost",
            "LAVALINK_PASSWORD": "secret",
            "BOT_PREFIX": "null",
            "LOGGING_JSON": '{"file":{"enable":false}}',
        }
        with patch.dict(os.environ, env, clear=True):
            main = importlib.import_module("main")
        healthy = {
            "status": "ok",
            "discord": True,
            "mongodb": True,
            "lavalink": True,
        }
        starting = {**healthy, "status": "starting", "mongodb": False}

        with (
            patch.object(main.MongoDBHandler, "ping", AsyncMock(return_value=True)) as ping,
            patch.object(main.bot, "health_status", return_value=healthy),
        ):
            response = await main.bot._health_check(MagicMock())
        self.assertEqual(response.status, 200)
        ping.assert_awaited_once()

        with (
            patch.object(main.MongoDBHandler, "ping", AsyncMock(return_value=False)),
            patch.object(main.bot, "health_status", return_value=starting),
        ):
            response = await main.bot._health_check(MagicMock())
        self.assertEqual(response.status, 503)

    async def test_global_startup_deadline_cancels_an_unready_client(self) -> None:
        main = import_main_with_test_env()

        class NeverReadyClient:
            def __init__(self):
                self.start_cancelled = False
                self.context_closed = False

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                self.context_closed = True

            async def start(self, token, reconnect=True):
                try:
                    await asyncio.Event().wait()
                finally:
                    self.start_cancelled = True

            async def wait_until_ready(self):
                await asyncio.Event().wait()

        client = NeverReadyClient()
        with patch.object(main.bot_config, "startup_timeout", 0.01):
            with self.assertRaisesRegex(RuntimeError, "before Discord became ready"):
                await main.run_bot(client, "test-token")

        self.assertTrue(client.start_cancelled)
        self.assertTrue(client.context_closed)

    async def test_shutdown_closes_discord_before_bounded_resource_cleanup(self) -> None:
        main = import_main_with_test_env()
        bot = object.__new__(main.Vocard)
        bot._vocard_closing = False
        bot._health_runner = None
        bot.ipc_client = None
        order = []

        async def close_discord(_):
            order.append("discord")

        async def stalled_mongodb_close():
            order.append("mongodb")
            await asyncio.Event().wait()

        with (
            patch.object(main.commands.Bot, "close", close_discord),
            patch.object(
                main.MongoDBHandler,
                "close",
                AsyncMock(side_effect=stalled_mongodb_close),
            ),
            patch.object(main, "RESOURCE_SHUTDOWN_TIMEOUT", 0.01),
            patch.object(main.NodePool, "_nodes", {}),
        ):
            await asyncio.wait_for(bot.close(), timeout=0.2)

        self.assertEqual(order[:2], ["discord", "mongodb"])

    async def test_player_teardown_propagates_cancellation(self) -> None:
        player = object.__new__(Player)
        player._guild = SimpleNamespace(id=123)
        player.settings = {"played_time": 0}
        player.joinTime = time.time()
        player._ipc_client = SimpleNamespace(_is_connected=False)
        player._ipc_connection = False
        update_started = asyncio.Event()

        async def stalled_update(*args, **kwargs):
            update_started.set()
            await asyncio.Event().wait()

        with patch.object(
            MongoDBHandler,
            "update_settings",
            AsyncMock(side_effect=stalled_update),
        ):
            task = asyncio.create_task(player.teardown())
            await asyncio.wait_for(update_started.wait(), timeout=1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task


class SignalShutdownTests(unittest.TestCase):
    @unittest.skipUnless(hasattr(signal, "SIGTERM"), "SIGTERM is unavailable")
    def test_sigterm_closes_the_bot_before_process_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ready_path = Path(temp_dir) / "ready"
            closed_path = Path(temp_dir) / "closed"
            script = """
import asyncio
import os
from pathlib import Path
import main

ready_path = Path(os.environ["VOCARD_TEST_READY"])
closed_path = Path(os.environ["VOCARD_TEST_CLOSED"])

async def fake_start(token, reconnect=True):
    ready_path.write_text("ready", encoding="utf8")
    await asyncio.Event().wait()

async def fake_close():
    closed_path.write_text("closed", encoding="utf8")

main.bot.start = fake_start
main.bot.close = fake_close
asyncio.run(main.run_bot(main.bot, "test-token"))
"""
            env = os.environ.copy()
            env.update(
                {
                    "DISCORD_TOKEN": "test-token",
                    "MONGODB_URL": "mongodb://localhost:27017",
                    "LAVALINK_HOST": "localhost",
                    "LAVALINK_PASSWORD": "secret",
                    "BOT_PREFIX": "null",
                    "LOGGING_JSON": '{"file":{"enable":false}}',
                    "VOCARD_TEST_READY": str(ready_path),
                    "VOCARD_TEST_CLOSED": str(closed_path),
                }
            )
            process = subprocess.Popen(
                [sys.executable, "-c", script],
                cwd=Path(__file__).resolve().parent.parent,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                deadline = time.monotonic() + 10
                while (
                    not ready_path.exists()
                    and process.poll() is None
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.05)

                self.assertTrue(ready_path.exists(), "child process did not become ready")
                process.send_signal(signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, stdout + stderr)
                self.assertTrue(closed_path.exists(), stdout + stderr)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
