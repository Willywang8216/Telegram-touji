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

    def test_load_forum_topic_management_fields(self):
        cfg = json.loads(self.path.read_text(encoding="utf-8"))
        cfg["relay"].update(
            {
                "distribute_unrouted_to_buckets": True,
                "unrouted_distribution_mode": "message",
                "general_topic_buckets": {"-100": ["A", "B"]},
                "manage_forum_topics": False,
                "allow_topic_creation": False,
                "ensure_forum_topics": [{"chat_id": "-100", "topics": ["T1", "T2"]}],
                "topic_renames": {"-100": {"Old": "New"}},
                "topic_deletes": {"-100": ["DeleteMe"]},
            }
        )
        self.path.write_text(json.dumps(cfg), encoding="utf-8")

        m = ConfigManager(str(self.path))
        s = load_relay_settings(m)

        self.assertTrue(s["distribute_unrouted_to_buckets"])
        self.assertEqual(s["unrouted_distribution_mode"], "message")
        self.assertEqual(s["general_topic_buckets"], {-100: ["A", "B"]})
        self.assertFalse(s["manage_forum_topics"])
        self.assertFalse(s["allow_topic_creation"])
        self.assertEqual(s["ensure_forum_topics"], [{"chat_id": -100, "topics": ["T1", "T2"]}])
        self.assertEqual(s["topic_renames"], {-100: {"Old": "New"}})
        self.assertEqual(s["topic_deletes"], {-100: ["DeleteMe"]})

    def test_save(self):
        m = ConfigManager(str(self.path))
        cfg = m.load()
        cfg["master_account_id"] = 999
        m.save(cfg)
        cfg2 = m.load(force=True)
        self.assertEqual(cfg2["master_account_id"], 999)


if __name__ == "__main__":
    unittest.main()
