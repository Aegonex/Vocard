import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

import requests
import update


class UpdaterConfigTests(unittest.TestCase):
    def test_version_check_is_non_fatal_when_github_is_unavailable(self) -> None:
        with patch(
            "update.requests.get",
            side_effect=requests.ConnectionError("offline"),
        ):
            self.assertEqual(update.check_version(), update.__version__)

    def test_standard_user_configuration_is_preserved(self) -> None:
        self.assertIn(".env", update.IGNORE_FILES)
        self.assertIn("settings.json", update.IGNORE_FILES)

    def test_nested_custom_settings_can_be_backed_up_and_restored(self) -> None:
        original_path = update.CUSTOM_SETTINGS_PATH
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                settings_path = Path(temp_dir) / "config" / "production.json"
                settings_path.parent.mkdir()
                settings_path.write_text('{"secret":"preserved"}', encoding="utf8")
                update.CUSTOM_SETTINGS_PATH = str(settings_path)

                backup_path = update.backup_custom_settings()
                settings_path.unlink()
                update.restore_custom_settings(backup_path)

                self.assertEqual(
                    settings_path.read_text(encoding="utf8"),
                    '{"secret":"preserved"}',
                )
        finally:
            update.CUSTOM_SETTINGS_PATH = original_path


if __name__ == "__main__":
    unittest.main()
