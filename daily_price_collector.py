"""Anonymous, paced daily Ctrip price collector.

The legacy interactive scraper is intentionally not imported: this entry point
uses a fresh ordinary browser profile, no saved account/cookies, no proxy, no
stealth flags, and no automatic query retries.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from airport_scope import (
    Airport,
    AirportCoverageUnverified,
    DESTINATION_AIRPORTS,
    build_airport_route_pairs,
    build_matrix_plan,
    coverage_report,
)
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

from browser_network_capture import BrowserNetworkCaptureDriver
from ctrip_dom_selectors import (
    CURRENT_ARRIVAL_FIELD_SELECTOR,
    CURRENT_DEPARTURE_DATE_SELECTOR,
    CURRENT_DEPARTURE_FIELD_SELECTOR,
    CtripSelectorMismatch,
    current_departure_date,
    current_departure_date_input,
    wait_for_city_inputs,
)
from price_history import PriceHistoryStore, gmt8_now

HOME_URL = "https://flights.ctrip.com/online/channel/domestic"
SOURCE = "Ctrip public domestic-flight UI via Chromium Network events"
BATCH_SEARCH_PATH = "/international/search/api/search/batchSearch"
DEFAULT_DATABASE = Path(__file__).resolve().parent / "data" / "ctrip_price_history.sqlite3"
TIME_ZONE = ZoneInfo("Asia/Shanghai")
CITY_ACCESSIBLE_NAME = "可输入城市或机场"
DATE_ACCESSIBLE_NAME = "请选择日期"
CALENDAR_TITLE_SELECTOR = ".date-title"
CALENDAR_DAY_SELECTOR = ".date-day"
CALENDAR_DAY_NUMBER_SELECTOR = ".date-d"
# These two legacy calendar locators are used only if present and visible; every
# click is followed by a displayed-month check. The date/day/title selectors
# above were observed in the live public form on 2026-09-30.
CALENDAR_PREVIOUS_SELECTOR = ".in-date-picker.icon.prev-ico.iconf-left"
CALENDAR_NEXT_SELECTOR = ".in-date-picker.icon.next-ico.iconf-right"
MINIMUM_QUERY_DELAY_SECONDS = 5.0


class CollectionError(RuntimeError):
    """A non-retryable DOM, network, or parsing failure for this run."""


class AccessBlocked(CollectionError):
    """The site requires login/verification or refuses the request."""


@dataclass(frozen=True)
class Query:
    departure_city: str
    arrival_city: str
    departure_date: str
    departure_airport_code: str | None = None
    departure_airport_name: str | None = None
    arrival_airport_code: str | None = None
    arrival_airport_name: str | None = None

    def as_scope_row(self) -> dict[str, Any]:
        return asdict(self)


def build_date_window(run_date: date | None = None) -> list[str]:
    """Thirty natural dates beginning on the run date in GMT+8, inclusive."""
    start = run_date or datetime.now(TIME_ZONE).date()
    return [(start + timedelta(days=offset)).isoformat() for offset in range(30)]


def build_scope(
    run_date: date | None = None,
    inventory: dict[str, Any] = DESTINATION_AIRPORTS,
) -> list[Query]:
    """Build all 30-day airport-pair queries or refuse an incomplete inventory."""
    plan = build_matrix_plan(inventory)
    routes = build_airport_route_pairs(inventory)
    dates = build_date_window(run_date)
    scope = [
        Query(
            origin.city,
            destination.city,
            day,
            origin.code,
            origin.name,
            destination.code,
            destination.name,
        )
        for origin, destination in routes
        for day in dates
    ]
    if len(scope) != plan["expected_queries"]:
        raise AssertionError("Airport query scope does not match the shared matrix formula.")
    return scope


def _selected_city_value(value: str | None, city: str) -> bool:
    """Require a selected city value ending in a 3-letter city/airport code."""
    if not value or not value.startswith(city + "(") or not value.endswith(")"):
        return False
    return re.search(r"\([A-Z]{3}\)$", value) is not None


def _candidate_city_options(driver: Any, city: str) -> list[Any]:
    """Find a visible city-level option; refuse POIs explicitly named as airports."""
    candidates: list[Any] = []
    # A role=option or the exact POI attribute observed in the live autocomplete
    # is in scope. Generic buttons/links elsewhere on the page are not.
    for element in driver.find_elements(
        By.CSS_SELECTOR, "[role='option'], li[data-u_key='poi_select_item']"
    ):
        try:
            if not element.is_displayed() or not element.is_enabled():
                continue
            text = (element.accessible_name or element.text or "").strip()
            remark = element.get_attribute("data-u_remark") or ""
            if "Name:" in remark and re.search(r"Name:[^,\]]*机场", remark):
                continue
            if "机场" in text:
                continue
            if text.startswith(city + "(") and re.search(r"\([A-Z]{3}\)$", text):
                candidates.append(element)
        except WebDriverException:
            continue
    return candidates


def _visible_city_suggestions(driver: Any, city: str) -> list[str]:
    """Summarize exact live POI items for diagnostics without choosing an airport."""
    suggestions = []
    # This data attribute was observed on the live Shanghai autocomplete items;
    # it is used only to report the displayed choices, never to select one.
    for element in driver.find_elements(By.CSS_SELECTOR, "li[data-u_key='poi_select_item']"):
        try:
            if not element.is_displayed():
                continue
            text = (element.text or "").strip()
            if city not in text:
                continue
            remark = element.get_attribute("data-u_remark") or ""
            suggestions.append(f"{text} [{remark}]")
        except WebDriverException:
            continue
    return suggestions


_AIRPORT_IDENTITY_PATTERN = re.compile(r"Name:([^,\]]+),Code:([A-Z0-9]{3})(?:[,\]]|$)")


def _airport_identity(remark: str | None) -> tuple[str, str] | None:
    match = _AIRPORT_IDENTITY_PATTERN.search(remark or "")
    return (match.group(1), match.group(2)) if match else None


def _selected_airport_value(value: str | None, airport: Airport) -> bool:
    return bool(
        value
        and value.startswith(airport.city + "(")
        and value.endswith(f"({airport.code})")
    )


def _candidate_airport_options(driver: Any, airport: Airport) -> list[Any]:
    """Find exact visible Ctrip POIs by their displayed name and observed code."""
    candidates = []
    for element in driver.find_elements(By.CSS_SELECTOR, "li[data-u_key='poi_select_item']"):
        try:
            if not element.is_displayed() or not element.is_enabled():
                continue
            text = (element.accessible_name or element.text or "").strip()
            identity = _airport_identity(element.get_attribute("data-u_remark"))
            if (
                identity == (airport.name, airport.code)
                and airport.city in text
            ):
                candidates.append(element)
        except WebDriverException:
            continue
    return candidates


def choose_airport(
    driver: Any,
    input_element: Any,
    airport: Airport,
    timeout: float = 4.0,
) -> str:
    """Select one exact visible airport POI and verify the selected IATA/code suffix."""
    input_element.click()
    input_element.send_keys(Keys.CONTROL + "a")
    input_element.send_keys(airport.city)

    def selected_or_option(_driver: Any) -> Any:
        value = input_element.get_attribute("value")
        if _selected_airport_value(value, airport):
            return ("selected", value)
        options = _candidate_airport_options(_driver, airport)
        if options:
            return ("options", options)
        return False

    try:
        outcome = WebDriverWait(driver, timeout, poll_frequency=0.2).until(selected_or_option)
    except TimeoutException as exc:
        raise CtripSelectorMismatch(
            f"The visible {airport.city} airport option {airport.name} ({airport.code}) "
            "was not uniquely verifiable; the query was not submitted."
        ) from exc

    if outcome[0] == "options":
        if len(outcome[1]) != 1:
            raise CtripSelectorMismatch(
                f"Airport {airport.city}/{airport.code} produced {len(outcome[1])} exact visible options; "
                "the query was not submitted."
            )
        outcome[1][0].click()
        try:
            WebDriverWait(driver, timeout, poll_frequency=0.2).until(
                lambda _d: _selected_airport_value(
                    input_element.get_attribute("value"), airport
                )
            )
        except TimeoutException as exc:
            raise CtripSelectorMismatch(
                f"Selecting {airport.name} ({airport.code}) did not verify in the city textbox."
            ) from exc

    selected_value = input_element.get_attribute("value")
    if not _selected_airport_value(selected_value, airport):
        raise CtripSelectorMismatch(
            f"Airport selection verification failed for {airport.city}/{airport.code}."
        )
    return selected_value


def choose_city(driver: Any, input_element: Any, city: str, timeout: float = 4.0) -> str:
    """Select one unambiguous visible city suggestion and verify its chosen value."""
    input_element.click()
    # This chord was verified against the live current textbox; clear() did not
    # replace the selected-city text in the controlled React input.
    input_element.send_keys(Keys.CONTROL + "a")
    input_element.send_keys(city)

    def selected_or_option(_driver: Any) -> Any:
        value = input_element.get_attribute("value")
        if _selected_city_value(value, city):
            return ("selected", value)
        options = _candidate_city_options(_driver, city)
        if options:
            return ("options", options)
        airport_options = _visible_city_suggestions(_driver, city)
        if airport_options:
            return ("airport_options", airport_options)
        return False

    try:
        outcome = WebDriverWait(driver, timeout, poll_frequency=0.2).until(selected_or_option)
    except TimeoutException as exc:
        raise CtripSelectorMismatch(
            f"The accessible city textbox did not expose a verifiable {city} selection; "
            "the query was not submitted."
        ) from exc

    if outcome[0] == "airport_options":
        raise CtripSelectorMismatch(
            f"The visible {city} suggestions are airport-specific, not a verified city-level option: "
            + "; ".join(outcome[1])
            + ". The query was not submitted."
        )

    if outcome[0] == "options":
        options = outcome[1]
        if len(options) != 1:
            raise CtripSelectorMismatch(
                f"City {city} produced {len(options)} matching visible options; "
                "the query was not submitted because the choice is ambiguous."
            )
        options[0].click()
        try:
            WebDriverWait(driver, timeout, poll_frequency=0.2).until(
                lambda _d: _selected_city_value(input_element.get_attribute("value"), city)
            )
        except TimeoutException as exc:
            raise CtripSelectorMismatch(
                f"Selecting the visible {city} option did not update the city textbox."
            ) from exc

    selected_value = input_element.get_attribute("value")
    if not _selected_city_value(selected_value, city):
        raise CtripSelectorMismatch(f"City selection verification failed for {city}.")
    return selected_value


def _month_from_title(text: str) -> tuple[int, int] | None:
    match = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月", text or "")
    return (int(match.group(1)), int(match.group(2))) if match else None


def _month_number(value: tuple[int, int]) -> int:
    return value[0] * 12 + value[1]


def _visible_titles(driver: Any) -> list[Any]:
    return [
        element
        for element in driver.find_elements(By.CSS_SELECTOR, CALENDAR_TITLE_SELECTOR)
        if element.is_displayed() and _month_from_title(element.text)
    ]


def _calendar_panel_for_title(title: Any) -> Any | None:
    # The nearest ancestor containing displayed date-day elements is the panel
    # associated with this displayed month. This relies on live DOM structure,
    # not guessed fixed parent depth.
    for ancestor in title.find_elements(By.XPATH, "ancestor::*"):
        try:
            if ancestor.is_displayed() and ancestor.find_elements(
                By.CSS_SELECTOR, CALENDAR_DAY_SELECTOR
            ):
                return ancestor
        except WebDriverException:
            continue
    return None


def _visible_months(driver: Any) -> list[tuple[int, int]]:
    months = []
    for title in _visible_titles(driver):
        month = _month_from_title(title.text)
        if month and month not in months:
            months.append(month)
    return months


def _navigate_calendar_month(driver: Any, direction: int, before: list[tuple[int, int]]) -> None:
    selector = CALENDAR_NEXT_SELECTOR if direction > 0 else CALENDAR_PREVIOUS_SELECTOR
    controls = [
        control
        for control in driver.find_elements(By.CSS_SELECTOR, selector)
        if control.is_displayed() and control.is_enabled()
    ]
    if not controls:
        raise CtripSelectorMismatch(
            "The open date picker did not expose a visible month-navigation control; "
            "the query was not submitted."
        )
    controls[0].click()
    try:
        WebDriverWait(driver, 4, poll_frequency=0.2).until(
            lambda d: _visible_months(d) != before
        )
    except TimeoutException as exc:
        raise CtripSelectorMismatch(
            "The date-picker month did not change after one navigation action."
        ) from exc


def choose_departure_date(driver: Any, target: str, timeout: float = 4.0) -> None:
    """Select an enabled visible day and verify it via the current ``u_remark``."""
    if current_departure_date(driver) == target:
        return
    date_input = current_departure_date_input(driver, timeout=timeout)
    date_input.click()
    try:
        WebDriverWait(driver, timeout, poll_frequency=0.2).until(
            lambda d: bool(_visible_titles(d))
        )
    except TimeoutException as exc:
        raise CtripSelectorMismatch("The departure date picker did not open.") from exc

    wanted = date.fromisoformat(target)
    wanted_month = (wanted.year, wanted.month)
    for _ in range(2):
        titles = _visible_titles(driver)
        for title in titles:
            if _month_from_title(title.text) != wanted_month:
                continue
            panel = _calendar_panel_for_title(title)
            if panel is None:
                continue
            for day_cell in panel.find_elements(By.CSS_SELECTOR, CALENDAR_DAY_SELECTOR):
                classes = (day_cell.get_attribute("class") or "").split()
                if "date-disabled" in classes:
                    continue
                day_number_nodes = day_cell.find_elements(
                    By.CSS_SELECTOR, CALENDAR_DAY_NUMBER_SELECTOR
                )
                day_text = day_number_nodes[0].text.strip() if day_number_nodes else day_cell.text.strip()
                if day_text == str(wanted.day):
                    day_cell.click()
                    try:
                        WebDriverWait(driver, timeout, poll_frequency=0.2).until(
                            lambda _d: current_departure_date(driver) == target
                        )
                    except TimeoutException as exc:
                        raise CtripSelectorMismatch(
                            f"Clicked {target}, but the date control did not verify it."
                        ) from exc
                    return

        visible = _visible_months(driver)
        if not visible:
            break
        if any(_month_number(month) < _month_number(wanted_month) for month in visible) and max(
            map(_month_number, visible)
        ) < _month_number(wanted_month):
            _navigate_calendar_month(driver, 1, visible)
        elif any(_month_number(month) > _month_number(wanted_month) for month in visible) and min(
            map(_month_number, visible)
        ) > _month_number(wanted_month):
            _navigate_calendar_month(driver, -1, visible)
        else:
            break

    raise CtripSelectorMismatch(
        f"The date picker did not expose an enabled cell for {target}; the query was not submitted."
    )


def _ensure_one_way(driver: Any) -> None:
    labels = [
        item
        for item in driver.find_elements(By.CLASS_NAME, "radio-label")
        if item.is_displayed() and item.text.strip() == "单程"
    ]
    if len(labels) != 1:
        raise CtripSelectorMismatch(
            f"Expected one visible 单程 selector, found {len(labels)}."
        )
    labels[0].click()


def _page_block_reason(driver: Any) -> str | None:
    """Return a stop reason for challenges, authentication, or explicit refusal."""
    for selector, reason in (
        ("#verification-code", "验证码控件 appeared"),
        ("[data-testid='doubleAuthSwitcherBox']", "二次验证/authentication challenge appeared"),
        (".lg_loginbox_modal", "登录弹窗 appeared; account/cookie use is prohibited"),
        ("[class*='captcha']", "captcha control appeared"),
    ):
        for element in driver.find_elements(By.CSS_SELECTOR, selector):
            try:
                if element.is_displayed():
                    return reason
            except WebDriverException:
                continue

    for element in driver.find_elements(By.CSS_SELECTOR, "input[type='password']"):
        try:
            if element.is_displayed():
                return "Visible password/login input appeared; account/cookie use is prohibited"
        except WebDriverException:
            continue

    try:
        text = (driver.title + "\n" + driver.find_element(By.TAG_NAME, "body").text).lower()
    except WebDriverException:
        text = (getattr(driver, "title", "") or "").lower()
    blocked_phrases = (
        "access denied",
        "forbidden",
        "访问被拒绝",
        "拒绝访问",
        "访问频繁",
        "访问过于频繁",
        "异常访问",
        "请求过于频繁",
        "请完成验证",
        "请输入验证码",
    )
    return "Site displayed an access-denial or anti-bot message" if any(
        phrase in text for phrase in blocked_phrases
    ) else None


def _assert_not_blocked(driver: Any) -> None:
    reason = _page_block_reason(driver)
    if reason:
        raise AccessBlocked(reason)


def create_driver() -> BrowserNetworkCaptureDriver:
    proxy_env_names = {
        "http_proxy", "https_proxy", "all_proxy", "ftp_proxy", "socks_proxy",
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "FTP_PROXY", "SOCKS_PROXY",
    }
    configured = sorted(proxy_env_names.intersection(os.environ))
    if configured:
        raise CollectionError(
            "Proxy environment variables are configured; the collector will not use a proxy."
        )

    options = webdriver.ChromeOptions()
    # Chromium's native performance log/CDP channel is used only to capture the
    # response generated by the visible UI. No TLS override, proxy, or stealth flag.
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    options.add_experimental_option("perfLoggingPrefs", {"enableNetwork": True})
    raw_driver = webdriver.Chrome(options=options)
    proxy = raw_driver.capabilities.get("proxy") or {}
    proxy_type = str(proxy.get("proxyType", "direct")).lower()
    if proxy_type not in ("direct", "unspecified"):
        raw_driver.quit()
        raise CollectionError("Browser reported a proxy configuration; collection stopped.")
    if raw_driver.capabilities.get("acceptInsecureCerts") is True:
        raw_driver.quit()
        raise CollectionError("Browser certificate verification is not at its default secure setting.")
    return BrowserNetworkCaptureDriver(raw_driver)


def _safe_error(error: BaseException) -> str:
    text = f"{type(error).__name__}: {error}"
    text = re.sub(r"https?://[^\s?]+\?[^\s]+", lambda match: match.group(0).split("?", 1)[0], text)
    return text[:1200]


def _decimal(value: Any, field: str) -> Decimal:
    if value is None or value == "":
        raise ValueError(f"Missing fare field {field}.")
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid fare field {field}: {value!r}.") from exc


def _money_text(value: Decimal | None) -> str | None:
    return format(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), ".2f") if value is not None else None


def _economy_fare(price: dict[str, Any]) -> Decimal:
    adult_price = _decimal(price.get("adultPrice"), "adultPrice")
    if "adultTax" in price and price.get("adultTax") is not None:
        adult_tax = _decimal(price["adultTax"], "adultTax")
    else:
        sort_price = _decimal(price.get("sortPrice", adult_price), "sortPrice")
        estimated_tax = sort_price - adult_price if not price.get("freeOilFeeAndTax") else Decimal("0")
        adult_tax = estimated_tax.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return (adult_price + adult_tax).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _as_nonnegative_int(value: Any, field: str) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {field}: {value!r}.") from exc
    if number < 0:
        raise ValueError(f"Invalid negative {field}: {value!r}.")
    return number


def _is_login_required(payload: Any) -> bool:
    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key).lower() in {"needuserlogin", "needlogin", "requirelogin"} and value is True:
                return True
            if _is_login_required(value):
                return True
    elif isinstance(payload, list):
        return any(_is_login_required(item) for item in payload)
    return False


def _response_route_metadata(payload: dict[str, Any]) -> dict[str, Any] | None:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    segments = payload.get("flightSegments") or data.get("flightSegments")
    if not isinstance(segments, list) or not segments or not isinstance(segments[0], dict):
        return None
    return segments[0]


def _validate_request_body(request_body: bytes, query: Query) -> None:
    """Verify the route/date the visible page actually submitted before parsing fares."""
    if not request_body:
        raise CollectionError("Captured search request omitted its body; route/date cannot be verified.")
    try:
        request_payload = json.loads(request_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CollectionError("Captured search request body was not valid UTF-8 JSON.") from exc
    segments = request_payload.get("flightSegments") if isinstance(request_payload, dict) else None
    if not isinstance(segments, list) or not segments or not isinstance(segments[0], dict):
        raise CollectionError("Captured request omitted the observed flightSegments query fields.")
    segment = segments[0]
    observed = (
        segment.get("departureCityName"),
        segment.get("arrivalCityName"),
        segment.get("departureDate"),
    )
    expected = (query.departure_city, query.arrival_city, query.departure_date)
    if observed != expected:
        raise CollectionError(
            f"Submitted request route/date did not match scope (expected={expected!r}, observed={observed!r})."
        )
    if query.departure_airport_code or query.arrival_airport_code:
        airport_codes = (
            segment.get("departureAirportCode"),
            segment.get("arrivalAirportCode"),
        )
        expected_codes = (
            query.departure_airport_code,
            query.arrival_airport_code,
        )
        if None in expected_codes or airport_codes != expected_codes:
            raise CollectionError(
                "Submitted request omitted or mismatched the exact airport codes "
                f"(expected={expected_codes!r}, observed={airport_codes!r})."
            )


def parse_response_payload(
    payload: dict[str, Any],
    query: Query,
    captured_at: str,
    source: str = SOURCE,
    *,
    request_route_verified: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return only direct economy fare cards plus a separate all-itinerary summary."""
    if _is_login_required(payload):
        raise AccessBlocked("Flight response indicated login is required.")
    route_meta = _response_route_metadata(payload)
    if route_meta is None and not request_route_verified:
        raise CollectionError(
            "Response omitted route metadata and no captured request proof was supplied."
        )
    if route_meta is not None:
        expected = (query.departure_city, query.arrival_city, query.departure_date)
        observed = (
            route_meta.get("departureCityName"),
            route_meta.get("arrivalCityName"),
            route_meta.get("departureDate"),
        )
        if observed != expected:
            raise CollectionError(
                f"Response route/date did not match request (expected={expected!r}, observed={observed!r})."
            )

    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("flightItineraryList"), list):
        raise CollectionError("Response omitted data.flightItineraryList; schema changed or failed.")
    itineraries = data["flightItineraryList"]
    observations: list[dict[str, Any]] = []
    counts_by_stops: dict[int, int] = {}
    min_by_stops: dict[int, Decimal] = {}
    all_economy_prices: list[Decimal] = []
    direct_economy_prices: list[Decimal] = []
    direct_count = 0

    for itinerary in itineraries:
        if not isinstance(itinerary, dict):
            raise CollectionError("Response contained a non-object flight itinerary.")
        segments = itinerary.get("flightSegments") or []
        if not segments or not isinstance(segments[0], dict):
            raise CollectionError("Flight itinerary omitted its first flight segment.")
        segment = segments[0]
        flights = segment.get("flightList") or []
        if not isinstance(flights, list) or not flights:
            raise CollectionError("Flight segment omitted flightList.")
        flight = flights[0]
        transfer_count = _as_nonnegative_int(
            segment.get("transferCount", itinerary.get("transferCount", 0)), "transferCount"
        )
        stop_count = _as_nonnegative_int(flight.get("stopCount", 0), "stopCount")
        stop_list = segment.get("stopList") or flight.get("stopList") or []
        stops = max(transfer_count, stop_count, max(0, len(flights) - 1), len(stop_list))
        is_direct = stops == 0 and len(flights) == 1 and not stop_list
        if is_direct:
            direct_count += 1
            if query.departure_airport_code or query.arrival_airport_code:
                observed_airports = (
                    flight.get("departureAirportCode"),
                    flight.get("arrivalAirportCode"),
                )
                expected_airports = (
                    query.departure_airport_code,
                    query.arrival_airport_code,
                )
                if None in expected_airports or observed_airports != expected_airports:
                    raise CollectionError(
                        "Direct itinerary airport codes did not match the selected airport pair "
                        f"(expected={expected_airports!r}, observed={observed_airports!r})."
                    )

        price_list = itinerary.get("priceList") or []
        if not isinstance(price_list, list):
            raise CollectionError("Flight itinerary priceList is not a list.")
        for price in price_list:
            if not isinstance(price, dict):
                raise CollectionError("Response contained a non-object fare card.")
            if str(price.get("cabin", "")).upper() != "Y":
                continue
            fare = _economy_fare(price)
            all_economy_prices.append(fare)
            counts_by_stops[stops] = counts_by_stops.get(stops, 0) + 1
            if stops not in min_by_stops or fare < min_by_stops[stops]:
                min_by_stops[stops] = fare
            if not is_direct:
                continue
            direct_economy_prices.append(fare)
            flight_no = str(
                flight.get("flightNo")
                or itinerary.get("flightNo")
                or str(itinerary.get("itineraryId", "")).split("_")[0]
            ).strip()
            if not flight_no:
                raise CollectionError("Direct economy fare card omitted a flight number.")
            observations.append(
                {
                    "departure_airport": flight.get("departureAirportName"),
                    "departure_airport_code": flight.get("departureAirportCode"),
                    "arrival_airport": flight.get("arrivalAirportName"),
                    "arrival_airport_code": flight.get("arrivalAirportCode"),
                    "flight_no": flight_no,
                    "airline": flight.get("marketAirlineName") or flight.get("operateAirlineName"),
                    "departure_time": flight.get("departureDateTime"),
                    "arrival_time": flight.get("arrivalDateTime"),
                    "economy_fare_cny": _money_text(fare),
                    "currency": "CNY",
                    "stops": stops,
                    "direct": 1,
                    "economy": 1,
                    "status": "ok",
                    "error": None,
                    "flight_card_json": json.dumps(itinerary, ensure_ascii=False, sort_keys=True),
                    "fare_card_json": json.dumps(price, ensure_ascii=False, sort_keys=True),
                }
            )

    connecting_count = len(itineraries) - direct_count
    summary = {
        "itinerary_count": len(itineraries),
        "direct_itinerary_count": direct_count,
        "connecting_itinerary_count": connecting_count,
        "all_economy_min_fare_cny": _money_text(min(all_economy_prices) if all_economy_prices else None),
        "direct_economy_min_fare_cny": _money_text(min(direct_economy_prices) if direct_economy_prices else None),
        "transfer_summary": {
            "economy_fare_card_count_by_stops": {str(key): counts_by_stops[key] for key in sorted(counts_by_stops)},
            "economy_min_fare_cny_by_stops": {
                str(key): _money_text(min_by_stops[key]) for key in sorted(min_by_stops)
            },
        },
        # Summary fields intentionally exclude flight/fare-card arrays.
        "summary": {
            "has_connecting_itineraries": connecting_count > 0,
            "no_economy_fares": not all_economy_prices,
        },
    }
    return observations, summary


