import unittest

from route_filter_utils import filter_routes, parse_route_filters


class TestRouteFilterUtils(unittest.TestCase):
    def test_parse_route_filters(self):
        f = parse_route_filters('source=-1,-2 dest=-100 topic="Hot" foo')
        self.assertEqual(f["source"], [-1, -2])
        self.assertEqual(f["dest"], -100)
        self.assertEqual(f["topic"], "Hot")
        self.assertEqual(f["terms"], ["foo"])

    def test_filter_routes(self):
        routes = [
            {
                "source_chats": [-1],
                "destinations": [{"chat_id": -100, "topic_title": "🔥 Hot Right Now"}],
            },
            {
                "source_chats": [-2],
                "destinations": [{"chat_id": -200, "topic_title": "Other"}],
            },
        ]

        f = parse_route_filters("source=-1")
        self.assertEqual(len(filter_routes(routes, filters=f)), 1)

        f = parse_route_filters("source=-1,-2")
        self.assertEqual(len(filter_routes(routes, filters=f)), 2)

        f = parse_route_filters("dest=-200")
        self.assertEqual(filter_routes(routes, filters=f)[0]["source_chats"], [-2])

        f = parse_route_filters('topic="hot"')
        self.assertEqual(filter_routes(routes, filters=f)[0]["source_chats"], [-1])

        f = parse_route_filters("foo")
        self.assertEqual(len(filter_routes(routes, filters=f)), 0)


if __name__ == "__main__":
    unittest.main()
