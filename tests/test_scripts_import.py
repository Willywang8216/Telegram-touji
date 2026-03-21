import importlib
import unittest


class TestScriptsImport(unittest.TestCase):
    def test_scripts_import_without_connecting(self):
        # These scripts should be importable without making any Telethon connections.
        importlib.import_module("scripts.sync_forum_topics")
        importlib.import_module("scripts.dedupe_forum_topics")


if __name__ == "__main__":
    unittest.main()
