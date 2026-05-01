import ast
import unittest
from pathlib import Path


class TestSyntax(unittest.TestCase):
    def test_repo_python_files_parse(self):
        files = [
            Path("telegram_bot.py"),
            Path("bot_relay.py"),
            Path("common_config.py"),
            Path("command_utils.py"),
            Path("telegram_link_utils.py"),
            Path("delivery.py"),
            Path("structured_logger.py"),
            Path("twitter_expand.py"),
            Path("twitter_expansion.py"),
            Path("twitter_watch.py"),
            Path("route_filter_utils.py"),
            Path("route_manager.py"),
            Path("scripts/sync_forum_topics.py"),
            Path("scripts/dedupe_forum_topics.py"),
            Path("scripts/export_forum_topic_ids.py"),
        ]
        for p in files:
            with self.subTest(file=str(p)):
                src = p.read_text(encoding="utf-8")
                ast.parse(src, filename=str(p))


if __name__ == "__main__":
    unittest.main()