def _parse_response_body(body: bytes) -> dict[str, Any]:
    if body.startswith(b"\x1f\x8b"):
        import gzip

        body = gzip.decompress(body)
    try:
        result = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        lowered = body[:4000].decode("utf-8", errors="ignore").lower()
        if any(term in lowered for term in ("access denied", "forbidden", "captcha", "验证码", "访问频繁")):
            raise AccessBlocked("Search response contained an access-denial or challenge page.") from exc
        raise CollectionError("Search response was not valid UTF-8 JSON.") from exc
    if not isinstance(result, dict):
        raise CollectionError("Search response JSON root was not an object.")
    return result


def collect_one_query(driver: BrowserNetworkCaptureDriver, query: Query) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    """Submit exactly one visible UI query and return its captured API response."""
    if not all((query.departure_airport_code, query.departure_airport_name, query.arrival_airport_code, query.arrival_airport_name)):
        raise CollectionError("Airport-level scope is required; city-only queries are not allowed.")
    driver.get(HOME_URL)
    try:
        WebDriverWait(driver, 18, poll_frequency=0.25).until(
            lambda d: d.find_elements(By.CSS_SELECTOR, CURRENT_DEPARTURE_FIELD_SELECTOR)
            and d.find_elements(By.CSS_SELECTOR, CURRENT_ARRIVAL_FIELD_SELECTOR)
        )
    except TimeoutException as exc:
        _assert_not_blocked(driver)
        raise CtripSelectorMismatch("Current public flight form wrappers did not appear.") from exc
    _assert_not_blocked(driver)

    city_inputs = wait_for_city_inputs(driver, timeout=8)
    if len(city_inputs) != 2:
        raise CtripSelectorMismatch(f"Expected two verified city textboxes, found {len(city_inputs)}.")
    choose_airport(
        driver,
        city_inputs[0],
        Airport(query.departure_city, query.departure_airport_name, query.departure_airport_code),
    )
    _assert_not_blocked(driver)
    choose_airport(
        driver,
        city_inputs[1],
        Airport(query.arrival_city, query.arrival_airport_name, query.arrival_airport_code),
    )
    _assert_not_blocked(driver)
    choose_departure_date(driver, query.departure_date)
    _ensure_one_way(driver)
    _assert_not_blocked(driver)

    driver.clear_requests()
    search_buttons = driver.find_elements(By.CSS_SELECTOR, ".search-btn")
    if len(search_buttons) != 1 or not search_buttons[0].is_displayed() or not search_buttons[0].is_enabled():
        raise CtripSelectorMismatch("Expected one visible, enabled search button.")
    search_buttons[0].click()

    try:
        request = driver.wait_for_request(BATCH_SEARCH_PATH, timeout=20)
    except TimeoutError as exc:
        _assert_not_blocked(driver)
        raise CollectionError("No batch-search response was captured; no retry was attempted.") from exc
    _assert_not_blocked(driver)
    if request.response is None:
        raise CollectionError("Captured search request had no response.")
    if request.response.status_code in (401, 403, 429):
        raise AccessBlocked(f"Search response HTTP status {request.response.status_code}.")
    if request.response.status_code < 200 or request.response.status_code >= 300:
        raise CollectionError(f"Search response HTTP status {request.response.status_code}.")
    payload = _parse_response_body(request.response.body)
    if _is_login_required(payload):
        raise AccessBlocked("Flight response indicated login is required.")
    _validate_request_body(request.body, query)
    captured_at = gmt8_now()
    observations, summary = parse_response_payload(
        payload, query, captured_at, request_route_verified=True
    )
    query_status = "success" if observations else "no_results"
    return observations, summary, query_status


