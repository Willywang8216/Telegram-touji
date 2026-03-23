import unittest

from bot_relay import RelayBot
from twitter_expand import extract_tweet_urls


class FakeConfigManager:
    def reload_if_changed(self):
        return False


class FakeRateLimiter:
    async def wait(self):
        return


class FakeClient:
    def __init__(self):
        self.calls = []

    async def send_message(self, chat_id, *, message=None, reply_to=None):
        self.calls.append(("send_message", chat_id, message, reply_to))

    async def send_file(self, chat_id, file, *, caption=None, reply_to=None):
        self.calls.append(("send_file", chat_id, file, caption, reply_to))


class FakeTweetResolver:
    def __init__(self, files):
        self.files = files
        self.seen_urls = []

    async def resolve(self, url: str):
        self.seen_urls.append(url)
        return self.files


class FakeMessage:
    def __init__(self, *, message_id=1, media=None, raw_text=""):
        self.id = message_id
        self.media = media
        self.raw_text = raw_text


class TestTwitterExpansionUrls(unittest.TestCase):
    def test_extract_tweet_urls_normalizes(self):
        text = "Check this (https://twitter.com/jack/status/20?s=21). Also https://example.com/x"
        self.assertEqual(extract_tweet_urls(text), ["https://twitter.com/jack/status/20"])

        text2 = "https://x.com/jack/status/20/photo/1"
        self.assertEqual(extract_tweet_urls(text2), ["https://x.com/jack/status/20"])

        text3 = "https://twitter.com/i/web/status/12345?t=abc"
        self.assertEqual(extract_tweet_urls(text3), ["https://twitter.com/i/web/status/12345"])

        text4 = "https://mobile.twitter.com/jack/status/20"
        self.assertEqual(extract_tweet_urls(text4), ["https://twitter.com/jack/status/20"])


class TestRelayBotTweetExpansion(unittest.IsolatedAsyncioTestCase):
    async def test_send_copy_expands_tweet_url_into_files(self):
        client = FakeClient()
        resolver = FakeTweetResolver(files=["F1.jpg", "F2.jpg"])
        bot = RelayBot(
            client,
            FakeConfigManager(),
            {"dest_channels": [-100], "master_account_id": 0},
            tweet_resolver=resolver,
            rate_limiter=FakeRateLimiter(),
        )

        msg = FakeMessage(message_id=10, media=None, raw_text="https://twitter.com/jack/status/20?s=21")
        await bot.send_copy(msg)

        self.assertEqual(resolver.seen_urls, ["https://twitter.com/jack/status/20"])
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0][0], "send_file")
        self.assertEqual(client.calls[0][1], -100)
        self.assertEqual(client.calls[0][2], ["F1", "F2"])


if __name__ == "__main__":
    unittest.main()
