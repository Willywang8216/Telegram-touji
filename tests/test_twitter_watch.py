import unittest

from twitter_watch import normalize_twitter_profile_url, tweet_id_from_url, _extract_tweet_entries, candidate_profile_urls


class TestTwitterWatchHelpers(unittest.TestCase):
    def test_normalize_profile_url(self):
        self.assertEqual(normalize_twitter_profile_url("Mastertpe1125"), "https://x.com/Mastertpe1125/media")
        self.assertEqual(normalize_twitter_profile_url("@Mastertpe1125"), "https://x.com/Mastertpe1125/media")
        self.assertEqual(
            normalize_twitter_profile_url("https://twitter.com/Mastertpe1125"),
            "https://x.com/Mastertpe1125/media",
        )
        self.assertEqual(
            normalize_twitter_profile_url("https://x.com/Mastertpe1125/media"),
            "https://x.com/Mastertpe1125/media",
        )

    def test_candidate_profile_urls(self):
        urls = candidate_profile_urls("https://x.com/SomeUser", media_only=True)
        self.assertIn("https://x.com/SomeUser/media", urls)
        self.assertIn("https://twitter.com/SomeUser/media", urls)
        self.assertIn("https://x.com/SomeUser", urls)
        self.assertIn("https://twitter.com/SomeUser", urls)

    def test_tweet_id_from_url(self):
        self.assertEqual(tweet_id_from_url("https://x.com/a/status/123"), "123")
        self.assertEqual(tweet_id_from_url("https://twitter.com/i/web/status/999"), "999")
        self.assertIsNone(tweet_id_from_url("https://x.com/a"))

    def test_extract_entries_best_effort(self):
        info = {
            "entries": [
                {
                    "webpage_url": "https://x.com/jack/status/20?s=21",
                    "description": "hello",
                },
                {
                    "id": "12345",
                    "uploader_id": "jack",
                    "title": "hi",
                },
            ]
        }
        out = _extract_tweet_entries(info)
        self.assertEqual(out[0]["url"], "https://x.com/jack/status/20")
        self.assertEqual(out[0]["text"], "hello")
        self.assertEqual(out[1]["url"], "https://x.com/jack/status/12345")
        self.assertEqual(out[1]["text"], "hi")


if __name__ == "__main__":
    unittest.main()
