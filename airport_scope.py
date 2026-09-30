"""Evidence-gated airport inventory and exact airport-pair query matrix.

Airport entries below are limited to public Ctrip sources inspected on
2026-09-30. A listed entry is not treated as a complete city inventory unless
its source clearly enumerates the city's full domestic-airport list. Unknown
or partial city inventories block scope generation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Sequence

CITY_HUBS = ("北京", "广州", "深圳", "成都", "乌鲁木齐")
MATRIX_DAYS = 30
MATRIX_FORMULA = "30 × 4 × S; S = sum(n_i) across the five destination cities"


@dataclass(frozen=True)
class Airport:
    city: str
    name: str
    code: str


@dataclass(frozen=True)
class AirportInventory:
    city: str
    airports: tuple[Airport, ...]
    complete: bool
    source_url: str
    evidence_note: str


class AirportCoverageUnverified(ValueError):
    """Raised rather than constructing a partial or guessed airport matrix."""

    def __init__(self, cities: Sequence[str]):
        self.cities = tuple(cities)
        super().__init__(
            "Complete Ctrip airport-option evidence is missing for: "
            + ", ".join(self.cities)
            + ". No matrix was generated and no collection should start."
        )


# The user's explicit Shanghai origin scope is PVG + SHA. The Ctrip dropdown
# evidence also listed JS2, but it is intentionally outside the user's scope.
SHANGHAI_AIRPORTS = (
    Airport("上海", "浦东国际机场", "PVG"),
    Airport("上海", "虹桥国际机场", "SHA"),
)

_CTRIP_AIRPORT_GUIDE = "https://flights.ctrip.com/booking/airport-guides.html"

# These entries are exact airport records found on Ctrip's public airport
# guide/list pages. `complete=False` means that page does not establish that
# the domestic city/airport selector exposes no other airport choices.
DESTINATION_AIRPORTS: dict[str, AirportInventory] = {
    "北京": AirportInventory(
        "北京",
        (
            Airport("北京", "首都国际机场", "PEK"),
            Airport("北京", "大兴国际机场", "PKX"),
        ),
        False,
        _CTRIP_AIRPORT_GUIDE,
        "Ctrip's public domestic airport guide lists PEK and PKX, but does not establish that these are all selector choices.",
    ),
    "广州": AirportInventory(
        "广州",
        (Airport("广州", "白云国际机场", "CAN"),),
        False,
        _CTRIP_AIRPORT_GUIDE,
        "Ctrip's public guide lists CAN; the page does not establish that this is the complete domestic flight-selector inventory.",
    ),
    "深圳": AirportInventory(
        "深圳",
        (Airport("深圳", "宝安国际机场", "SZX"),),
        False,
        _CTRIP_AIRPORT_GUIDE,
        "Ctrip's public domestic airport guide lists SZX, but does not establish that it is the complete flight-selector inventory.",
    ),
    "成都": AirportInventory(
        "成都",
        (
            Airport("成都", "天府国际机场", "TFU"),
            Airport("成都", "双流国际机场", "CTU"),
        ),
        True,
        _CTRIP_AIRPORT_GUIDE,
        "Ctrip's public domestic-airport directory lists the Chengdu airport entries TFU and CTU.",
    ),
    "乌鲁木齐": AirportInventory(
        "乌鲁木齐",
        (Airport("乌鲁木齐", "天山国际机场", "URC"),),
        False,
        "https://flights.ctrip.com/booking/airport-urc",
        "Ctrip's public URC airport page supports this entry but does not enumerate Urumqi's complete selector inventory.",
    ),
}


def unverified_cities(
    inventory: Mapping[str, AirportInventory] = DESTINATION_AIRPORTS,
) -> tuple[str, ...]:
    """Return any requested destination without a verified complete inventory."""
    return tuple(
        city
        for city in CITY_HUBS
        if city not in inventory
        or inventory[city].city != city
        or not inventory[city].complete
        or not inventory[city].airports
    )


def coverage_report(
    inventory: Mapping[str, AirportInventory] = DESTINATION_AIRPORTS,
) -> dict[str, object]:
    """Report evidence separately from complete airport counts; never infer n_i."""
    missing = unverified_cities(inventory)
    cities: dict[str, dict[str, object]] = {}
    for city in CITY_HUBS:
        item = inventory.get(city)
        cities[city] = {
            "complete": bool(item and item.complete),
            "n_i": len(item.airports) if item and item.complete else None,
            "listed_option_count": len(item.airports) if item else 0,
            "airport_options": [
                {"name": airport.name, "code": airport.code}
                for airport in (item.airports if item else ())
            ],
            "source_url": item.source_url if item else None,
            "evidence_note": item.evidence_note if item else "No Ctrip airport evidence recorded.",
        }
    if missing:
        return {
            "formula": MATRIX_FORMULA,
            "airport_counts_by_city": cities,
            "S": None,
            "directed_routes_per_departure_date": None,
            "expected_queries": None,
            "unverified_cities": list(missing),
        }
    plan = build_matrix_plan(inventory)
    return {
        "formula": MATRIX_FORMULA,
        "airport_counts_by_city": cities,
        "S": plan["S"],
        "directed_routes_per_departure_date": plan["directed_routes_per_departure_date"],
        "expected_queries": plan["expected_queries"],
        "unverified_cities": [],
    }


def build_matrix_plan(
    inventory: Mapping[str, AirportInventory] = DESTINATION_AIRPORTS,
    origins: Sequence[Airport] = SHANGHAI_AIRPORTS,
    days: int = MATRIX_DAYS,
) -> dict[str, object]:
    """Calculate the exact full-scope matrix, raising if any n_i is unverified."""
    if days != MATRIX_DAYS:
        raise ValueError(f"The requested matrix is fixed at {MATRIX_DAYS} natural dates.")
    if tuple(airport.code for airport in origins) != ("PVG", "SHA"):
        raise ValueError("Shanghai origin scope must be exactly PVG and SHA in that order.")
    missing = unverified_cities(inventory)
    if missing:
        raise AirportCoverageUnverified(missing)

    counts: dict[str, int] = {}
    for city in CITY_HUBS:
        airports = inventory[city].airports
        codes = [airport.code for airport in airports]
        if any(airport.city != city or not re.fullmatch(r"[A-Z0-9]{3}", airport.code) for airport in airports):
            raise ValueError(f"Invalid city/code in the {city} airport inventory.")
        if len(codes) != len(set(codes)):
            raise ValueError(f"Duplicate airport code in the {city} airport inventory.")
        counts[city] = len(airports)

    airport_sum = sum(counts.values())
    routes_per_date = len(origins) * 2 * airport_sum
    return {
        "airport_counts_by_city": counts,
        "S": airport_sum,
        "departure_dates": MATRIX_DAYS,
        "directed_routes_per_departure_date": routes_per_date,
        "expected_queries": MATRIX_DAYS * routes_per_date,
        "formula": MATRIX_FORMULA,
    }


def build_airport_route_pairs(
    inventory: Mapping[str, AirportInventory] = DESTINATION_AIRPORTS,
    origins: Sequence[Airport] = SHANGHAI_AIRPORTS,
) -> tuple[tuple[Airport, Airport], ...]:
    """Build both directions for each of the two Shanghai/destination pairs."""
    build_matrix_plan(inventory, origins)
    routes: list[tuple[Airport, Airport]] = []
    for city in CITY_HUBS:
        for destination in inventory[city].airports:
            for origin in origins:
                routes.append((origin, destination))
                routes.append((destination, origin))
    return tuple(routes)
