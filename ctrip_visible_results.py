"""Parse only text visibly rendered in Ctrip flight result cards.

This module accepts no response payload, request metadata, browser performance
log, hidden page state, or script-derived data. Callers supply displayed card
text and confirm that the visible public form was set to economy before search.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Iterable


class VisibleCardParseError(ValueError):
    """Raised when visible card text is insufficient or ambiguous."""


_FLIGHT_NO = re.compile(r"(?<![A-Z0-9])([A-Z0-9]{2}\s*-?\s*\d{3,4})(?!\d)")
_TIME = re.compile(r"(?<!\d)([01]\d|2[0-3]):([0-5]\d)(?!\d)")
_PRICE = re.compile(r"(?:¥|￥|CNY)\s*([0-9][0-9,]*(?:\.\d{1,2})?)(?!\w)", re.IGNORECASE)
_PRICE_STARTING = re.compile(
    r"(?:¥|￥|CNY)\s*([0-9][0-9,]*(?:\.\d{1,2})?)\s*起", re.IGNORECASE
)
_CONNECTING = re.compile(r"经停|中转|转机|换乘")


def _price_value(value: str) -> Decimal:
    try:
        amount = Decimal(value.replace(",", "")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    except (InvalidOperation, ValueError) as exc:
        raise VisibleCardParseError(f"Invalid displayed CNY fare: {value!r}.") from exc
    if amount <= 0:
        raise VisibleCardParseError("Displayed fare must be positive.")
    return amount


def _money(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), ".2f")


def _displayed_fare(text: str, flight_no: str) -> tuple[Decimal, str]:
    starting_prices = list(_PRICE_STARTING.finditer(text))
    if len(starting_prices) == 1:
        match = starting_prices[0]
        return _price_value(match.group(1)), match.group(0).strip()
    if len(starting_prices) > 1:
        raise VisibleCardParseError(
            f"Visible direct card {flight_no} has multiple starting-price labels."
        )

    prices = list(_PRICE.finditer(text))
    if len(prices) == 1:
        match = prices[0]
        return _price_value(match.group(1)), match.group(0).strip()
    if not prices:
        raise VisibleCardParseError(
            f"Visible flight card {flight_no} has no explicit CNY price text."
        )
    raise VisibleCardParseError(
        f"Visible direct card {flight_no} contains multiple unlabelled CNY prices."
    )


def _visible_datetime(day: str, clock: str, *, arrival: bool, departure_clock: str) -> str:
    current_day = date.fromisoformat(day)
    if arrival and clock < departure_clock:
        current_day += timedelta(days=1)
    return f"{current_day.isoformat()} {clock}"


def parse_visible_flight_cards(
    card_texts: Iterable[str],
    query: object,
    captured_at: str,
    *,
    economy_filter_verified: bool,
    visible_page_text: str = "",
) -> tuple[list[dict[str, object]], dict[str, object]]:
    """Parse fully identifiable direct economy cards from rendered text only.

    A Ctrip standalone card is accepted as direct only when it visibly contains
    exactly one flight number, both selected airport names, two local times, one
    displayed CNY starting fare, and no stop/connection marker. This matches the
    site's current public result-card format without relying on hidden payloads.
    """
    if not economy_filter_verified:
        raise VisibleCardParseError("The visible economy-class filter was not verified before search.")

    departure_city = getattr(query, "departure_city", None)
    arrival_city = getattr(query, "arrival_city", None)
    departure_date = getattr(query, "departure_date", None)
    departure_airport_code = getattr(query, "departure_airport_code", None)
    departure_airport_name = getattr(query, "departure_airport_name", None)
    arrival_airport_code = getattr(query, "arrival_airport_code", None)
    arrival_airport_name = getattr(query, "arrival_airport_name", None)
    if not all((departure_city, arrival_city, departure_date, departure_airport_code,
                departure_airport_name, arrival_airport_code, arrival_airport_name)):
        raise VisibleCardParseError("Airport-level route and date are required for visible-card parsing.")

    observations: list[dict[str, object]] = []
    itinerary_count = 0
    connecting_count = 0
    economy_prices_by_stops: dict[int, list[Decimal]] = {}
    seen_cards: set[str] = set()

    for raw_text in card_texts:
        text = "\n".join(line.strip() for line in str(raw_text).splitlines() if line.strip())
        if not text or text in seen_cards:
            continue
        seen_cards.add(text)
        flight_numbers = [
            re.sub(r"[\s-]", "", match.group(1)).upper()
            for match in _FLIGHT_NO.finditer(text)
        ]
        if not flight_numbers:
            # Non-flight UI text (ads, filters, navigation) is not a result card.
            continue
        itinerary_count += 1

        has_connection_marker = bool(_CONNECTING.search(text))
        stops = max(0, len(flight_numbers) - 1)
        if has_connection_marker and stops == 0:
            stops = 1
        if len(flight_numbers) != 1 or has_connection_marker:
            connecting_count += 1
            continue

        flight_no = flight_numbers[0]
        has_route_airports = (
            departure_airport_name in text and arrival_airport_name in text
        )
        if not has_route_airports:
            raise VisibleCardParseError(
                f"Visible card {flight_no} did not show both selected airport names."
            )
        time_matches = [match.group(0) for match in _TIME.finditer(text)]
        if len(time_matches) < 2:
            raise VisibleCardParseError(
                f"Visible direct card {flight_no} did not expose two departure/arrival times."
            )
        amount, visible_price = _displayed_fare(text, flight_no)
        economy_prices_by_stops.setdefault(0, []).append(amount)

        first_flight_match = _FLIGHT_NO.search(text)
        airline = text[: first_flight_match.start()].strip(" \n|-") if first_flight_match else ""
        departure_time, arrival_time = time_matches[0], time_matches[1]
        observations.append(
            {
                "departure_airport": departure_airport_name,
                "departure_airport_code": departure_airport_code,
                "arrival_airport": arrival_airport_name,
                "arrival_airport_code": arrival_airport_code,
                "flight_no": flight_no,
                "airline": airline or None,
                "departure_time": _visible_datetime(
                    departure_date, departure_time, arrival=False, departure_clock=departure_time
                ),
                "arrival_time": _visible_datetime(
                    departure_date, arrival_time, arrival=True, departure_clock=departure_time
                ),
                "economy_fare_cny": _money(amount),
                "currency": "CNY",
                "stops": 0,
                "direct": 1,
                "economy": 1,
                "status": "ok",
                "error": None,
                "flight_card_json": json.dumps(
                    {"visible_card_text": text}, ensure_ascii=False, sort_keys=True
                ),
                "fare_card_json": json.dumps(
                    {"visible_price_text": visible_price, "visible_cabin_filter": "经济舱"},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            }
        )

    direct_prices = [Decimal(str(row["economy_fare_cny"])) for row in observations]
    direct_minimum = _money(min(direct_prices) if direct_prices else None)
    connecting_section_visible = "中转组合" in visible_page_text
    summary: dict[str, object] = {
        "itinerary_count": itinerary_count,
        "direct_itinerary_count": len(observations),
        "connecting_itinerary_count": connecting_count,
        "all_economy_min_fare_cny": direct_minimum,
        "direct_economy_min_fare_cny": direct_minimum,
        "transfer_summary": {
            "economy_fare_card_count_by_stops": {"0": len(observations)} if observations else {},
            "economy_min_fare_cny_by_stops": {"0": direct_minimum} if observations else {},
        },
        "summary": {
            "has_connecting_itineraries": connecting_count > 0 or connecting_section_visible,
            "connecting_section_visible": connecting_section_visible,
            "parsed_card_set_complete": False,
            "no_economy_fares": not observations,
        },
        "captured_at": captured_at,
    }
    return observations, summary
