import csv
import json
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from daily_price_collector import (
    CollectionError,
    Query,
    ROUTE_PAIRS,
    _selected_city_value,
    _candidate_city_options,
    _visible_city_suggestions,
    choose_city,
    build_date_window,
    build_scope,
    parse_response_payload,
    _validate_request_body,
)
from price_history import PriceHistoryStore, SCHEMA_VERSION
from ctrip_dom_selectors import CtripSelectorMismatch


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "ctrip_batch_search_sample.json"
CITY_SUGGESTIONS_FIXTURE = ROOT / "tests" / "fixtures" / "ctrip_city_suggestions_excerpt.html"
SAMPLE_QUERY = Query("上海", "北京", "2026-09-30")
CAPTURED_AT = "2026-09-30T08:00:00+08:00"


def sample_parsed():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return parse_response_payload(payload, SAMPLE_QUERY, CAPTURED_AT)


def make_flight_observation():
    return {
        "departure_airport": "上海虹桥国际机场",
        "departure_airport_code": "SHA",
        "arrival_airport": "北京首都国际机场",
        "arrival_airport_code": "PEK",
        "flight_no": "MU5101",
        "airline": "东方航空",
        "departure_time": "2026-09-30 08:30",
        "arrival_time": "2026-09-30 11:00",
        "economy_fare_cny": "1050.00",
        "currency": "CNY",
        "stops": 0,
        "direct": 1,
        "economy": 1,
        "status": "ok",
        "error": None,
        "flight_card_json": '{"itineraryId":"MU5101_0"}',
        "fare_card_json": '{"cabin":"Y","adultPrice":1000,"adultTax":50}',
    }


class DailyScopeTests(unittest.TestCase):
    def test_date_window_is_thirty_natural_days_inclusive(self):
        days = build_date_window(date(2026, 9, 30))
        self.assertEqual(len(days), 30)
        self.assertEqual(days[0], "2026-09-30")
        self.assertEqual(days[-1], "2026-10-29")

    def test_scope_is_exactly_ten_bidirectional_city_routes_by_thirty_dates(self):
        scope = build_scope(date(2026, 9, 30))
        expected_routes = {
            ("上海", "北京"), ("北京", "上海"),
            ("上海", "广州"), ("广州", "上海"),
            ("上海", "深圳"), ("深圳", "上海"),
            ("上海", "成都"), ("成都", "上海"),
            ("上海", "乌鲁木齐"), ("乌鲁木齐", "上海"),
        }
        self.assertEqual(set(ROUTE_PAIRS), expected_routes)
        self.assertEqual(len(scope), 300)
        self.assertEqual({(q.departure_city, q.arrival_city) for q in scope}, expected_routes)
        self.assertEqual({q.departure_date for q in scope}, set(build_date_window(date(2026, 9, 30))))

    def test_selected_city_requires_a_site_formatted_three_letter_code(self):
        self.assertTrue(_selected_city_value("上海(SHA)", "上海"))
        self.assertTrue(_selected_city_value("乌鲁木齐(乌鲁木齐天山国际机场)(URC)", "乌鲁木齐"))
        self.assertFalse(_selected_city_value("上海", "上海"))
        self.assertFalse(_selected_city_value("上海(SHA)", "北京"))

    def test_observed_airport_suggestions_are_not_accepted_as_city_level(self):
        fixture = CITY_SUGGESTIONS_FIXTURE.read_text(encoding="utf-8")
        for airport in ("浦东国际机场", "虹桥国际机场", "金山水上通用机场"):
            self.assertIn(airport, fixture)

        class Suggestion:
            def __init__(self, text, remark):
                self.text = text
                self.accessible_name = ""
                self.remark = remark

            def is_displayed(self):
                return True

            def is_enabled(self):
                return True

            def get_attribute(self, name):
                return self.remark if name == "data-u_remark" else None

        items = [
            Suggestion("上海浦东国际机场PVG", "Name:浦东国际机场,Code:PVG"),
            Suggestion("上海虹桥国际机场SHA", "Name:虹桥国际机场,Code:SHA"),
            Suggestion("上海金山水上通用机场JS2", "Name:金山水上通用机场,Code:JS2"),
        ]

        class SuggestionDriver:
            def find_elements(self, by, selector):
                return items

        driver = SuggestionDriver()
        self.assertEqual(_candidate_city_options(driver, "上海"), [])
        suggestions = _visible_city_suggestions(driver, "上海")
        self.assertEqual(len(suggestions), 3)
        self.assertTrue(any("Code:PVG" in item for item in suggestions))
        self.assertTrue(any("Code:SHA" in item for item in suggestions))
        self.assertTrue(any("Code:JS2" in item for item in suggestions))

        formatted_airport = Suggestion("上海(SHA)", "Name:虹桥国际机场,Code:SHA")

        class FormattedAirportDriver:
            def find_elements(self, by, selector):
                return [formatted_airport]

        self.assertEqual(_candidate_city_options(FormattedAirportDriver(), "上海"), [])

        # Even a prefilled Shanghai(SHA) value is revalidated from the current
        # autocomplete; a matching airport POI is never clicked as a city.
        class PrefilledInput:
            def __init__(self):
                self.value = "上海(SHA)"
                self.clicked = False
                self.sent = []

            def get_attribute(self, name):
                return self.value if name == "value" else None

            def click(self):
                self.clicked = True

            def send_keys(self, value):
                self.sent.append(value)
                if value == "上海":
                    self.value = "上海"

        prefilled = PrefilledInput()
        with self.assertRaisesRegex(CtripSelectorMismatch, "airport-specific"):
            choose_city(driver, prefilled, "上海", timeout=0.1)
        self.assertTrue(prefilled.clicked)
        self.assertEqual(prefilled.sent[-1], "上海")
        self.assertEqual(len(prefilled.sent), 2)


