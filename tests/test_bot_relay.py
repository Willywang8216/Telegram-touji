import unittest

from bot_relay import RelayBot


class FakeConfigManager:
    def reload_if_changed(self):
        return False


class FakeRateLimiter:
    async def wait(self):
        return


class FakeClient:
    def __init__(self):
        self.calls = []

    async def send_message(self, channel_id, *, message=None):
        self.calls.append(("send_message", channel_id, message))

    async def send_file(self, channel_id, file, *, caption=None):
        self.calls.append(("send_file", channel_id, file, caption))


class FakeSender:
    def __init__(self, is_self=False):
        self.is_self = is_self


class FakeMessage:
    def __init__(self, *, message_id=1, grouped_id=None, media=None, raw_text=""):
        self.id = message_id
        self.grouped_id = grouped_id
        self.media = media
        self.raw_text = raw_text


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
            [("send_message", -100, "hello"), ("send_message", -200, "hello")],
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

        self.assertEqual(client.calls, [("send_file", -100, "MEDIA", "cap")])

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
