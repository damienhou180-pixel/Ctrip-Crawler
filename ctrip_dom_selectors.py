"""Evidence-based controls for Ctrip's public domestic-flight form.

On 2026-09-30, a fresh, anonymous Chromium session exposed the current city
textboxes by accessible name ``可输入城市或机场`` inside the observed departure
and arrival wrappers. The read-only date textbox was exposed as
``请选择日期`` inside ``.modifyDate.depart-date``; the chosen date remains
verifiable through the wrapper's ``u_remark`` value. No child is selected by a
guessed class or fixed DOM position.
"""

from __future__ import annotations

import re
from typing import Any

from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait


LEGACY_CITY_INPUT_SELECTOR = ".form-input-v3"
CURRENT_DEPARTURE_FIELD_SELECTOR = ".form-item-v3.flt-depart"
CURRENT_ARRIVAL_FIELD_SELECTOR = ".form-item-v3.flt-arrival"
CURRENT_DEPARTURE_DATE_SELECTOR = ".modifyDate.depart-date"
CURRENT_DEPARTURE_DATE_ATTRIBUTE = "u_remark"
CURRENT_CITY_ACCESSIBLE_NAME = "可输入城市或机场"
CURRENT_DATE_ACCESSIBLE_NAME = "请选择日期"

LEGACY_CITY_INPUTS = "legacy-city-inputs"
CURRENT_CITY_WRAPPERS = "current-city-wrappers"

_DEPARTURE_DATE_PATTERN = re.compile(r"(?:^|[\[,])date:(\d{4}-\d{2}-\d{2})(?=[,\]])")


class CtripSelectorMismatch(RuntimeError):
    """Raised when Ctrip renders a form with no uniquely verified editor."""


def _accessible_textboxes(container: Any, name: str) -> list[Any]:
    """Return visible, enabled descendant textboxes with the exact observed name."""
    try:
        descendants = container.find_elements(By.CSS_SELECTOR, "*")
    except (AttributeError, WebDriverException):
        return []
    result = []
    for element in descendants:
        try:
            if element.aria_role != "textbox" or element.accessible_name != name:
                continue
            if not element.is_displayed() or not element.is_enabled():
                continue
            result.append(element)
        except (AttributeError, WebDriverException):
            continue
    return result


def detect_city_form_mode(driver: Any) -> str | None:
    """Return the observed city-field layout, preferring wrapper-scoped controls."""
    departure = driver.find_elements(By.CSS_SELECTOR, CURRENT_DEPARTURE_FIELD_SELECTOR)
    arrival = driver.find_elements(By.CSS_SELECTOR, CURRENT_ARRIVAL_FIELD_SELECTOR)
    if departure and arrival:
        return CURRENT_CITY_WRAPPERS

    legacy_inputs = driver.find_elements(By.CSS_SELECTOR, LEGACY_CITY_INPUT_SELECTOR)
    if len(legacy_inputs) >= 2:
        return LEGACY_CITY_INPUTS
    return None


def _current_city_inputs(driver: Any) -> list[Any] | bool:
    departures = driver.find_elements(By.CSS_SELECTOR, CURRENT_DEPARTURE_FIELD_SELECTOR)
    arrivals = driver.find_elements(By.CSS_SELECTOR, CURRENT_ARRIVAL_FIELD_SELECTOR)
    if len(departures) != 1 or len(arrivals) != 1:
        return False
    departure_fields = _accessible_textboxes(departures[0], CURRENT_CITY_ACCESSIBLE_NAME)
    arrival_fields = _accessible_textboxes(arrivals[0], CURRENT_CITY_ACCESSIBLE_NAME)
    if len(departure_fields) != 1 or len(arrival_fields) != 1:
        return False
    return [departure_fields[0], arrival_fields[0]]


def wait_for_city_inputs(driver: Any, timeout: float) -> list[Any]:
    """Wait for two current accessible textboxes or two genuinely legacy inputs."""
    try:
        mode = WebDriverWait(driver, timeout).until(detect_city_form_mode)
    except TimeoutException as exc:
        raise CtripSelectorMismatch(
            "Neither the observed current city wrappers nor two legacy city inputs appeared."
        ) from exc

    if mode == CURRENT_CITY_WRAPPERS:
        try:
            inputs = WebDriverWait(driver, timeout).until(_current_city_inputs)
        except TimeoutException as exc:
            raise CtripSelectorMismatch(
                "Ctrip rendered the current departure/arrival wrappers but did not expose "
                f"exactly one accessible textbox named {CURRENT_CITY_ACCESSIBLE_NAME!r} in each; "
                "no child locator was guessed."
            ) from exc
        return list(inputs)

    inputs = driver.find_elements(By.CSS_SELECTOR, LEGACY_CITY_INPUT_SELECTOR)
    if len(inputs) < 2:
        raise CtripSelectorMismatch(
            f"Expected two legacy city inputs matching {LEGACY_CITY_INPUT_SELECTOR}."
        )
    return inputs[:2]


def current_departure_date_input(driver: Any, timeout: float = 5.0) -> Any:
    """Return the single accessible date textbox inside the observed date control."""
    def find_input(current_driver: Any) -> Any | bool:
        controls = current_driver.find_elements(By.CSS_SELECTOR, CURRENT_DEPARTURE_DATE_SELECTOR)
        if len(controls) != 1:
            return False
        inputs = _accessible_textboxes(controls[0], CURRENT_DATE_ACCESSIBLE_NAME)
        return inputs[0] if len(inputs) == 1 else False

    try:
        return WebDriverWait(driver, timeout).until(find_input)
    except TimeoutException as exc:
        raise CtripSelectorMismatch(
            f"Expected exactly one accessible date textbox named {CURRENT_DATE_ACCESSIBLE_NAME!r} "
            f"inside {CURRENT_DEPARTURE_DATE_SELECTOR}; no input was guessed."
        ) from exc


def departure_date_from_remark(remark: str | None) -> str:
    """Extract YYYY-MM-DD from the observed date-picker ``u_remark`` attribute."""
    match = _DEPARTURE_DATE_PATTERN.search(remark or "")
    if not match:
        raise CtripSelectorMismatch(
            f"Could not read departure date from current date-picker metadata: {remark!r}."
        )
    return match.group(1)


def current_departure_date(driver: Any) -> str:
    """Read the selected date from the observed ``u_remark`` metadata."""
    controls = driver.find_elements(By.CSS_SELECTOR, CURRENT_DEPARTURE_DATE_SELECTOR)
    if len(controls) != 1:
        raise CtripSelectorMismatch(
            f"Expected one departure-date control matching {CURRENT_DEPARTURE_DATE_SELECTOR}; "
            f"found {len(controls)}."
        )
    return departure_date_from_remark(controls[0].get_attribute(CURRENT_DEPARTURE_DATE_ATTRIBUTE))
