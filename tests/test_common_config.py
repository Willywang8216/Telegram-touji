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
                        "default_dest_channels": [-101],
                        "routes_by_source": {
                            "-10": [-200, {"dest_chat": -300, "topic": "Topic A"}]
                        },
                    },
                    "proxy": None,
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()
        for key in ["RELAY_DEST_CHANNELS", "API_ID"]:
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
        # default_dest_channels comes from config if explicitly set.
        self.assertEqual(s["default_dest_channels"], [-101])

    def test_relay_extra_fields(self):
        m = ConfigManager(str(self.path))
        s = load_relay_settings(m)
        self.assertEqual(s["default_dest_channels"], [-101])
        # legacy relay.routes_by_source format is accepted.
        self.assertEqual(
            s["routes_by_source"].get(-10),
            [-200, {"dest_chat": -300, "topic": "Topic A"}],
        )

    def test_routes_parsing(self):
        cfg = json.loads(self.path.read_text(encoding="utf-8"))
        cfg["relay"].pop("routes_by_source", None)
        cfg["relay"]["routes"] = [
            {"sources": [-1001, -1002], "dest_chat": -9999, "topic": "Asian Bear"},
            {"sources": [-1001], "dest_chat": -8888},
        ]
        cfg["relay"]["transform"] = {"remove_mentions": True}
        cfg["relay"]["translation"] = {"enabled": False}
        self.path.write_text(json.dumps(cfg), encoding="utf-8")

        m = ConfigManager(str(self.path))
        s = load_relay_settings(m)
        self.assertEqual(
            s["routes_by_source"],
            {
                -1001: [{"dest_chat": -9999, "topic": "Asian Bear"}, {"dest_chat": -8888, "topic": None}],
                -1002: [{"dest_chat": -9999, "topic": "Asian Bear"}],
            },
        )
        self.assertEqual(s["transform"], {"remove_mentions": True})
        self.assertEqual(s["translation"], {"enabled": False})

    def test_save(self):
        m = ConfigManager(str(self.path))
        cfg = m.load()
        cfg["master_account_id"] = 999
        m.save(cfg)
        cfg2 = m.load(force=True)
        self.assertEqual(cfg2["master_account_id"], 999)


if __name__ == "__main__":
    unittest.main()
