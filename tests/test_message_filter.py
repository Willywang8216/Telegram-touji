import unittest

from message_filter import contains_blocked_substring, looks_like_contact_ad, should_block_text


class TestMessageFilter(unittest.TestCase):
    def test_contains_blocked_substring_case_and_whitespace_insensitive(self):
        text = "新年特惠活动\n\n✨ 新春超值福利来袭"
        self.assertTrue(contains_blocked_substring(text, ["新年特惠活动"]))

        text2 = "Ban: 各类rush有货"
        self.assertTrue(contains_blocked_substring(text2, ["Ban:  各类rush有货"]))

    def test_contact_ad_heuristic(self):
        text = "👉 加入会员私信联系\n@xiaoxuxuezhang\n@xiaoxuBot"
        self.assertTrue(looks_like_contact_ad(text))

        self.assertFalse(looks_like_contact_ad("@hello"))

    def test_should_block_text(self):
        text = "活动优惠：全场享买一送一\n咨询&下单：\n@xiaoxuBot"
        self.assertTrue(should_block_text(text, blocklist_substrings=[]))

        self.assertTrue(should_block_text("some text", blocklist_regexes=[r"some\\s+text"]))


if __name__ == "__main__":
    unittest.main()
