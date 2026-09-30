import unittest

from daily_price_collector import Query, _visible_result_card_texts
from ctrip_visible_results import VisibleCardParseError, parse_visible_flight_cards


QUERY = Query(
    "上海",
    "北京",
    "2026-10-07",
    "PVG",
    "浦东国际机场",
    "PEK",
    "首都国际机场",
)
CAPTURED_AT = "2026-09-30T14:32:00+08:00"
VISIBLE_DIRECT_CARDS = [
    "东方航空 MU5163 空客321(中) 19:30 浦东国际机场 T1 21:50 首都国际机场 T2 "
    "赠 ¥ 50接送机券 ¥ 700 起 经济舱 3.3折 订票",
    "中国国航 CA8358 空客330(大) 20:05 浦东国际机场 T2 22:25 首都国际机场 T3 "
    "接送机最高60元满减券 ¥ 700 起 经济舱 3.3折 订票",
]


class _VisibleElement:
    def __init__(self, accessible_name, text, displayed=True):
        self.accessible_name = accessible_name
        self.text = text
        self._displayed = displayed

    def is_displayed(self):
        return self._displayed


class _VisibleDriver:
    def __init__(self, elements):
        self.elements = elements

    def find_elements(self, by, selector):
        if selector != "*":
            raise AssertionError(f"unexpected selector: {selector}")
        return self.elements


class VisibleFlightCardParserTests(unittest.TestCase):
    def test_parses_live_ui_card_and_ignores_coupon_as_fare(self):
        observations, summary = parse_visible_flight_cards(
            [VISIBLE_DIRECT_CARDS[0]],
            QUERY,
            CAPTURED_AT,
            economy_filter_verified=True,
            visible_page_text="直飞/经停 中转组合",
        )

        self.assertEqual(len(observations), 1)
        card = observations[0]
        self.assertEqual(card["flight_no"], "MU5163")
        self.assertEqual(card["airline"], "东方航空")
        self.assertEqual(card["departure_airport_code"], "PVG")
        self.assertEqual(card["arrival_airport_code"], "PEK")
        self.assertEqual(card["departure_time"], "2026-10-07 19:30")
        self.assertEqual(card["arrival_time"], "2026-10-07 21:50")
        self.assertEqual(card["economy_fare_cny"], "700.00")
        self.assertEqual(card["direct"], 1)
        self.assertEqual(card["economy"], 1)
        self.assertIn("visible_card_text", card["flight_card_json"])
        self.assertIn("¥ 700 起", card["fare_card_json"])
        self.assertEqual(summary["direct_economy_min_fare_cny"], "700.00")
        self.assertTrue(summary["summary"]["connecting_section_visible"])
        self.assertFalse(summary["summary"]["parsed_card_set_complete"])

    def test_parses_two_live_direct_cards_and_excludes_connecting_card(self):
        connecting = (
            "组合行程 MU5101 08:00 浦东国际机场 中转至 MU5202 13:30 "
            "首都国际机场 ¥ 650 起 经济舱"
        )
        observations, summary = parse_visible_flight_cards(
            VISIBLE_DIRECT_CARDS + [connecting],
            QUERY,
            CAPTURED_AT,
            economy_filter_verified=True,
            visible_page_text="中转组合",
        )
        self.assertEqual([row["flight_no"] for row in observations], ["MU5163", "CA8358"])
        self.assertEqual([row["economy_fare_cny"] for row in observations], ["700.00", "700.00"])
        self.assertEqual(summary["itinerary_count"], 3)
        self.assertEqual(summary["direct_itinerary_count"], 2)
        self.assertEqual(summary["connecting_itinerary_count"], 1)
        self.assertEqual(summary["direct_economy_min_fare_cny"], "700.00")
        self.assertTrue(summary["summary"]["has_connecting_itineraries"])

    def test_requires_verified_economy_filter(self):
        with self.assertRaisesRegex(VisibleCardParseError, "economy-class filter"):
            parse_visible_flight_cards(
                VISIBLE_DIRECT_CARDS,
                QUERY,
                CAPTURED_AT,
                economy_filter_verified=False,
            )

    def test_requires_visible_selected_airports_on_direct_card(self):
        with self.assertRaisesRegex(VisibleCardParseError, "both selected airport names"):
            parse_visible_flight_cards(
                ["MU5163 19:30 21:50 ¥ 700 起 经济舱"],
                QUERY,
                CAPTURED_AT,
                economy_filter_verified=True,
            )

    def test_requires_two_visible_times_and_one_flight_number(self):
        with self.assertRaisesRegex(VisibleCardParseError, "two departure/arrival times"):
            parse_visible_flight_cards(
                ["MU5163 浦东国际机场 首都国际机场 19:30 ¥ 700 起 经济舱"],
                QUERY,
                CAPTURED_AT,
                economy_filter_verified=True,
            )

    def test_rejects_missing_or_ambiguous_direct_fares(self):
        with self.assertRaisesRegex(VisibleCardParseError, "no explicit CNY price"):
            parse_visible_flight_cards(
                ["MU5163 19:30 浦东国际机场 21:50 首都国际机场 经济舱"],
                QUERY,
                CAPTURED_AT,
                economy_filter_verified=True,
            )
        ambiguous = (
            "MU5163 19:30 浦东国际机场 21:50 首都国际机场 "
            "¥ 700 起 ¥ 750 起 经济舱"
        )
        with self.assertRaisesRegex(VisibleCardParseError, "multiple starting-price labels"):
            parse_visible_flight_cards(
                [ambiguous], QUERY, CAPTURED_AT, economy_filter_verified=True
            )

    def test_ignores_nonflight_ui_text_and_returns_empty_summary(self):
        observations, summary = parse_visible_flight_cards(
            ["排序 低价优先", "暂无航班"],
            QUERY,
            CAPTURED_AT,
            economy_filter_verified=True,
        )
        self.assertEqual(observations, [])
        self.assertEqual(summary["itinerary_count"], 0)
        self.assertIsNone(summary["direct_economy_min_fare_cny"])

    def test_arrival_time_crossing_midnight_uses_next_day(self):
        card = (
            "CA1501 23:20 浦东国际机场 01:15 首都国际机场 "
            "直飞 ¥ 900 起 经济舱"
        )
        observations, _ = parse_visible_flight_cards(
            [card], QUERY, CAPTURED_AT, economy_filter_verified=True
        )
        self.assertEqual(observations[0]["arrival_time"], "2026-10-08 01:15")

    def test_visible_dom_extractor_deduplicates_nested_cards_and_ignores_hidden_nodes(self):
        short = "中国国航 CA8358 20:05 浦东国际机场 22:25 首都国际机场 ¥ 700 起 经济舱"
        full = VISIBLE_DIRECT_CARDS[1]
        hidden = _VisibleElement("南方航空", "CZ9999 10:00 浦东国际机场 12:00 首都国际机场 ¥ 300 起 经济舱", False)
        driver = _VisibleDriver(
            [
                _VisibleElement("东方航空", VISIBLE_DIRECT_CARDS[0]),
                _VisibleElement("中国国航", short),
                _VisibleElement("中国国航", full),
                hidden,
                _VisibleElement("广告", "上海酒店 ¥ 300 起"),
            ]
        )
        cards = _visible_result_card_texts(driver)
        self.assertEqual(cards, [VISIBLE_DIRECT_CARDS[0], full])


if __name__ == "__main__":
    unittest.main()