class ResponseParsingTests(unittest.TestCase):
    def test_keeps_only_direct_economy_cards_and_separates_transfer_summary(self):
        observations, summary = sample_parsed()
        self.assertEqual(len(observations), 2)
        self.assertEqual({item["flight_no"] for item in observations}, {"MU5101"})
        self.assertEqual({item["economy_fare_cny"] for item in observations}, {"1050.00", "1240.00"})
        for item in observations:
            self.assertEqual(item["currency"], "CNY")
            self.assertEqual(item["direct"], 1)
            self.assertEqual(item["economy"], 1)
            self.assertEqual(item["stops"], 0)
            self.assertIn('"cabin": "Y"', item["fare_card_json"])
        self.assertEqual(summary["itinerary_count"], 3)
        self.assertEqual(summary["direct_itinerary_count"], 2)
        self.assertEqual(summary["connecting_itinerary_count"], 1)
        self.assertEqual(summary["all_economy_min_fare_cny"], "750.00")
        self.assertEqual(summary["direct_economy_min_fare_cny"], "1050.00")
        self.assertEqual(summary["transfer_summary"]["economy_fare_card_count_by_stops"], {"0": 2, "1": 1})
        serialized_summary = json.dumps(summary, ensure_ascii=False)
        self.assertNotIn("flightList", serialized_summary)
        self.assertNotIn("fareBasis", serialized_summary)

    def test_route_or_date_mismatch_is_rejected(self):
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        with self.assertRaisesRegex(CollectionError, "did not match request"):
            parse_response_payload(payload, Query("上海", "广州", "2026-09-30"), CAPTURED_AT)

    def test_captured_ui_search_request_must_match_route_and_date(self):
        request_body = json.dumps({"flightSegments": [{
            "departureCityName": "上海",
            "arrivalCityName": "北京",
            "departureDate": "2026-09-30",
        }]}).encode("utf-8")
        _validate_request_body(request_body, SAMPLE_QUERY)
        with self.assertRaisesRegex(CollectionError, "did not match scope"):
            _validate_request_body(
                request_body, Query("上海", "深圳", "2026-09-30")
            )

    def test_login_required_response_is_blocked(self):
        payload = {
            "flightSegments": [{"departureCityName": "上海", "arrivalCityName": "北京", "departureDate": "2026-09-30"}],
            "data": {"needUserLogin": True, "flightItineraryList": []},
        }
        from daily_price_collector import AccessBlocked
        with self.assertRaises(AccessBlocked):
            parse_response_payload(payload, SAMPLE_QUERY, CAPTURED_AT)


