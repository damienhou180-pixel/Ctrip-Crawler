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
    _selected_city_value,
    _candidate_city_options,
    _candidate_airport_options,
    _airport_identity,
    _selected_airport_value,
    _visible_city_suggestions,
    choose_city,
    choose_airport,
    build_date_window,
    build_scope,
    parse_response_payload,
    _validate_request_body,
    run_collection,
)
from airport_scope import (
    Airport,
    AirportCoverageUnverified,
    AirportInventory,
    CITY_HUBS,
    DESTINATION_AIRPORTS,
    SHANGHAI_AIRPORTS,
    build_airport_route_pairs,
    build_matrix_plan,
    coverage_report,
)
from price_history import PriceHistoryStore, SCHEMA_VERSION
from ctrip_dom_selectors import CtripSelectorMismatch


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "ctrip_batch_search_sample.json"
CITY_SUGGESTIONS_FIXTURE = ROOT / "tests" / "fixtures" / "ctrip_city_suggestions_excerpt.html"
DIRECTORY_FIXTURE = ROOT / "tests" / "fixtures" / "ctrip_airport_directory_snapshot.json"
SAMPLE_QUERY = Query("上海", "北京", "2026-09-30", "SHA", "虹桥国际机场", "PEK", "首都国际机场")
CAPTURED_AT = "2026-09-30T08:00:00+08:00"


