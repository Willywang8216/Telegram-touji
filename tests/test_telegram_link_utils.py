import unittest

from telegram_link_utils import looks_like_message_link, parse_message_link


class TestTelegramLinkUtils(unittest.TestCase):
    def test_looks_like(self):
        self.assertTrue(looks_like_message_link("https://t.me/c/123/456"))
        self.assertFalse(looks_like_message_link("not a link"))

    def test_parse_c_link(self):
        p = parse_message_link("https://t.me/c/123456/789")
        self.assertIsNotNone(p)
        assert p is not None
        self.assertEqual(p.chat, -100123456)
        self.assertEqual(p.message_id, 789)
        self.assertIsNone(p.topic_id)

    def test_parse_c_topic_link(self):
        p = parse_message_link("https://t.me/c/123456/777/888")
        self.assertIsNotNone(p)
        assert p is not None
        self.assertEqual(p.chat, -100123456)
        self.assertEqual(p.message_id, 888)
        self.assertEqual(p.topic_id, 777)

    def test_parse_username_link(self):
        p = parse_message_link("https://t.me/somegroup/999")
        self.assertIsNotNone(p)
        assert p is not None
        self.assertEqual(p.chat, "somegroup")
        self.assertEqual(p.message_id, 999)
        self.assertIsNone(p.topic_id)

    def test_parse_trailing_punctuation(self):
        p = parse_message_link("https://t.me/somegroup/999)")
        self.assertIsNotNone(p)
        assert p is not None
        self.assertEqual(p.chat, "somegroup")
        self.assertEqual(p.message_id, 999)

    def test_parse_username_topic_link(self):
        p = parse_message_link("t.me/somegroup/777/888")
        self.assertIsNotNone(p)
        assert p is not None
        self.assertEqual(p.chat, "somegroup")
        self.assertEqual(p.message_id, 888)
        self.assertEqual(p.topic_id, 777)


if __name__ == "__main__":
    unittest.main()
