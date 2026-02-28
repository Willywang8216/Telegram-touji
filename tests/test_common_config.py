import json
import os
import tempfile
import unittest
from pathlib import Path

from common_config import ConfigManager, load_relay_settings, load_userbot_settings


class TestCommonConfig(unittest.TestCase):
    def setUp(self):
        # Tests should not depend on the caller environment (e.g. docker compose env_file or repo .env).
        self._saved_env = {}
        for key in [
            "API_ID",
            "API_HASH",
            "MASTER_ACCOUNT_ID",
            "RELAY_API_ID",
            "RELAY_API_HASH",
            "RELAY_BOT_TOKEN",
            "RELAY_DEST_CHANNELS",
            "RELAY_ALLOWED_SENDER_IDS",
            "ADMIN_API_ID",
            "ADMIN_API_HASH",
            "ADMIN_BOT_TOKEN",
            "ADMIN_BOT_ADMIN_USER_IDS",
        ]:
            self._saved_env[key] = os.environ.get(key)
            os.environ.pop(key, None)

        self._saved_cwd = os.getcwd()
        self.tmp = tempfile.TemporaryDirectory()
        # Prevent ConfigManager._load_dotenv() from reading /app/.env by changing CWD.
        os.chdir(self.tmp.name)

        self.path = Path(self.tmp.name) / "config.json"
        self.path.write_text(
            json.dumps(
                {
                    "api_id": 1,
                    "api_hash": "h",
                    "master_account_id": 2,
                    "bot_mappings": [{"source_chat": -1, "target_bot": "@bot"}],
                    "relay": {
                        "api_id": 1,
                        "api_hash": "h",
                        "bot_token": "token",
                        "dest_channels": [-100],
                    },
                    "proxy": None,
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        try:
            os.chdir(self._saved_cwd)
        finally:
            self.tmp.cleanup()

        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_load_userbot_settings(self):
        m = ConfigManager(str(self.path))
        s = load_userbot_settings(m)
        self.assertEqual(s["api_id"], 1)
        self.assertEqual(s["master_account_id"], 2)

    def test_env_override(self):
        os.environ["RELAY_DEST_CHANNELS"] = "-200,-300"
        m = ConfigManager(str(self.path))
        s = load_relay_settings(m)
        self.assertEqual(s["dest_channels"], [-200, -300])

    def test_routes_without_default_destinations(self):
        self.path.write_text(
            json.dumps(
                {
                    "api_id": 1,
                    "api_hash": "h",
                    "master_account_id": 2,
                    "bot_mappings": [{"source_chat": -1, "target_bot": "@bot"}],
                    "relay": {
                        "api_id": 1,
                        "api_hash": "h",
                        "bot_token": "token",
                        "routes": [
                            {"source_chat": -1001, "destinations": [{"chat_id": -100}]},
                        ],
                    },
                    "proxy": None,
                }
            ),
            encoding="utf-8",
        )
        m = ConfigManager(str(self.path))
        s = load_relay_settings(m)
        self.assertEqual(len(s["routes"]), 1)

    def test_save(self):
        m = ConfigManager(str(self.path))
        cfg = m.load()
        cfg["master_account_id"] = 999
        m.save(cfg)
        cfg2 = m.load(force=True)
        self.assertEqual(cfg2["master_account_id"], 999)


if __name__ == "__main__":
    unittest.main()
