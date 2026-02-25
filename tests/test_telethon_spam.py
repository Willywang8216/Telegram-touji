import unittest

from telethon import types

from telethon_spam import extract_urls_and_mentions, looks_like_promo_directory


class TestTelethonSpam(unittest.TestCase):
    def test_extract_text_url_entity(self):
        text = "Foo"
        ents = [types.MessageEntityTextUrl(offset=0, length=3, url="https://t.me/foo")]
        urls, mentions = extract_urls_and_mentions(text, ents)
        self.assertEqual(urls, ["https://t.me/foo"])
        self.assertEqual(mentions, [])

    def test_promo_directory_many_links_is_blocked(self):
        text = "Best channels\n" + "\n".join([f"Item {i}" for i in range(10)])
        urls = [f"https://t.me/ch{i}" for i in range(6)]
        self.assertTrue(looks_like_promo_directory(text, urls=urls, mentions=[]))

    def test_single_link_not_blocked(self):
        text = "nice"
        urls = ["https://t.me/somechannel"]
        self.assertFalse(looks_like_promo_directory(text, urls=urls, mentions=[]))

    def test_price_and_mention_blocked(self):
        text = "新年特惠活动\n咨询&下单 @xiaoxuBot"
        self.assertTrue(looks_like_promo_directory(text, urls=[], mentions=["@xiaoxuBot"]))


if __name__ == "__main__":
    unittest.main()
