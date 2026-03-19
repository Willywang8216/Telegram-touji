import json
import os
import tempfile
import unittest
from pathlib import Path

from common_config import ConfigManager, load_relay_settings, load_userbot_settings


class TestCommonConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
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
        self.tmp.cleanup()
        for key in ["RELAY_DEST_CHANNELS", "API_ID", "RELAY_MASTER_ACCOUNT_ID"]:
            os.environ.pop(key, None)

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

    def test_relay_master_account_default(self):
        m = ConfigManager(str(self.path))
        s = load_relay_settings(m)
        # Backwards-compatible default: disabled unless explicitly set.
        self.assertEqual(s["master_account_id"], 0)

    def test_relay_master_account_env_override(self):
        os.environ["RELAY_MASTER_ACCOUNT_ID"] = "123"
        m = ConfigManager(str(self.path))
        s = load_relay_settings(m)
        self.assertEqual(s["master_account_id"], 123)

    def test_save(self):
        m = ConfigManager(str(self.path))
        cfg = m.load()
        cfg["master_account_id"] = 999
        m.save(cfg)
        cfg2 = m.load(force=True)
        self.assertEqual(cfg2["master_account_id"], 999)


if __name__ == "__main__":
    unittest.main()
