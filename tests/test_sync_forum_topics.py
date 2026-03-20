import logging
import unittest
from types import SimpleNamespace

from scripts import sync_forum_topics as sft


class FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def __call__(self, request):
        self.calls.append(request)
        name = request.__class__.__name__
        if name == "GetForumTopicsRequest":
            key = (str(getattr(request, "q", "") or ""), int(getattr(request, "offset_topic", 0) or 0), int(getattr(request, "offset_id", 0) or 0))
            return self.responses.get(key, SimpleNamespace(topics=[]))
        return SimpleNamespace()


class TestSyncForumTopics(unittest.IsolatedAsyncioTestCase):
    async def test_list_topics_matching_title_pages_and_filters_exact(self):
        t1 = SimpleNamespace(id=1, title="Old", top_message=10)
        t_other = SimpleNamespace(id=2, title="OldX", top_message=20)
        t2 = SimpleNamespace(id=3, title="Old", top_message=30)

        responses = {
            ("Old", 0, 0): SimpleNamespace(topics=[t1, t_other]),
            ("Old", 2, 20): SimpleNamespace(topics=[t2]),
            ("Old", 3, 30): SimpleNamespace(topics=[]),
        }
        client = FakeClient(responses)

        topics = await sft._list_topics_matching_title(client, peer="peer", title="Old")
        self.assertEqual(sorted(int(x.id) for x in topics), [1, 3])

    async def test_rename_topic_renames_most_popular_and_archives_others(self):
        # Higher top_message wins.
        dup1 = SimpleNamespace(id=11, title="Old", top_message=10)
        dup2 = SimpleNamespace(id=22, title="Old", top_message=99)

        responses = {
            ("Old", 0, 0): SimpleNamespace(topics=[dup1, dup2]),
            ("Old", 22, 99): SimpleNamespace(topics=[]),
        }
        client = FakeClient(responses)
        logger = logging.getLogger("test_sync_forum_topics")

        await sft._rename_topic(
            client,
            peer="peer",
            chat_id=123,
            old_title="Old",
            new_title="New",
            dry_run=False,
            logger=logger,
        )

        edit_calls = [c for c in client.calls if c.__class__.__name__ == "EditForumTopicRequest"]
        self.assertEqual(len(edit_calls), 2)

        keep = next(c for c in edit_calls if int(c.topic_id) == 22)
        self.assertEqual(str(keep.title), "New")

        archived = next(c for c in edit_calls if int(c.topic_id) == 11)
        self.assertTrue(bool(getattr(archived, "hidden", False)))
        self.assertTrue(bool(getattr(archived, "closed", False)))
        self.assertIn("archived", str(getattr(archived, "title", "")))

    async def test_delete_topic_history_and_hide_applies_to_all_matching_titles(self):
        t1 = SimpleNamespace(id=1, title="Del", top_message=101)
        t2 = SimpleNamespace(id=2, title="Del", top_message=202)

        responses = {
            ("Del", 0, 0): SimpleNamespace(topics=[t1, t2]),
            ("Del", 2, 202): SimpleNamespace(topics=[]),
        }
        client = FakeClient(responses)
        logger = logging.getLogger("test_sync_forum_topics")

        await sft._delete_topic_history_and_hide(client, peer="peer", chat_id=123, title="Del", dry_run=False, logger=logger)

        delete_calls = [c for c in client.calls if c.__class__.__name__ == "DeleteTopicHistoryRequest"]
        self.assertEqual(sorted(int(c.top_msg_id) for c in delete_calls), [101, 202])

        edit_calls = [c for c in client.calls if c.__class__.__name__ == "EditForumTopicRequest"]
        self.assertEqual(sorted(int(c.topic_id) for c in edit_calls), [1, 2])
        for c in edit_calls:
            self.assertTrue(bool(getattr(c, "hidden", False)))
            self.assertTrue(bool(getattr(c, "closed", False)))


if __name__ == "__main__":
    unittest.main()
