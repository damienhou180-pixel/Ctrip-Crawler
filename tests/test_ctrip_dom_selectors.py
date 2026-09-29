from pathlib import Path
import unittest

from selenium.webdriver.common.by import By

from ctrip_dom_selectors import (
    CURRENT_ARRIVAL_FIELD_SELECTOR,
    CURRENT_CITY_WRAPPERS,
    CURRENT_DEPARTURE_DATE_ATTRIBUTE,
    CURRENT_DEPARTURE_DATE_SELECTOR,
    CURRENT_DEPARTURE_FIELD_SELECTOR,
    LEGACY_CITY_INPUT_SELECTOR,
    LEGACY_CITY_INPUTS,
    CtripSelectorMismatch,
    current_departure_date,
    departure_date_from_remark,
    detect_city_form_mode,
    wait_for_city_inputs,
)


class FakeElement:
    def __init__(self, attributes=None):
        self.attributes = attributes or {}

    def get_attribute(self, name):
        return self.attributes.get(name)


class FakeDriver:
    def __init__(self, elements_by_selector):
        self.elements_by_selector = elements_by_selector

    def find_elements(self, by, selector):
        if by != By.CSS_SELECTOR:
            raise AssertionError(f"unexpected locator strategy: {by}")
        return list(self.elements_by_selector.get(selector, []))


class CtripDomSelectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = (
            Path(__file__).parent / "fixtures" / "ctrip_current_form_excerpt.html"
        ).read_text(encoding="utf-8")

    def test_captured_dom_has_current_wrappers_not_legacy_inputs_or_old_date_aria(self):
        self.assertIn('class="form-item-v3 flt-depart none-value"', self.fixture)
        self.assertIn('class="form-item-v3 flt-arrival"', self.fixture)
        self.assertIn('class="modifyDate depart-date"', self.fixture)
        self.assertIn('u_remark="日期选择框[flightWay:RT,date:2026-09-30,mode:departTime]"', self.fixture)
        self.assertNotIn(LEGACY_CITY_INPUT_SELECTOR, self.fixture)
        self.assertNotIn('aria-label="请选择日期"', self.fixture)

    def test_detects_current_wrappers_and_fails_instead_of_waiting_for_stale_input(self):
        driver = FakeDriver(
            {
                LEGACY_CITY_INPUT_SELECTOR: [],
                CURRENT_DEPARTURE_FIELD_SELECTOR: [FakeElement()],
                CURRENT_ARRIVAL_FIELD_SELECTOR: [FakeElement()],
            }
        )
        self.assertEqual(detect_city_form_mode(driver), CURRENT_CITY_WRAPPERS)
        with self.assertRaisesRegex(CtripSelectorMismatch, CURRENT_DEPARTURE_FIELD_SELECTOR):
            wait_for_city_inputs(driver, timeout=0.1)

    def test_retains_legacy_result_editor_when_two_inputs_really_exist(self):
        expected = [FakeElement(), FakeElement(), FakeElement()]
        driver = FakeDriver({LEGACY_CITY_INPUT_SELECTOR: expected})
        self.assertEqual(detect_city_form_mode(driver), LEGACY_CITY_INPUTS)
        self.assertEqual(wait_for_city_inputs(driver, timeout=0.1), expected[:2])

    def test_reads_departure_date_from_current_u_remark_attribute(self):
        remark = "日期选择框[flightWay:RT,date:2026-09-30,mode:departTime]"
        self.assertEqual(departure_date_from_remark(remark), "2026-09-30")
        driver = FakeDriver(
            {
                CURRENT_DEPARTURE_DATE_SELECTOR: [
                    FakeElement({CURRENT_DEPARTURE_DATE_ATTRIBUTE: remark})
                ]
            }
        )
        self.assertEqual(current_departure_date(driver), "2026-09-30")

    def test_invalid_or_missing_date_metadata_fails_clearly(self):
        with self.assertRaises(CtripSelectorMismatch):
            departure_date_from_remark("日期选择框[flightWay:RT,mode:departTime]")
        with self.assertRaises(CtripSelectorMismatch):
            current_departure_date(FakeDriver({CURRENT_DEPARTURE_DATE_SELECTOR: []}))

    def test_both_scrapers_use_shared_dom_diagnostics(self):
        root = Path(__file__).resolve().parents[1]
        for relpath in (
            "ctrip_flights_scraper_V3.py",
            "Linux_version/ctrip_flights_scraper_V3.5.py",
        ):
            source = (root / relpath).read_text(encoding="utf-8")
            self.assertIn("wait_for_city_inputs", source, relpath)
            self.assertIn("current_departure_date", source, relpath)
            self.assertIn("CtripSelectorMismatch", source, relpath)


if __name__ == "__main__":
    unittest.main()
