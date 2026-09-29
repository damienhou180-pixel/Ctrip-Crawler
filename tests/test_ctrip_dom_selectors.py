from pathlib import Path
import unittest

from selenium.webdriver.common.by import By

from ctrip_dom_selectors import (
    CURRENT_ARRIVAL_FIELD_SELECTOR,
    CURRENT_CITY_ACCESSIBLE_NAME,
    CURRENT_CITY_WRAPPERS,
    CURRENT_DATE_ACCESSIBLE_NAME,
    CURRENT_DEPARTURE_DATE_ATTRIBUTE,
    CURRENT_DEPARTURE_DATE_SELECTOR,
    CURRENT_DEPARTURE_FIELD_SELECTOR,
    LEGACY_CITY_INPUT_SELECTOR,
    LEGACY_CITY_INPUTS,
    CtripSelectorMismatch,
    current_departure_date,
    current_departure_date_input,
    departure_date_from_remark,
    detect_city_form_mode,
    wait_for_city_inputs,
)


class FakeElement:
    def __init__(self, attributes=None, *, role=None, name=None, children=None):
        self.attributes = attributes or {}
        self.aria_role = role
        self.accessible_name = name
        self.children = children or []

    def find_elements(self, by, selector):
        if by != By.CSS_SELECTOR or selector != "*":
            raise AssertionError(f"unexpected descendant query: {by} {selector}")
        return list(self.children)

    def is_displayed(self):
        return True

    def is_enabled(self):
        return True

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

    def test_captured_dom_has_current_wrappers_and_verified_accessible_inputs(self):
        self.assertIn('class="form-item-v3 flt-depart none-value"', self.fixture)
        self.assertIn('class="form-item-v3 flt-arrival"', self.fixture)
        self.assertIn('class="modifyDate depart-date"', self.fixture)
        self.assertIn('u_remark="日期选择框[flightWay:RT,date:2026-09-30,mode:departTime]"', self.fixture)
        self.assertIn('aria-label="可输入城市或机场"', self.fixture)
        self.assertIn('aria-label="请选择日期"', self.fixture)
        self.assertIn('readonly="true"', self.fixture)

    def test_detects_current_wrappers_and_resolves_exact_accessible_city_textboxes(self):
        departure_input = FakeElement(role="textbox", name=CURRENT_CITY_ACCESSIBLE_NAME)
        arrival_input = FakeElement(role="textbox", name=CURRENT_CITY_ACCESSIBLE_NAME)
        driver = FakeDriver(
            {
                LEGACY_CITY_INPUT_SELECTOR: [],
                CURRENT_DEPARTURE_FIELD_SELECTOR: [FakeElement(children=[departure_input])],
                CURRENT_ARRIVAL_FIELD_SELECTOR: [FakeElement(children=[arrival_input])],
            }
        )
        self.assertEqual(detect_city_form_mode(driver), CURRENT_CITY_WRAPPERS)
        self.assertEqual(wait_for_city_inputs(driver, timeout=0.1), [departure_input, arrival_input])

    def test_current_wrappers_without_accessible_editors_fail_closed(self):
        driver = FakeDriver(
            {
                LEGACY_CITY_INPUT_SELECTOR: [],
                CURRENT_DEPARTURE_FIELD_SELECTOR: [FakeElement()],
                CURRENT_ARRIVAL_FIELD_SELECTOR: [FakeElement()],
            }
        )
        with self.assertRaisesRegex(CtripSelectorMismatch, "accessible textbox"):
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

    def test_resolves_date_editor_by_its_accessible_name_inside_date_control(self):
        date_input = FakeElement(role="textbox", name=CURRENT_DATE_ACCESSIBLE_NAME)
        driver = FakeDriver(
            {
                CURRENT_DEPARTURE_DATE_SELECTOR: [
                    FakeElement(children=[date_input], attributes={CURRENT_DEPARTURE_DATE_ATTRIBUTE: "date:2026-09-30"})
                ]
            }
        )
        self.assertEqual(current_departure_date_input(driver, timeout=0.1), date_input)

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
