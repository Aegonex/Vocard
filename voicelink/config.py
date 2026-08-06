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

import copy
import json
import logging
import os

from pathlib import Path
from dotenv import load_dotenv
from typing import (
    Dict,
    List,
    Any,
    Union,
    Optional
)

from .enums import SearchType

WORKING_DIR = Path(__file__).resolve().parent.parent
DEFAULT_SETTINGS_FILE = WORKING_DIR / "settings.default.json"

# An environment variable supplied by the process must win over the local .env file.
load_dotenv(WORKING_DIR / ".env", override=False)

_MISSING = object()


def _read_settings_file(path: Path, *, required: bool = False) -> Dict[str, Any]:
    """Read a JSON settings file and return an object at its root."""
    if not path.is_file():
        if required:
            raise FileNotFoundError(f"Configuration defaults were not found: {path}")
        return {}

    try:
        with path.open(encoding="utf8") as json_file:
            data = json.load(json_file)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid JSON in {path}: line {exc.lineno}, column {exc.colno}"
        ) from exc

    if not isinstance(data, dict):
        raise ValueError(f"Configuration file {path} must contain a JSON object.")
    return data


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Merge nested settings without mutating either input."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if value and isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _first_env(*names: str) -> tuple[Any, Optional[str]]:
    for name in names:
        if name in os.environ:
            return os.environ[name], name
    return _MISSING, None


def _parse_bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value

    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false (received {value!r}).")


def _parse_int(
    value: Any,
    name: str,
    *,
    default: Optional[int] = None,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer (received {value!r}).")
    if value is None or value == "":
        if default is None:
            raise ValueError(f"{name} must be an integer (received {value!r}).")
        return default

    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer (received {value!r}).") from exc

    if minimum is not None and parsed < minimum:
        raise ValueError(f"{name} must be at least {minimum}.")
    if maximum is not None and parsed > maximum:
        raise ValueError(f"{name} must be at most {maximum}.")
    return parsed


def _parse_json(value: str, name: str, expected_type: type) -> Any:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} contains invalid JSON: {exc.msg}.") from exc

    if not isinstance(parsed, expected_type):
        raise ValueError(f"{name} must contain a JSON {expected_type.__name__}.")
    return parsed


