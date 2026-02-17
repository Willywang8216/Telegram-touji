import unittest

# Importing from scripts is OK in this repo because we run tests from repo root.
from scripts.config_wizard import _parse_range_list, _parse_source  # noqa: PLC2701


class TestConfigWizardParsing(unittest.TestCase):
    def test_parse_source_int(self):
        self.assertEqual(_parse_source("-100123"), -100123)

    def test_parse_source_username(self):
        self.assertEqual(_parse_source("@mychannel"), "@mychannel")

    def test_parse_range_list_single(self):
        self.assertEqual(_parse_range_list("3", n=10), [3])

    def test_parse_range_list_multi(self):
        self.assertEqual(_parse_range_list("1,2,5-7", n=10), [1, 2, 5, 6, 7])

    def test_parse_range_list_all(self):
        self.assertEqual(_parse_range_list("all", n=3), [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
