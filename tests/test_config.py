import json
import os
import re
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from voicelink.config import Config


class ConfigTests(unittest.TestCase):
    def required_env(self, **overrides: str) -> dict[str, str]:
        values = {
            "DISCORD_TOKEN": "env-token",
            "MONGODB_URL": "mongodb://localhost:27017",
            "MONGODB_NAME": "vocard-test",
        }
        values.update(overrides)
        return values

    def load_settings(self, data: dict, env: dict[str, str]) -> Config:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings_path = Path(temp_dir) / "settings.json"
            settings_path.write_text(json.dumps(data), encoding="utf8")
            with patch.dict(os.environ, env, clear=True):
                return Config.load(settings_path)

    def test_env_only_uses_bundled_defaults(self) -> None:
        env = self.required_env(
            LAVALINK_HOST="audio",
            LAVALINK_PORT="2444",
            LAVALINK_PASSWORD="secret",
            LAVALINK_SECURE="false",
            BOT_PREFIX="",
        )

        with patch.dict(os.environ, env, clear=True):
            config = Config.load("")
            config.validate()

        self.assertIsNone(config.settings_file)
        self.assertEqual(config.bot_prefix, "")
        self.assertEqual(config.nodes["DEFAULT"]["host"], "audio")
        self.assertEqual(config.nodes["DEFAULT"]["port"], 2444)
        self.assertTrue(config.controller["buttons"])
        self.assertIn("others", config.sources_settings)

    def test_env_overrides_partial_legacy_settings(self) -> None:
        config = self.load_settings(
            {
                "token": "json-token",
                "mongodb_url": "mongodb://json:27017",
                "mongodb_name": "json-db",
                "prefix": "!",
                "aliases": {},
                "logging": {"file": {"enable": False}},
                "ipc_client": {},
            },
            self.required_env(),
        )
        config.validate()

        self.assertEqual(config.token, "env-token")
        self.assertEqual(config.mongodb_url, "mongodb://localhost:27017")
        self.assertEqual(config.bot_prefix, "!")
        self.assertEqual(config.aliases_settings, {})
        self.assertFalse(config.logging["file"]["enable"])
        self.assertEqual(config.logging["file"]["path"], "./logs")
        self.assertEqual(config.ipc_client["host"], "127.0.0.1")
        self.assertIn("password", config.ipc_client)

    def test_nodes_json_takes_precedence_over_single_node_values(self) -> None:
        nodes = {
            "A": {"host": "a", "port": 2333, "password": "a", "secure": False},
            "B": {"host": "b", "port": 2444, "password": "b", "secure": True},
        }
        env = self.required_env(
            NODES_JSON=json.dumps(nodes),
            LAVALINK_HOST="single-node-value",
            LAVALINK_PORT="2555",
        )

        with patch.dict(os.environ, env, clear=True):
            config = Config.load("")
            config.validate()

        self.assertEqual(set(config.nodes), {"A", "B"})
        self.assertEqual(config.nodes["A"]["host"], "a")

    def test_blank_ports_are_rejected(self) -> None:
        for variable in ("LAVALINK_PORT", "IPC_PORT"):
            with self.subTest(variable=variable):
                with patch.dict(
                    os.environ, self.required_env(**{variable: ""}), clear=True
                ):
                    with self.assertRaisesRegex(ValueError, variable):
                        Config.load("")

    def test_invalid_nested_settings_fail_before_startup(self) -> None:
        config = self.load_settings(
            {"logging": {"file": None}, "activity": ["invalid"]},
            self.required_env(),
        )

        with self.assertRaisesRegex(ValueError, "logging.file must be an object"):
            config.validate()

        with self.assertRaisesRegex(ValueError, "activity must contain a JSON list"):
            self.load_settings({"activity": {}}, self.required_env())

    def test_invalid_runtime_shapes_are_reported(self) -> None:
        invalid_settings = (
            (
                {"logging": {"level": {"vocard": "NOT_A_LEVEL"}}},
                "logging.level.vocard",
            ),
            (
                {"sources_settings": {"youtube": {"color": 123}}},
                "sources_settings.youtube.color",
            ),
            (
                {"default_controller": {"buttons": [{"skip": "invalid"}]}},
                "default_controller.buttons[0].skip",
            ),
            (
                {"ipc_client": {"host": {}, "password": []}},
                "IPC_HOST must be a non-empty string",
            ),
        )

        for settings, expected_error in invalid_settings:
            with self.subTest(settings=settings):
                config = self.load_settings(settings, self.required_env())
                with self.assertRaisesRegex(ValueError, re.escape(expected_error)):
                    config.validate()

    def test_boolean_integer_values_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "nodes.DEFAULT.port must be an integer"):
            self.load_settings(
                {"nodes": {"DEFAULT": {"port": True}}}, self.required_env()
            )

    def test_missing_required_values_are_reported_together(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            config = Config.load("")
            with self.assertRaises(ValueError) as context:
                config.validate()

        message = str(context.exception)
        self.assertIn("DISCORD_TOKEN", message)
        self.assertIn("MONGODB_URL", message)
        self.assertIn("MONGODB_NAME", message)


if __name__ == "__main__":
    unittest.main()