class SQLiteHistoryTests(unittest.TestCase):
    def test_schema_indexes_and_append_only_observations(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "history.sqlite3"
            with PriceHistoryStore(database) as store:
                version = store.connection.execute("PRAGMA user_version").fetchone()[0]
                self.assertEqual(version, SCHEMA_VERSION)
                indexes = {
                    row[0] for row in store.connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='index'"
                    ).fetchall()
                }
                self.assertIn("idx_flights_route_date_captured", indexes)
                self.assertIn("idx_summaries_route_date_captured", indexes)
                scope = [SAMPLE_QUERY.as_scope_row()]
                store.create_run("run-one", scope, CAPTURED_AT)
                store.mark_query_running("run-one", "上海", "北京", "2026-09-30")
                observations, summary = sample_parsed()
                store.record_query_result(
                    run_id="run-one", departure_city="上海", arrival_city="北京",
                    departure_date="2026-09-30", status="success", captured_at=CAPTURED_AT,
                    source="fixture", observations=observations, summary=summary,
                )
                first = store.finish_run("run-one")
                self.assertEqual(first["status"], "completed")
                self.assertEqual(first["successful_queries"], 1)

                # A later run of the same route/date appends new observations rather than replacing old ones.
                store.create_run("run-two", scope, "2026-10-01T08:00:00+08:00")
                store.mark_query_running("run-two", "上海", "北京", "2026-09-30")
                store.record_query_result(
                    run_id="run-two", departure_city="上海", arrival_city="北京",
                    departure_date="2026-09-30", status="success",
                    captured_at="2026-10-01T08:00:00+08:00", source="fixture",
                    observations=observations, summary=summary,
                )
                store.finish_run("run-two")
                count = store.connection.execute(
                    "SELECT COUNT(*) FROM flight_observations WHERE flight_no='MU5101'"
                ).fetchone()[0]
                self.assertEqual(count, 4)
                saved = store.connection.execute(
                    "SELECT captured_at, currency, direct, economy, stops FROM flight_observations ORDER BY observation_id LIMIT 1"
                ).fetchone()
                self.assertTrue(saved["captured_at"].endswith("+08:00"))
                self.assertEqual((saved["currency"], saved["direct"], saved["economy"], saved["stops"]), ("CNY", 1, 1, 0))
                self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM daily_summaries").fetchone()[0], 2)

    def test_blocked_run_records_error_and_marks_remaining_queries_not_run(self):
        with tempfile.TemporaryDirectory() as temp:
            with PriceHistoryStore(Path(temp) / "history.sqlite3") as store:
                scope = [
                    {"departure_city": "上海", "arrival_city": "北京", "departure_date": "2026-09-30"},
                    {"departure_city": "上海", "arrival_city": "北京", "departure_date": "2026-10-01"},
                ]
                store.create_run("blocked-run", scope, CAPTURED_AT)
                store.mark_query_running("blocked-run", "上海", "北京", "2026-09-30")
                store.record_query_result(
                    run_id="blocked-run", departure_city="上海", arrival_city="北京",
                    departure_date="2026-09-30", status="blocked", captured_at=CAPTURED_AT,
                    source="fixture", error="AccessBlocked: captcha appeared",
                )
                result = store.finish_run("blocked-run", "captcha appeared")
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["expected_queries"], 2)
                self.assertEqual(result["attempted_queries"], 1)
                self.assertEqual(result["not_run_queries"], 1)
                rows = store.connection.execute(
                    "SELECT status, error FROM query_observations ORDER BY departure_date"
                ).fetchall()
                self.assertEqual(rows[0]["status"], "blocked")
                self.assertIn("captcha", rows[0]["error"])
                self.assertEqual(rows[1]["status"], "not_run")

    def test_csv_exports_flights_and_summaries_separately(self):
        with tempfile.TemporaryDirectory() as temp:
            with PriceHistoryStore(Path(temp) / "history.sqlite3") as store:
                store.create_run("csv-run", [SAMPLE_QUERY.as_scope_row()], CAPTURED_AT)
                store.mark_query_running("csv-run", "上海", "北京", "2026-09-30")
                observations, summary = sample_parsed()
                store.record_query_result(
                    run_id="csv-run", departure_city="上海", arrival_city="北京",
                    departure_date="2026-09-30", status="success", captured_at=CAPTURED_AT,
                    source="fixture", observations=observations, summary=summary,
                )
                store.finish_run("csv-run")
                flights = Path(temp) / "flights.csv"
                summaries = Path(temp) / "summaries.csv"
                self.assertEqual(store.export_csv(flights, "flights"), 2)
                self.assertEqual(store.export_csv(summaries, "summaries"), 1)
                with flights.open(encoding="utf-8-sig", newline="") as handle:
                    flight_rows = list(csv.DictReader(handle))
                with summaries.open(encoding="utf-8-sig", newline="") as handle:
                    summary_rows = list(csv.DictReader(handle))
                self.assertEqual(len(flight_rows), 2)
                self.assertEqual(flight_rows[0]["flight_no"], "MU5101")
                self.assertIn("fare_card_json", flight_rows[0])
                self.assertEqual(len(summary_rows), 1)
                self.assertNotIn("fare_card_json", summary_rows[0])


if __name__ == "__main__":
    unittest.main()
