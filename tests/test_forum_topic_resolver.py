import logging
import unittest
from types import SimpleNamespace

from telethon import functions

from bot_relay import ForumTopicResolver


class FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def get_input_entity(self, chat_id):
        return f"peer:{chat_id}"

    async def __call__(self, request):
        self.calls.append(request)
        name = request.__class__.__name__
        if name == "GetForumTopicsRequest":
            q = str(getattr(request, "q", "") or "")
            offset_topic = int(getattr(request, "offset_topic", 0) or 0)
            offset_id = int(getattr(request, "offset_id", 0) or 0)
            key = (q, offset_topic, offset_id)
            return self.responses.get(key, SimpleNamespace(topics=[]))
        return SimpleNamespace()


class TestForumTopicResolver(unittest.IsolatedAsyncioTestCase):
    async def test_find_topic_normalizes_variation_selectors(self):
        # Existing topic stored without VS16.
        topic = SimpleNamespace(id=11, title="✋ Spank Fetish", top_message=999, hidden=False, pinned=False)

        responses = {
            ("✋️ Spank Fetish", 0, 0): SimpleNamespace(topics=[topic]),
        }
        client = FakeClient(responses)
        resolver = ForumTopicResolver(client, logger=logging.getLogger("test"), settings_getter=lambda: {"allow_topic_creation": True})

        found = await resolver.find_topic(-100, "✋️ Spank Fetish")
        self.assertEqual(found, {"topic_id": 11, "top_message": 999})

    async def test_get_or_create_does_not_create_when_disabled(self):
        responses = {
            ("Missing", 0, 0): SimpleNamespace(topics=[]),
            ("", 0, 0): SimpleNamespace(topics=[]),
        }
        client = FakeClient(responses)
        resolver = ForumTopicResolver(client, logger=logging.getLogger("test"), settings_getter=lambda: {"allow_topic_creation": False})

        top = await resolver.get_or_create_top_message_id(-100, "Missing")
        self.assertIsNone(top)

        create_calls = [c for c in client.calls if c.__class__ == functions.messages.CreateForumTopicRequest]
        self.assertEqual(create_calls, [])


if __name__ == "__main__":
    unittest.main()
