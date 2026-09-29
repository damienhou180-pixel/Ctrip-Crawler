"""Evidence-based selectors for Ctrip's current domestic flight page.

The current public form was observed on 2026-09-30 in a fresh anonymous
Chromium session. Its city controls are wrapper elements, not the legacy
``form-input-v3`` text inputs; the departure date is exposed via ``u_remark``.
"""

from __future__ import annotations

import re
from typing import Any

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait


LEGACY_CITY_INPUT_SELECTOR = ".form-input-v3"
CURRENT_DEPARTURE_FIELD_SELECTOR = ".form-item-v3.flt-depart"
CURRENT_ARRIVAL_FIELD_SELECTOR = ".form-item-v3.flt-arrival"
CURRENT_DEPARTURE_DATE_SELECTOR = ".modifyDate.depart-date"
CURRENT_DEPARTURE_DATE_ATTRIBUTE = "u_remark"

LEGACY_CITY_INPUTS = "legacy-city-inputs"
CURRENT_CITY_WRAPPERS = "current-city-wrappers"

_DEPARTURE_DATE_PATTERN = re.compile(r"(?:^|[\[,])date:(\d{4}-\d{2}-\d{2})(?=[,\]])")


class CtripSelectorMismatch(RuntimeError):
    """Raised when Ctrip renders a known layout without a supported editor."""


def detect_city_form_mode(driver: Any) -> str | None:
    """Return the observed city-field mode, or None while neither is present."""
    legacy_inputs = driver.find_elements(By.CSS_SELECTOR, LEGACY_CITY_INPUT_SELECTOR)
    if len(legacy_inputs) >= 2:
        return LEGACY_CITY_INPUTS

    departure = driver.find_elements(By.CSS_SELECTOR, CURRENT_DEPARTURE_FIELD_SELECTOR)
    arrival = driver.find_elements(By.CSS_SELECTOR, CURRENT_ARRIVAL_FIELD_SELECTOR)
    if departure and arrival:
        return CURRENT_CITY_WRAPPERS
    return None


def wait_for_city_inputs(driver: Any, timeout: float) -> list[Any]:
    """Wait for editable legacy inputs, rejecting the currently observed wrappers.

    The current page exposes labeled wrapper divs but the no-cookie run did not
    expose an editable textbox. Do not guess a child locator or repeatedly retry
    the stale selector; report the concrete layout and stop this city change.
    """
    try:
        mode = WebDriverWait(driver, timeout).until(detect_city_form_mode)
    except TimeoutException as exc:
        raise CtripSelectorMismatch(
            "Neither two legacy city inputs nor both current Ctrip city wrappers "
            f"({CURRENT_DEPARTURE_FIELD_SELECTOR}, {CURRENT_ARRIVAL_FIELD_SELECTOR}) appeared."
        ) from exc

    if mode == CURRENT_CITY_WRAPPERS:
        raise CtripSelectorMismatch(
            "Ctrip rendered the current labeled city wrappers "
            f"({CURRENT_DEPARTURE_FIELD_SELECTOR}, {CURRENT_ARRIVAL_FIELD_SELECTOR}), "
            f"not editable {LEGACY_CITY_INPUT_SELECTOR} inputs; the no-cookie page "
            "did not expose a verified textbox, so no child locator is guessed."
        )

    inputs = driver.find_elements(By.CSS_SELECTOR, LEGACY_CITY_INPUT_SELECTOR)
    if len(inputs) < 2:
        raise CtripSelectorMismatch(
            f"Expected two editable legacy city inputs matching {LEGACY_CITY_INPUT_SELECTOR}."
        )
    return inputs[:2]


def departure_date_from_remark(remark: str | None) -> str:
    """Extract YYYY-MM-DD from the observed date-picker ``u_remark`` attribute."""
    match = _DEPARTURE_DATE_PATTERN.search(remark or "")
    if not match:
        raise CtripSelectorMismatch(
            f"Could not read departure date from current date-picker metadata: {remark!r}."
        )
    return match.group(1)


def current_departure_date(driver: Any) -> str:
    """Read the departure date from the currently observed date-picker element."""
    controls = driver.find_elements(By.CSS_SELECTOR, CURRENT_DEPARTURE_DATE_SELECTOR)
    if len(controls) != 1:
        raise CtripSelectorMismatch(
            f"Expected one departure-date control matching {CURRENT_DEPARTURE_DATE_SELECTOR}; "
            f"found {len(controls)}."
        )
    return departure_date_from_remark(
        controls[0].get_attribute(CURRENT_DEPARTURE_DATE_ATTRIBUTE)
    )