class Config:
    _instance: Optional['Config'] = None
    WORKING_DIR: Path = WORKING_DIR
    DEFAULT_SETTINGS_FILE: Path = DEFAULT_SETTINGS_FILE
    LAST_SESSION_FILE_DIR: Path = WORKING_DIR / "last-session.json"

    def __new__(cls, settings: Dict[str, Any] = None) -> 'Config':
        """
        Singleton pattern to ensure only one instance of Config exists.
        If settings are provided, creates a new instance that replaces the old one.
        
        Args:
            settings (Dict[str, Any], optional): A dictionary containing configuration settings. Defaults to None.
                                               If provided, creates a new instance that replaces the old one.
        """
        if settings is not None:
            instance = super(Config, cls).__new__(cls)
            instance.__init__(settings)
            cls._instance = instance
            return instance
            
        if cls._instance is None:
            cls._instance = super(Config, cls).__new__(cls)
            
        return cls._instance

    @classmethod
    def load(cls, settings_file: Optional[Union[str, Path]] = None) -> 'Config':
        """Load bundled defaults, then an optional settings file, then environment values."""
        settings = _read_settings_file(cls.DEFAULT_SETTINGS_FILE, required=True)

        explicit_path = settings_file is not None
        if settings_file is None:
            env_path, _ = _first_env("SETTINGS_FILE")
            if env_path is _MISSING:
                settings_file = "settings.json"
            else:
                settings_file = env_path
                explicit_path = True

        resolved_path: Optional[Path] = None
        if settings_file:
            resolved_path = Path(settings_file).expanduser()
            if not resolved_path.is_absolute():
                resolved_path = cls.WORKING_DIR / resolved_path

            if explicit_path and not resolved_path.is_file():
                raise FileNotFoundError(f"SETTINGS_FILE does not exist: {resolved_path}")
            if resolved_path.is_file():
                settings = _deep_merge(settings, _read_settings_file(resolved_path))

        instance = cls(settings)
        instance.settings_file = resolved_path if resolved_path and resolved_path.is_file() else None
        return instance

    @staticmethod
    def _value(
        settings: Dict[str, Any],
        key: str,
        *env_names: str,
        default: Any = None,
    ) -> tuple[Any, str]:
        env_value, env_name = _first_env(*env_names)
        if env_value is not _MISSING:
            return env_value, env_name
        return copy.deepcopy(settings.get(key, default)), key

    @staticmethod
    def _json_value(
        settings: Dict[str, Any],
        key: str,
        env_name: str,
        expected_type: type,
        *,
        default: Any,
        merge: bool = False,
    ) -> Any:
        current = copy.deepcopy(settings.get(key, default))
        if not isinstance(current, expected_type):
            raise ValueError(f"{key} must contain a JSON {expected_type.__name__}.")

        env_value, _ = _first_env(env_name)
        if env_value is _MISSING:
            return current

        parsed = _parse_json(env_value, env_name, expected_type)
        if merge and parsed and isinstance(current, dict):
            return _deep_merge(current, parsed)
        return parsed

    def __init__(self, settings: Dict[str, Any] = None) -> None:
        """Initialize a configuration instance from merged JSON settings and env."""
        if hasattr(self, 'initialized'):
            return

        settings = settings or {}

        value, _ = self._value(settings, "token", "DISCORD_TOKEN", "TOKEN", default="")
        self.token: str = str(value).strip() if value is not None else ""

        value, source = self._value(
            settings, "client_id", "DISCORD_CLIENT_ID", "CLIENT_ID", default=0
        )
        self.client_id: int = _parse_int(value, source, default=0, minimum=0)

        value, _ = self._value(settings, "genius_token", "GENIUS_TOKEN", default="")
        self.genius_token: str = str(value).strip() if value is not None else ""

        value, _ = self._value(
            settings, "mongodb_url", "MONGODB_URL", "MONGO_URL", default=""
        )
        self.mongodb_url: str = str(value).strip() if value is not None else ""

        value, _ = self._value(
            settings, "mongodb_name", "MONGODB_NAME", default="vocard"
        )
        self.mongodb_name: str = str(value).strip() if value is not None else ""

        value, _ = self._value(settings, "invite_link", "INVITE_LINK", default="https://discord.gg/wRCgB7vBQv")
        self.invite_link: str = str(value).strip()

        self.nodes: Dict[str, Dict[str, Any]] = self._json_value(
            settings, "nodes", "NODES_JSON", dict, default={}
        )
        lavalink_env_names = (
            "LAVALINK_HOST",
            "LAVALINK_PORT",
            "LAVALINK_PASSWORD",
            "LAVALINK_SECURE",
            "LAVALINK_IDENTIFIER",
        )
        nodes_json, _ = _first_env("NODES_JSON")
        if nodes_json is _MISSING and any(name in os.environ for name in lavalink_env_names):
            default_identifier, default_node = next(iter(self.nodes.items()), ("DEFAULT", {}))
            node = copy.deepcopy(default_node)

            value, _ = _first_env("LAVALINK_IDENTIFIER")
            identifier = str(
                node.get("identifier", default_identifier) if value is _MISSING else value
            ).strip()

            value, _ = _first_env("LAVALINK_HOST")
            if value is not _MISSING:
                node["host"] = value

            value, source = _first_env("LAVALINK_PORT")
            node["port"] = _parse_int(
                node.get("port", 2333) if value is _MISSING else value,
                "LAVALINK_PORT" if source else "nodes.port",
                minimum=1,
                maximum=65535,
            )

            value, _ = _first_env("LAVALINK_PASSWORD")
            if value is not _MISSING:
                node["password"] = value

            value, source = _first_env("LAVALINK_SECURE")
            node["secure"] = _parse_bool(
                node.get("secure", False) if value is _MISSING else value,
                source or "nodes.secure",
            )
            node["identifier"] = identifier
            self.nodes = {identifier: node}

        normalized_nodes: Dict[str, Dict[str, Any]] = {}
        for key, raw_node in self.nodes.items():
            if not isinstance(raw_node, dict):
                raise ValueError(f"Node {key!r} must contain a JSON object.")
            node = copy.deepcopy(raw_node)
            identifier = str(node.get("identifier") or key).strip()
            node["identifier"] = identifier
            node["port"] = _parse_int(
                node.get("port", 2333),
                f"nodes.{identifier}.port",
                minimum=1,
                maximum=65535,
            )
            node["secure"] = _parse_bool(
                node.get("secure", False), f"nodes.{identifier}.secure"
            )
            normalized_nodes[identifier] = node
        self.nodes = normalized_nodes

        value, source = self._value(
            settings, "default_max_queue", "DEFAULT_MAX_QUEUE", "MAX_QUEUE", default=1000
        )
        self.max_queue: int = _parse_int(value, source, default=1000, minimum=1)

        value, source = self._value(
            settings,
            "default_search_platform",
            "DEFAULT_SEARCH_PLATFORM",
            "SEARCH_PLATFORM",
            default="youtube",
        )
        self.search_platform: Optional[SearchType] = SearchType.from_platform(str(value))
        if self.search_platform is None:
            raise ValueError(f"{source} contains an unsupported search platform: {value!r}.")

        value, source = self._value(
            settings, "prefix", "BOT_PREFIX", "PREFIX", default="?"
        )
        prefix_disabled = (
            source in {"BOT_PREFIX", "PREFIX"}
            and str(value).strip().lower() in {"null", "none", "disabled"}
        )
        self.bot_prefix: Optional[str] = (
            None if value is None or prefix_disabled else str(value)
        )

        self.activity: List[Dict[str, str]] = self._json_value(
            settings, "activity", "ACTIVITY_JSON", list, default=[]
        )
        self.logging: Dict[str, Any] = self._json_value(
            settings, "logging", "LOGGING_JSON", dict, default={}, merge=True
        )

        value, source = self._value(settings, "embed_color", "EMBED_COLOR", default="0xb3b3b3")
        try:
            self.embed_color: int = value if isinstance(value, int) else int(str(value), 16)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{source} must be a hexadecimal color (received {value!r}).") from exc
        if not 0 <= self.embed_color <= 0xFFFFFF:
            raise ValueError(f"{source} must be between 0x000000 and 0xFFFFFF.")

        self.bot_access_user: List[int] = copy.deepcopy(settings.get("bot_access_user", []))
        access_users, access_users_name = _first_env(
            "BOT_ACCESS_USERS", "BOT_ACCESS_USERS_JSON", "BOT_ACCESS_USER_JSON"
        )
        if access_users is not _MISSING:
            raw_access_users = str(access_users).strip()
            if not raw_access_users:
                self.bot_access_user = []
            elif raw_access_users.startswith("["):
                self.bot_access_user = _parse_json(raw_access_users, access_users_name, list)
            else:
                self.bot_access_user = [part.strip() for part in raw_access_users.split(",")]
        try:
            self.bot_access_user = [int(user_id) for user_id in self.bot_access_user]
        except (TypeError, ValueError) as exc:
            raise ValueError("BOT_ACCESS_USERS must contain only Discord user IDs.") from exc

        self.sources_settings: Dict[str, Dict[str, str]] = self._json_value(
            settings, "sources_settings", "SOURCES_SETTINGS_JSON", dict, default={}, merge=True
        )
        self.cooldowns_settings: Dict[str, List[int]] = self._json_value(
            settings, "cooldowns", "COOLDOWNS_JSON", dict, default={}, merge=True
        )
        self.aliases_settings: Dict[str, List[str]] = self._json_value(
            settings, "aliases", "ALIASES_JSON", dict, default={}, merge=True
        )
        self.controller: Dict[str, Any] = self._json_value(
            settings, "default_controller", "DEFAULT_CONTROLLER_JSON", dict, default={}, merge=True
        )

        value, _ = self._value(
            settings,
            "default_voice_status_template",
            "DEFAULT_VOICE_STATUS_TEMPLATE",
            default="",
        )
        self.voice_status_template: str = str(value)

        value, source = self._value(
            settings, "lyrics_platform", "LYRICS_PLATFORM", default="lrclib"
        )
        self.lyrics_platform: str = str(value).lower()
        supported_lyrics_platforms = {"a_zlyrics", "genius", "lyrist", "lrclib", "musixmatch"}
        if self.lyrics_platform not in supported_lyrics_platforms:
            raise ValueError(f"{source} contains an unsupported lyrics platform: {value!r}.")

        self.ipc_client: Dict[str, Any] = self._json_value(
            settings, "ipc_client", "IPC_CLIENT_JSON", dict, default={}, merge=True
        )
        self.ipc_client.setdefault("host", "127.0.0.1")
        self.ipc_client.setdefault("password", "")
        ipc_env_map = {
            "host": ("IPC_HOST",),
            "port": ("IPC_PORT",),
            "password": ("IPC_PASSWORD",),
            "secure": ("IPC_SECURE",),
            "enable": ("IPC_ENABLED", "IPC_ENABLE"),
        }
        for key, env_names in ipc_env_map.items():
            env_value, _ = _first_env(*env_names)
            if env_value is not _MISSING:
                self.ipc_client[key] = env_value
        self.ipc_client["port"] = _parse_int(
            self.ipc_client.get("port", 8000), "IPC_PORT", minimum=1, maximum=65535
        )
        self.ipc_client["secure"] = _parse_bool(
            self.ipc_client.get("secure", False), "IPC_SECURE"
        )
        self.ipc_client["enable"] = _parse_bool(
            self.ipc_client.get("enable", False), "IPC_ENABLED"
        )
        if "heartbeat" in self.ipc_client:
            self.ipc_client["heartbeat"] = _parse_int(
                self.ipc_client["heartbeat"], "ipc_client.heartbeat", minimum=1
            )

        self.playlist_settings: Dict[str, Union[str, int]] = self._json_value(
            settings, "playlist_settings", "PLAYLIST_SETTINGS_JSON", dict, default={}, merge=True
        )
        self.timer_settings: Dict[str, Union[int, float]] = self._json_value(
            settings, "timer_settings", "TIMER_SETTINGS_JSON", dict, default={}, merge=True
        )

        value, source = self._value(
            settings,
            "sync_commands_on_startup",
            "SYNC_COMMANDS_ON_STARTUP",
            default=True,
        )
        self.sync_commands_on_startup: bool = _parse_bool(value, source)

        value, source = self._value(
            settings,
            "dependency_startup_retries",
            "DEPENDENCY_STARTUP_RETRIES",
            default=5,
        )
        self.dependency_startup_retries: int = _parse_int(
            value, source, default=5, minimum=1, maximum=20
        )

        value, source = self._value(
            settings,
            "dependency_retry_delay",
            "DEPENDENCY_RETRY_DELAY",
            default=5,
        )
        self.dependency_retry_delay: int = _parse_int(
            value, source, default=5, minimum=0, maximum=300
        )

        value, source = self._value(
            settings,
            "dependency_connect_timeout",
            "DEPENDENCY_CONNECT_TIMEOUT",
            default=20,
        )
        self.dependency_connect_timeout: int = _parse_int(
            value, source, default=20, minimum=1, maximum=300
        )

        value, source = self._value(
            settings,
            "startup_timeout",
            "STARTUP_TIMEOUT",
            default=540,
        )
        self.startup_timeout: int = _parse_int(
            value, source, default=540, minimum=30, maximum=540
        )

        value, source = self._value(
            settings,
            "check_for_updates_on_startup",
            "CHECK_FOR_UPDATES_ON_STARTUP",
            default=False,
        )
        self.check_for_updates_on_startup: bool = _parse_bool(value, source)
        self.version: str = str(settings.get("version", ""))

        self.initialized = True

    def validate(self) -> None:
        """Fail early with one actionable error containing all missing settings."""
        errors: List[str] = []
        required_values = {
            "DISCORD_TOKEN (or TOKEN)": self.token,
            "MONGODB_URL (or MONGO_URL)": self.mongodb_url,
            "MONGODB_NAME": self.mongodb_name,
        }
        for name, value in required_values.items():
            if not value:
                errors.append(f"{name} is required")

        if not self.nodes:
            errors.append("at least one Lavalink node is required")
        for identifier, node in self.nodes.items():
            if not identifier:
                errors.append("each Lavalink node needs an identifier")
            if not isinstance(node.get("host"), str) or not node["host"].strip():
                errors.append(f"Lavalink node {identifier or '<unknown>'} needs a host")
            if not isinstance(node.get("password"), str) or not node["password"].strip():
                errors.append(f"Lavalink node {identifier or '<unknown>'} needs a password")

        if not self.activity:
            errors.append("activity must contain at least one item")
        for index, activity in enumerate(self.activity):
            if not isinstance(activity, dict):
                errors.append(f"activity[{index}] must be an object")
                continue
            if not isinstance(activity.get("name"), str) or not activity.get("name"):
                errors.append(f"activity[{index}].name is required")
            for key in ("type", "status"):
                if key in activity and not isinstance(activity[key], str):
                    errors.append(f"activity[{index}].{key} must be a string")

        log_file = self.logging.get("file", {})
        log_levels = self.logging.get("level", {})
        if not isinstance(log_file, dict):
            errors.append("logging.file must be an object")
        else:
            if "enable" in log_file and not isinstance(log_file["enable"], bool):
                errors.append("logging.file.enable must be true or false")
            if "path" in log_file and not isinstance(log_file["path"], str):
                errors.append("logging.file.path must be a string")
        if not isinstance(log_levels, dict):
            errors.append("logging.level must be an object")
        else:
            valid_level_names = logging.getLevelNamesMapping()
            for logger_name, level in log_levels.items():
                if isinstance(level, bool) or (
                    not isinstance(level, int)
                    and (not isinstance(level, str) or level not in valid_level_names)
                ):
                    errors.append(
                        f"logging.level.{logger_name} must be a valid level name or integer"
                    )
        max_history = self.logging.get("max_history", 30)
        if isinstance(max_history, bool) or not isinstance(max_history, int) or max_history < 0:
            errors.append("logging.max_history must be a non-negative integer")

        if not isinstance(self.controller.get("embeds"), dict):
            errors.append("default_controller.embeds must be an object")
        if not isinstance(self.controller.get("buttons"), list):
            errors.append("default_controller.buttons must be a list")
        else:
            for index, row in enumerate(self.controller["buttons"]):
                if not isinstance(row, dict):
                    errors.append(f"default_controller.buttons[{index}] must be an object")
                    continue
                for button_name, button in row.items():
                    if not isinstance(button, dict):
                        errors.append(
                            f"default_controller.buttons[{index}].{button_name} must be an object"
                        )
                        continue
                    states = button.get("states")
                    if states is not None and (
                        not isinstance(states, dict)
                        or any(not isinstance(state, dict) for state in states.values())
                    ):
                        errors.append(
                            f"default_controller.buttons[{index}].{button_name}.states "
                            "must be an object of state objects"
                        )

        other_source_settings = self.sources_settings.get("others", {})
        if not isinstance(other_source_settings, dict) or not other_source_settings.get("color"):
            errors.append("sources_settings.others.color is required")
        for source, source_settings in self.sources_settings.items():
            if not isinstance(source_settings, dict):
                errors.append(f"sources_settings.{source} must be an object")
                continue
            color = source_settings.get("color")
            if not isinstance(color, str) or not color:
                errors.append(f"sources_settings.{source}.color must be a hexadecimal string")
            else:
                try:
                    parsed_color = int(color, 16)
                    if not 0 <= parsed_color <= 0xFFFFFF:
                        raise ValueError
                except (TypeError, ValueError):
                    errors.append(f"sources_settings.{source}.color must be a hexadecimal string")

        for key, value in self.timer_settings.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                errors.append(f"timer_settings.{key} must be a positive number")

        for key in ("max_playlists", "max_tracks_per_playlist"):
            value = self.playlist_settings.get(key)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
            ):
                errors.append(f"playlist_settings.{key} must be a positive integer")
        playlist_name = self.playlist_settings.get("default_playlist_name")
        if playlist_name is not None and not isinstance(playlist_name, str):
            errors.append("playlist_settings.default_playlist_name must be a string")

        for command, cooldown in self.cooldowns_settings.items():
            if (
                not isinstance(cooldown, list)
                or len(cooldown) != 2
                or any(
                    isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0
                    for value in cooldown
                )
            ):
                errors.append(f"cooldowns.{command} must contain two positive numbers")

        for command, aliases in self.aliases_settings.items():
            if not isinstance(aliases, list) or any(not isinstance(alias, str) for alias in aliases):
                errors.append(f"aliases.{command} must be a list of strings")

        if self.lyrics_platform == "genius" and not self.genius_token:
            errors.append("GENIUS_TOKEN is required when LYRICS_PLATFORM=genius")

        ipc_host = self.ipc_client.get("host")
        ipc_password = self.ipc_client.get("password")
        ipc_enabled = self.ipc_client.get("enable")
        if not isinstance(ipc_host, str):
            errors.append("IPC_HOST must be a non-empty string")
        elif ipc_enabled and not ipc_host.strip():
            errors.append("IPC_HOST is required when IPC is enabled")
        if not isinstance(ipc_password, str):
            errors.append("IPC_PASSWORD must be a string")
        elif ipc_enabled and not ipc_password.strip():
            errors.append("IPC_PASSWORD is required when IPC is enabled")

        if errors:
            details = "\n".join(f"- {error}" for error in errors)
            raise ValueError(f"Invalid configuration:\n{details}")
    
    @classmethod
    def get_source_config(cls, source: str, type: str) -> Union[str, None]:
        """
        Get source configuration for a specific source and type.
        
        Args:
            source (str): The source identifier (e.g., 'youtube', 'spotify').
                            Case-insensitive and spaces are removed.
            type (str): The type of configuration to retrieve (e.g., 'emoji', 'color').
        
        Returns:
            Union[str, None]: The configuration value for the specified source and type.
                            Returns None if either the source or type doesn't exist.
        
        Example:
            >>> Config().get_source("youtube", "emoji")
            "🎵"
        """
        if not isinstance(source, str) or not isinstance(type, str):
            return None
            
        normalized_source: str = source.lower().strip().replace(" ", "")
        source_settings: dict[str, str] = cls._instance.sources_settings.get(
            normalized_source,
            cls._instance.sources_settings.get("others", {})
        )
        
        return source_settings.get(type)
    
    @classmethod
    def get_playlist_config(cls) -> tuple[int, int, str]:
        config = cls._instance.playlist_settings
        return config.get("max_playlists", 5), config.get("max_tracks_per_playlist", 500), config.get("default_playlist_name", "Favourite")