def synthetic_complete_inventory():
    """Small synthetic inventory used only to test the matrix arithmetic."""
    counts = {"北京": 1, "广州": 2, "深圳": 1, "成都": 3, "乌鲁木齐": 1}
    prefixes = {"北京": "B", "广州": "G", "深圳": "S", "成都": "C", "乌鲁木齐": "U"}
    return {
        city: AirportInventory(
            city,
            tuple(
                Airport(city, f"{city}测试机场{index}", f"{prefixes[city]}{index:02d}")
                for index in range(1, counts[city] + 1)
            ),
            True,
            "fixture://synthetic-airport-inventory",
            "Synthetic test-only airport entries; not real Ctrip inventory.",
        )
        for city in CITY_HUBS
    }


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

    def test_airport_matrix_uses_exact_thirty_times_four_times_sum_formula(self):
        inventory = synthetic_complete_inventory()
        plan = build_matrix_plan(inventory)
        self.assertEqual(plan["airport_counts_by_city"], {"北京": 1, "广州": 2, "深圳": 1, "成都": 3, "乌鲁木齐": 1})
        self.assertEqual(plan["S"], 8)
        self.assertEqual(plan["directed_routes_per_departure_date"], 4 * 8)
        self.assertEqual(plan["expected_queries"], 30 * 4 * 8)
        routes = build_airport_route_pairs(inventory)
        self.assertEqual(len(routes), 32)
        scope = build_scope(date(2026, 9, 30), inventory)
        self.assertEqual(len(scope), plan["expected_queries"])
        self.assertEqual(len(scope), 960)
        self.assertTrue(all(q.departure_airport_code and q.arrival_airport_code for q in scope))
        self.assertEqual({q.departure_date for q in scope}, set(build_date_window(date(2026, 9, 30))))

    def test_official_directory_airports_and_candidate_matrix_are_exact_but_not_executable(self):
        snapshot = json.loads(DIRECTORY_FIXTURE.read_text(encoding="utf-8"))
        expected_codes = {
            city: [entry["code"] for entry in entries]
            for city, entries in snapshot["airport_entries_by_city"].items()
            if city in CITY_HUBS
        }
        configured_codes = {
            city: [airport.code for airport in DESTINATION_AIRPORTS[city].airports]
            for city in CITY_HUBS
        }
        self.assertEqual(configured_codes, expected_codes)

        excluded_codes = {
            entry["code"] for entry in snapshot["excluded_non_airport_directory_entries"]
        }
        self.assertTrue(excluded_codes.isdisjoint({code for codes in configured_codes.values() for code in codes}))

        report = coverage_report()
        directory_plan = report["directory_matrix"]
        self.assertEqual(
            directory_plan["directory_entry_counts_by_city"],
            {"北京": 2, "广州": 1, "深圳": 1, "成都": 2, "乌鲁木齐": 1},
        )
        self.assertEqual(directory_plan["S_directory"], 7)
        self.assertEqual(directory_plan["distinct_airport_pairs_per_period"], 14)
        self.assertEqual(directory_plan["directed_routes_per_departure_date"], 28)
        self.assertEqual(directory_plan["directory_based_candidate_queries"], 840)
        self.assertIsNone(report["S"])
        self.assertIsNone(report["expected_queries"])
        self.assertEqual(set(report["unverified_cities"]), set(CITY_HUBS))
        for city in CITY_HUBS:
            self.assertFalse(report["airport_counts_by_city"][city]["selector_complete"])
            self.assertIsNone(report["airport_counts_by_city"][city]["n_i"])

    def test_current_cities_without_complete_evidence_are_reported_and_fail_closed(self):
        report = coverage_report()
        counts = report["airport_counts_by_city"]
        self.assertEqual(counts["成都"]["directory_entry_count"], 2)
        for city in CITY_HUBS:
            self.assertEqual(counts[city]["selector_complete"], False)
            self.assertIsNone(counts[city]["n_i"])
        self.assertEqual(report["S"], None)
        self.assertEqual(report["expected_queries"], None)
        with self.assertRaises(AirportCoverageUnverified):
            build_matrix_plan()
        with self.assertRaises(AirportCoverageUnverified):
            build_scope(date(2026, 9, 30))

    def test_unverified_scope_is_rejected_before_database_or_browser_access(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "must-not-be-created.sqlite3"
            called = []

            def driver_factory():
                called.append(True)
                raise AssertionError("browser must not start for incomplete airport evidence")

            with self.assertRaises(AirportCoverageUnverified):
                run_collection(database, driver_factory=driver_factory)
            self.assertFalse(database.exists())
            self.assertEqual(called, [])

    def test_selected_city_requires_a_site_formatted_three_letter_code(self):
        self.assertTrue(_selected_city_value("上海(SHA)", "上海"))
        self.assertTrue(_selected_city_value("乌鲁木齐(乌鲁木齐天山国际机场)(URC)", "乌鲁木齐"))
        self.assertFalse(_selected_city_value("上海", "上海"))
        self.assertFalse(_selected_city_value("上海(SHA)", "北京"))

    def test_airport_selection_requires_exact_visible_name_code_and_verified_value(self):
        airport = Airport("上海", "虹桥国际机场", "SHA")
        self.assertEqual(
            _airport_identity("点击POI选项[city:,Province:上海,Name:虹桥国际机场,Code:SHA]"),
            ("虹桥国际机场", "SHA"),
        )
        self.assertIsNone(_airport_identity("上海(SHA)"))
        self.assertTrue(_selected_airport_value("上海(虹桥国际机场)(SHA)", airport))
        self.assertFalse(_selected_airport_value("上海(虹桥国际机场)(PVG)", airport))

        class Option:
            text = "上海虹桥国际机场SHA"
            accessible_name = ""

            def __init__(self, input_element):
                self.input_element = input_element

            def is_displayed(self):
                return True

            def is_enabled(self):
                return True

            def get_attribute(self, name):
                return "Name:虹桥国际机场,Code:SHA" if name == "data-u_remark" else None

            def click(self):
                self.input_element.value = "上海(虹桥国际机场)(SHA)"

        class Input:
            value = ""

            def click(self):
                pass

            def send_keys(self, value):
                if value == "上海":
                    self.value = value

            def get_attribute(self, name):
                return self.value if name == "value" else None

        input_element = Input()
        option = Option(input_element)

        class Driver:
            def find_elements(self, by, selector):
                return [option]

        driver = Driver()
        self.assertEqual(_candidate_airport_options(driver, airport), [option])
        self.assertEqual(choose_airport(driver, input_element, airport, timeout=0.1), "上海(虹桥国际机场)(SHA)")

        class DuplicateDriver:
            def find_elements(self, by, selector):
                return [option, Option(input_element)]

        with self.assertRaisesRegex(CtripSelectorMismatch, "2 exact visible options"):
            choose_airport(DuplicateDriver(), input_element, airport, timeout=0.1)

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
            "departureAirportCode": "SHA",
            "arrivalAirportCode": "PEK",
        }]}).encode("utf-8")
        _validate_request_body(request_body, SAMPLE_QUERY)
        wrong_airport_body = json.dumps({"flightSegments": [{
            "departureCityName": "上海",
            "arrivalCityName": "北京",
            "departureDate": "2026-09-30",
            "departureAirportCode": "SHA",
            "arrivalAirportCode": "CTU",
        }]}).encode("utf-8")
        with self.assertRaisesRegex(CollectionError, "exact airport codes"):
            _validate_request_body(wrong_airport_body, SAMPLE_QUERY)
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
                self.assertIn("idx_queries_airport_route_date", indexes)
                query_columns = {
                    row[1] for row in store.connection.execute("PRAGMA table_info(query_observations)")
                }
                self.assertTrue({"departure_airport_code", "departure_airport_name", "arrival_airport_code", "arrival_airport_name"}.issubset(query_columns))
                scope = [SAMPLE_QUERY.as_scope_row()]
                store.create_run("run-one", scope, CAPTURED_AT)
                store.mark_query_running("run-one", "上海", "北京", "2026-09-30", "SHA", "PEK")
                observations, summary = sample_parsed()
                store.record_query_result(
                    run_id="run-one", departure_city="上海", arrival_city="北京",
                    departure_date="2026-09-30", status="success", captured_at=CAPTURED_AT,
                    source="fixture", departure_airport_code="SHA", arrival_airport_code="PEK",
                    observations=observations, summary=summary,
                )
                first = store.finish_run("run-one")
                self.assertEqual(first["status"], "completed")
                self.assertEqual(first["successful_queries"], 1)

                # A later run of the same route/date appends new observations rather than replacing old ones.
                store.create_run("run-two", scope, "2026-10-01T08:00:00+08:00")
                store.mark_query_running("run-two", "上海", "北京", "2026-09-30", "SHA", "PEK")
                store.record_query_result(
                    run_id="run-two", departure_city="上海", arrival_city="北京",
                    departure_date="2026-09-30", status="success",
                    captured_at="2026-10-01T08:00:00+08:00", source="fixture",
                    departure_airport_code="SHA", arrival_airport_code="PEK",
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
                store.mark_query_running("csv-run", "上海", "北京", "2026-09-30", "SHA", "PEK")
                observations, summary = sample_parsed()
                store.record_query_result(
                    run_id="csv-run", departure_city="上海", arrival_city="北京",
                    departure_date="2026-09-30", status="success", captured_at=CAPTURED_AT,
                    source="fixture", departure_airport_code="SHA", arrival_airport_code="PEK",
                    observations=observations, summary=summary,
                )
                store.finish_run("csv-run")
                flights = Path(temp) / "flights.csv"
                summaries = Path(temp) / "summaries.csv"
                queries = Path(temp) / "queries.csv"
                self.assertEqual(store.export_csv(flights, "flights"), 2)
                self.assertEqual(store.export_csv(summaries, "summaries"), 1)
                self.assertEqual(store.export_csv(queries, "queries"), 1)
                with flights.open(encoding="utf-8-sig", newline="") as handle:
                    flight_rows = list(csv.DictReader(handle))
                with summaries.open(encoding="utf-8-sig", newline="") as handle:
                    summary_rows = list(csv.DictReader(handle))
                with queries.open(encoding="utf-8-sig", newline="") as handle:
                    query_rows = list(csv.DictReader(handle))
                self.assertEqual(len(flight_rows), 2)
                self.assertEqual(flight_rows[0]["flight_no"], "MU5101")
                self.assertIn("fare_card_json", flight_rows[0])
                self.assertEqual(len(summary_rows), 1)
                self.assertNotIn("fare_card_json", summary_rows[0])
                self.assertEqual(len(query_rows), 1)
                self.assertEqual((query_rows[0]["departure_city_name"], query_rows[0]["departure_airport_code"], query_rows[0]["arrival_city_name"], query_rows[0]["arrival_airport_code"]), ("上海", "SHA", "北京", "PEK"))


    def test_same_city_pair_and_date_can_track_distinct_airport_routes(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "airport-coverage.sqlite3"
            queries = [
                Query("上海", "成都", "2026-09-30", "PVG", "浦东国际机场", "TFU", "天府国际机场"),
                Query("上海", "成都", "2026-09-30", "PVG", "浦东国际机场", "CTU", "双流国际机场"),
            ]
            with PriceHistoryStore(database) as store:
                store.create_run("airport-pairs", [query.as_scope_row() for query in queries], CAPTURED_AT)
                rows = store.connection.execute(
                    """SELECT departure_city, departure_city_name, departure_airport_code,
                              arrival_city, arrival_city_name, arrival_airport_code, status
                       FROM query_observations ORDER BY arrival_airport_code"""
                ).fetchall()
                self.assertEqual(len(rows), 2)
                self.assertEqual({row["arrival_airport_code"] for row in rows}, {"CTU", "TFU"})
                self.assertEqual({row["departure_city_name"] for row in rows}, {"上海"})
                result = store.finish_run("airport-pairs")
                self.assertEqual(result["expected_queries"], 2)
                self.assertEqual(result["not_run_queries"], 2)
                output = Path(temp) / "airport-coverage.csv"
                self.assertEqual(store.export_csv(output, "queries"), 2)
                with output.open(encoding="utf-8-sig", newline="") as handle:
                    exported = list(csv.DictReader(handle))
                self.assertEqual({row["arrival_airport_code"] for row in exported}, {"CTU", "TFU"})

    def test_schema_v1_migration_preserves_existing_query_coverage(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "legacy-v1.sqlite3"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE runs (run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL,
                    finished_at TEXT, status TEXT NOT NULL, scope_json TEXT NOT NULL,
                    expected_queries INTEGER NOT NULL, attempted_queries INTEGER NOT NULL DEFAULT 0,
                    successful_queries INTEGER NOT NULL DEFAULT 0, failed_queries INTEGER NOT NULL DEFAULT 0,
                    error TEXT);
                CREATE TABLE query_observations (
                    query_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    departure_city TEXT NOT NULL, arrival_city TEXT NOT NULL,
                    departure_date TEXT NOT NULL, status TEXT NOT NULL, captured_at TEXT,
                    source TEXT, error TEXT, itinerary_count INTEGER,
                    direct_itinerary_count INTEGER, fare_card_count INTEGER,
                    UNIQUE (run_id, departure_city, arrival_city, departure_date));
                INSERT INTO runs(run_id,started_at,status,scope_json,expected_queries)
                    VALUES ('old-run','2026-09-30T08:00:00+08:00','failed','[]',1);
                INSERT INTO query_observations(run_id,departure_city,arrival_city,departure_date,status,error)
                    VALUES ('old-run','上海','北京','2026-09-30','not_run','preserved legacy coverage');
                PRAGMA user_version=1;
                """
            )
            connection.close()

            with PriceHistoryStore(database) as store:
                self.assertEqual(store.connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
                row = store.connection.execute(
                    "SELECT departure_city, arrival_city, status, error FROM query_observations"
                ).fetchone()
                self.assertEqual(tuple(row), ("上海", "北京", "not_run", "preserved legacy coverage"))
                columns = {column[1] for column in store.connection.execute("PRAGMA table_info(query_observations)")}
                self.assertTrue({"departure_airport_code", "departure_airport_name", "arrival_airport_code", "arrival_airport_name"}.issubset(columns))


if __name__ == "__main__":
    unittest.main()