def run_collection(
    database_path: str | Path = DEFAULT_DATABASE,
    *,
    run_date: date | None = None,
    max_queries: int | None = None,
    delay_seconds: float = MINIMUM_QUERY_DELAY_SECONDS,
    driver_factory: Any = create_driver,
    inventory: dict[str, Any] = DESTINATION_AIRPORTS,
) -> dict[str, Any]:
    if delay_seconds < MINIMUM_QUERY_DELAY_SECONDS:
        raise ValueError(f"delay_seconds must be at least {MINIMUM_QUERY_DELAY_SECONDS:g} seconds.")
    plan = build_matrix_plan(inventory)
    scope = build_scope(run_date, inventory)
    run_id = str(uuid.uuid4())
    store = PriceHistoryStore(database_path)
    store.create_run(run_id, [query.as_scope_row() for query in scope])
    driver = None
    stop_reason: str | None = None
    attempted_count = 0
    limit = len(scope) if max_queries is None else max(0, min(int(max_queries), len(scope)))

    try:
        driver = driver_factory()
        for index, query in enumerate(scope[:limit]):
            store.mark_query_running(
                run_id,
                query.departure_city,
                query.arrival_city,
                query.departure_date,
                query.departure_airport_code,
                query.arrival_airport_code,
            )
            attempted_count += 1
            try:
                observations, summary, query_status = collect_one_query(driver, query)
                store.record_query_result(
                    run_id=run_id,
                    departure_city=query.departure_city,
                    arrival_city=query.arrival_city,
                    departure_date=query.departure_date,
                    departure_airport_code=query.departure_airport_code,
                    arrival_airport_code=query.arrival_airport_code,
                    status=query_status,
                    captured_at=gmt8_now(),
                    source=SOURCE,
                    observations=observations,
                    summary=summary,
                )
            except AccessBlocked as exc:
                stop_reason = _safe_error(exc)
                store.record_query_result(
                    run_id=run_id,
                    departure_city=query.departure_city,
                    arrival_city=query.arrival_city,
                    departure_date=query.departure_date,
                    departure_airport_code=query.departure_airport_code,
                    arrival_airport_code=query.arrival_airport_code,
                    status="blocked",
                    captured_at=gmt8_now(),
                    source=SOURCE,
                    error=stop_reason,
                )
                break
            except Exception as exc:
                stop_reason = _safe_error(exc)
                store.record_query_result(
                    run_id=run_id,
                    departure_city=query.departure_city,
                    arrival_city=query.arrival_city,
                    departure_date=query.departure_date,
                    departure_airport_code=query.departure_airport_code,
                    arrival_airport_code=query.arrival_airport_code,
                    status="error",
                    captured_at=gmt8_now(),
                    source=SOURCE,
                    error=stop_reason,
                )
                break
            if index + 1 < limit:
                time.sleep(delay_seconds)
    except Exception as exc:
        stop_reason = _safe_error(exc)
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
        result = store.finish_run(run_id, stop_reason)
        store.close()

    result["scope_routes"] = plan["directed_routes_per_departure_date"]
    result["scope_airport_counts_by_city"] = plan["airport_counts_by_city"]
    result["scope_airport_sum"] = plan["S"]
    result["scope_dates"] = plan["departure_dates"]
    result["scope_formula"] = plan["formula"]
    result["scope_total"] = len(scope)
    result["stop_reason"] = stop_reason
    result["validation_limit"] = max_queries
    return result


