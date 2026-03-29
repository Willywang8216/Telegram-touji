import unittest

from twitter_expand import _sanitize_netscape_cookies_text


class TestTwitterExpandCookies(unittest.TestCase):
    def test_sanitize_netscape_cookies_drops_bad_lines_and_adds_header(self):
        raw = """BADLINE
.twitter.com\tTRUE\t/\tTRUE\t0\tauth_token\tAAA
"""
        out = _sanitize_netscape_cookies_text(raw)
        self.assertIsNotNone(out)
        assert out is not None
        self.assertTrue(out.startswith("# Netscape HTTP Cookie File\n"))
        self.assertIn("auth_token\tAAA", out)
        self.assertNotIn("BADLINE", out)

    def test_sanitize_returns_none_when_no_valid_lines(self):
        self.assertIsNone(_sanitize_netscape_cookies_text("BAD\nALSO_BAD"))


if __name__ == "__main__":
    unittest.main()
