import unittest

from telethon import types, utils

from bot_relay import ExpandedMedia, RelayBot


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


class FakeSender:
    def __init__(self, is_self=False):
        self.is_self = is_self


class FakeForwardHeader:
    def __init__(self, from_id):
        self.from_id = from_id


class FakeMessage:
    def __init__(self, *, message_id=1, grouped_id=None, media=None, raw_text="", fwd_from=None):
        self.id = message_id
        self.grouped_id = grouped_id
        self.media = media
        self.raw_text = raw_text
        self.fwd_from = fwd_from


class FakeEvent:
    def __init__(self, *, chat_id, sender_id, raw_text, message, sender_is_self=False):
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.raw_text = raw_text
        self.message = message
        self._sender_is_self = sender_is_self

    async def get_sender(self):
        return FakeSender(is_self=self._sender_is_self)


class TestRelayBot(unittest.IsolatedAsyncioTestCase):
    async def test_text_message_relays(self):
        client = FakeClient()
        bot = RelayBot(
            client,
            FakeConfigManager(),
            {"dest_channels": [-100, -200], "master_account_id": 0},
            rate_limiter=FakeRateLimiter(),
        )

        msg = FakeMessage(message_id=10, media=None, raw_text="hello")
        event = FakeEvent(chat_id=1, sender_id=2, raw_text="hello", message=msg)
        await bot.handle(event)

        self.assertEqual(
            client.calls,
            [("send_message", -100, "hello", None), ("send_message", -200, "hello", None)],
        )

    async def test_media_message_relays_as_upload(self):
        client = FakeClient()
        bot = RelayBot(
            client,
            FakeConfigManager(),
            {"dest_channels": [-100], "master_account_id": 0},
            rate_limiter=FakeRateLimiter(),
        )

        msg = FakeMessage(message_id=10, media="MEDIA", raw_text="cap")
        event = FakeEvent(chat_id=1, sender_id=2, raw_text="cap", message=msg)
        await bot.handle(event)

        self.assertEqual(client.calls, [("send_file", -100, "MEDIA", "cap", None)])

    async def test_tweet_link_expands_to_media(self):
        client = FakeClient()
        bot = RelayBot(
            client,
            FakeConfigManager(),
            {"dest_channels": [-100], "master_account_id": 0},
            rate_limiter=FakeRateLimiter(),
        )

        async def fake_expand(original_text):
            return ExpandedMedia(
                files=["F1.jpg", "F2.jpg"],
                cleanup=lambda: None,
                url="https://x.com/i/web/status/1",
            )

        bot._maybe_expand_twitter_media = fake_expand

        msg = FakeMessage(message_id=10, media=None, raw_text="https://twitter.com/user/status/1")
        event = FakeEvent(chat_id=1, sender_id=2, raw_text=msg.raw_text, message=msg)
        await bot.handle(event)

        self.assertEqual(client.calls, [("send_file", -100, ["F1.jpg", "F2.jpg"], msg.raw_text, None)])

    async def test_routes_choose_destinations_by_source_chat_id(self):
        client = FakeClient()
        source_id = utils.get_peer_id(types.PeerChannel(123))
        bot = RelayBot(
            client,
            FakeConfigManager(),
            {
                "dest_channels": [-999],
                "master_account_id": 0,
                "routes": [
                    {
                        "source_chats": [source_id],
                        "destinations": [{"chat_id": -100, "topic_title": "TopicA"}],
                    }
                ],
                "default_destinations": [{"chat_id": -200}],
            },
            rate_limiter=FakeRateLimiter(),
        )

        msg = FakeMessage(
            message_id=10,
            media=None,
            raw_text="hello",
            fwd_from=FakeForwardHeader(types.PeerChannel(123)),
        )
        event = FakeEvent(chat_id=1, sender_id=2, raw_text="hello", message=msg)

        # Avoid hitting Telegram API in tests.
        async def fake_get_or_create_top_message_id(chat_id, title):
            return 42

        bot.topic_resolver.get_or_create_top_message_id = fake_get_or_create_top_message_id

        await bot.handle(event)
        self.assertEqual(client.calls, [("send_message", -100, "hello", 42)])

    async def test_embedded_source_chat_id_marker_routes_message_and_strips_prefix(self):
        client = FakeClient()
        source_id = 1234
        bot = RelayBot(
            client,
            FakeConfigManager(),
            {
                "dest_channels": [-999],
                "master_account_id": 0,
                "routes": [
                    {
                        "source_chats": [source_id],
                        "destinations": [{"chat_id": -100, "topic_title": "TopicA"}],
                    }
                ],
                "default_destinations": [{"chat_id": -200}],
            },
            rate_limiter=FakeRateLimiter(),
        )

        msg = FakeMessage(
            message_id=10,
            media=None,
            raw_text="\u2063SRC_CHAT_ID=1234\nhello",
            fwd_from=None,
        )
        event = FakeEvent(chat_id=1, sender_id=2, raw_text=msg.raw_text, message=msg)

        async def fake_get_or_create_top_message_id(chat_id, title):
            return 42

        bot.topic_resolver.get_or_create_top_message_id = fake_get_or_create_top_message_id

        await bot.handle(event)
        self.assertEqual(client.calls, [("send_message", -100, "hello", 42)])

    async def test_unrouted_sources_distributed_into_topic_buckets(self):
        client = FakeClient()
        bot = RelayBot(
            client,
            FakeConfigManager(),
            {
                "dest_channels": [-100],
                "master_account_id": 0,
                "distribute_unrouted_to_buckets": True,
                "general_topic_buckets": {
                    -100: ["T1", "T2", "T3", "T4", "T5"],
                },
            },
            rate_limiter=FakeRateLimiter(),
        )

        # source-based: abs(7) % 5 == 2 -> T3
        msg = FakeMessage(
            message_id=10,
            media=None,
            raw_text="hello",
            fwd_from=FakeForwardHeader(types.PeerChannel(7)),
        )
        event = FakeEvent(chat_id=1, sender_id=2, raw_text="hello", message=msg)

        async def fake_get_or_create_top_message_id(chat_id, title):
            return 77

        bot.topic_resolver.get_or_create_top_message_id = fake_get_or_create_top_message_id

        await bot.handle(event)
        self.assertEqual(client.calls, [("send_message", -100, "hello", 77)])

    async def test_unrouted_distribution_mode_message_uses_message_id_seed(self):
        client = FakeClient()
        bot = RelayBot(
            client,
            FakeConfigManager(),
            {
                "dest_channels": [-100],
                "master_account_id": 0,
                "distribute_unrouted_to_buckets": True,
                "unrouted_distribution_mode": "message",
                "general_topic_buckets": {
                    -100: ["T1", "T2", "T3", "T4", "T5"],
                },
            },
            rate_limiter=FakeRateLimiter(),
        )

        # message-based: msg.id=11 -> 11 % 5 == 1 -> T2
        msg = FakeMessage(
            message_id=11,
            media=None,
            raw_text="hello",
            fwd_from=FakeForwardHeader(types.PeerChannel(999)),
        )
        event = FakeEvent(chat_id=1, sender_id=2, raw_text="hello", message=msg)

        seen = {}

        async def fake_get_or_create_top_message_id(chat_id, title):
            seen["title"] = title
            return 88

        bot.topic_resolver.get_or_create_top_message_id = fake_get_or_create_top_message_id

        await bot.handle(event)
        self.assertEqual(seen.get("title"), "T2")
        self.assertEqual(client.calls, [("send_message", -100, "hello", 88)])

    async def test_blocklist_applies_even_when_strip_text_enabled(self):
        client = FakeClient()
        bot = RelayBot(
            client,
            FakeConfigManager(),
            {
                "dest_channels": [-100],
                "master_account_id": 0,
                "strip_text": True,
                "blocklist_substrings": ["hello"],
            },
            rate_limiter=FakeRateLimiter(),
        )

        msg = FakeMessage(message_id=10, media=None, raw_text="hello world")
        event = FakeEvent(chat_id=1, sender_id=2, raw_text=msg.raw_text, message=msg)
        await bot.handle(event)

        self.assertEqual(client.calls, [])

    async def test_require_forum_topic_skips_when_unresolved(self):
        client = FakeClient()
        bot = RelayBot(
            client,
            FakeConfigManager(),
            {
                "dest_channels": [-100],
                "master_account_id": 0,
                "require_forum_topic": True,
                "default_destinations": [{"chat_id": -100, "topic_title": "TopicA"}],
            },
            rate_limiter=FakeRateLimiter(),
        )

        # Avoid hitting Telegram API in tests.
        async def fake_get_or_create_top_message_id(chat_id, title):
            return None

        bot.topic_resolver.get_or_create_top_message_id = fake_get_or_create_top_message_id

        msg = FakeMessage(message_id=10, media=None, raw_text="hello")
        event = FakeEvent(chat_id=1, sender_id=2, raw_text=msg.raw_text, message=msg)
        await bot.handle(event)

        self.assertEqual(client.calls, [])

    async def test_unauthorized_sender_blocked_when_enabled(self):
        client = FakeClient()
        bot = RelayBot(
            client,
            FakeConfigManager(),
            {"dest_channels": [-100], "master_account_id": 999},
            rate_limiter=FakeRateLimiter(),
        )

        msg = FakeMessage(message_id=10, media=None, raw_text="hello")
        event = FakeEvent(chat_id=1, sender_id=2, raw_text="hello", message=msg)
        await bot.handle(event)

        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