def _database_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", default=str(DEFAULT_DATABASE), help=f"SQLite path (default: {DEFAULT_DATABASE})")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Anonymous, append-only Ctrip price history collector")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="Run the complete verified airport-pair scope for 30 dates")
    _database_argument(run)
    run.add_argument("--dry-run", action="store_true", help="Report the exact airport-pair matrix without opening a browser")
    run.add_argument(
        "--max-queries",
        type=int,
        default=None,
        help="Validation only: attempt at most N queries; incomplete airport scope is rejected before browser or database access.",
    )
    run.add_argument(
        "--delay-seconds",
        type=float,
        default=MINIMUM_QUERY_DELAY_SECONDS,
        help="Minimum post-query delay; values below 5 seconds are rejected (default: 5).",
    )

    init = commands.add_parser("init-db", help="Create/upgrade the SQLite schema without querying Ctrip")
    _database_argument(init)

    export = commands.add_parser("export-csv", help="Export retained history to CSV")
    _database_argument(export)
    export.add_argument("--kind", choices=("flights", "summaries", "queries"), default="flights")
    export.add_argument("--output", required=True, help="CSV output path")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "init-db":
        with PriceHistoryStore(args.db) as store:
            version = int(store.connection.execute("PRAGMA user_version").fetchone()[0])
        print(json.dumps({"database": str(Path(args.db).resolve()), "schema_version": version}, ensure_ascii=False))
        return 0
    if args.command == "export-csv":
        with PriceHistoryStore(args.db) as store:
            count = store.export_csv(args.output, args.kind)
        print(json.dumps({"kind": args.kind, "rows": count, "csv": str(Path(args.output).resolve())}, ensure_ascii=False))
        return 0
    if args.command == "run" and args.dry_run:
        report = coverage_report()
        try:
            plan = build_matrix_plan()
            scope = build_scope()
            result = {
                "mode": "dry-run; no database rows or browser queries created",
                "status": "scope_ready",
                "run_day_gmt8": datetime.now(TIME_ZONE).date().isoformat(),
                "matrix": plan,
                "first_query": scope[0].as_scope_row(),
                "last_query": scope[-1].as_scope_row(),
                "query_delay_seconds_minimum": MINIMUM_QUERY_DELAY_SECONDS,
            }
            exit_code = 0
        except AirportCoverageUnverified as exc:
            result = {
                "mode": "dry-run; no database rows or browser queries created",
                "status": "scope_unverified",
                "run_day_gmt8": datetime.now(TIME_ZONE).date().isoformat(),
                "matrix": report,
                "stop_reason": str(exc),
                "query_delay_seconds_minimum": MINIMUM_QUERY_DELAY_SECONDS,
            }
            exit_code = 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return exit_code
    if args.command == "run":
        try:
            build_matrix_plan()
        except AirportCoverageUnverified as exc:
            print(
                json.dumps(
                    {
                        "status": "scope_unverified",
                        "matrix": coverage_report(),
                        "stop_reason": str(exc),
                        "database_touched": False,
                        "browser_opened": False,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 2
        if args.max_queries is not None and args.max_queries <= 0:
            parser.error("--max-queries must be positive when specified")
        if args.delay_seconds < MINIMUM_QUERY_DELAY_SECONDS:
            parser.error("--delay-seconds must be at least 5")
        if args.max_queries is not None:
            print(
                "Validation-only run requested: the stored scope remains the full airport-pair matrix; "
                "unattempted rows will be marked not_run and the run will be partial.",
                file=sys.stderr,
            )
        result = run_collection(
            args.db,
            max_queries=args.max_queries,
            delay_seconds=args.delay_seconds,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "completed" else 2
    parser.error("Unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
