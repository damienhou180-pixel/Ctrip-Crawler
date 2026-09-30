"""Ctrip airport-directory entries and an evidence-gated airport-pair matrix.

The public "国内机场" directory is an information page, not the domestic
flight-search autocomplete. Its entries support a directory-derived candidate
matrix, but do not prove that every runtime selector option has been captured.
Runtime matrix generation therefore requires separate selector verification
for every destination city and fails closed while that evidence is missing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Sequence

CITY_HUBS = ("北京", "广州", "深圳", "成都", "乌鲁木齐")
MATRIX_DAYS = 30
MATRIX_FORMULA = "30 × 4 × S; S = sum(n_i), with n_i complete in the runtime selector"
CTRIP_AIRPORT_DIRECTORY_URL = "https://flights.ctrip.com/booking/airport-guides.html"


@dataclass(frozen=True)
class Airport:
    city: str
    name: str
    code: str


@dataclass(frozen=True)
class AirportInventory:
    city: str
    airports: tuple[Airport, ...]
    selector_complete: bool
    source_url: str
    evidence_note: str


class AirportCoverageUnverified(ValueError):
    """Raised rather than constructing a partial or guessed selector matrix."""

    def __init__(self, cities: Sequence[str]):
        self.cities = tuple(cities)
        super().__init__(
            "Complete Ctrip autocomplete airport-option evidence is missing for: "
            + ", ".join(self.cities)
            + ". No runtime matrix was generated and no collection should start."
        )


# User-selected Shanghai origin scope. The prior visible Shanghai dropdown
# excerpt also showed JS2; it is intentionally excluded from the user's PVG/SHA scope.
SHANGHAI_AIRPORTS = (
    Airport("上海", "浦东国际机场", "PVG"),
    Airport("上海", "虹桥国际机场", "SHA"),
)

# These seven are airport entries, not a claim that the autocomplete is exhaustive.
# selector_complete stays False until the live search control independently proves
# the full set for the city. The collector therefore remains fail-closed.
DESTINATION_AIRPORTS: dict[str, AirportInventory] = {
    "北京": AirportInventory(
        "北京",
        (
            Airport("北京", "首都国际机场", "PEK"),
            Airport("北京", "大兴国际机场", "PKX"),
        ),
        False,
        CTRIP_AIRPORT_DIRECTORY_URL,
        "The Ctrip 国内机场 directory lists PEK and PKX; it does not state that the flight-search autocomplete has no additional options.",
    ),
    "广州": AirportInventory(
        "广州",
        (Airport("广州", "白云国际机场", "CAN"),),
        False,
        CTRIP_AIRPORT_DIRECTORY_URL,
        "The directory lists CAN. ZTI, NSZ, and PFT are passenger-port entries in the same directory and are excluded; the directory does not prove autocomplete completeness.",
    ),
    "深圳": AirportInventory(
        "深圳",
        (Airport("深圳", "宝安国际机场", "SZX"),),
        False,
        CTRIP_AIRPORT_DIRECTORY_URL,
        "The directory lists SZX. ZYK is the Shekou cruise port and is excluded; the directory does not prove autocomplete completeness.",
    ),
    "成都": AirportInventory(
        "成都",
        (
            Airport("成都", "天府国际机场", "TFU"),
            Airport("成都", "双流国际机场", "CTU"),
        ),
        False,
        CTRIP_AIRPORT_DIRECTORY_URL,
        "The directory lists TFU and CTU, but does not state that the flight-search autocomplete has no additional options.",
    ),
    "乌鲁木齐": AirportInventory(
        "乌鲁木齐",
        (Airport("乌鲁木齐", "天山国际机场", "URC"),),
        False,
        "https://flights.ctrip.com/booking/airport-urc",
        "The separate Ctrip URC airport page supports the airport identity, but does not enumerate every Urumqi autocomplete option.",
    ),
}

# Ctrip's same domestic-directory section includes these non-airport transport
# nodes; they are evidence of the directory/autocomplete distinction, not routes.
EXCLUDED_NON_AIRPORT_DIRECTORY_ENTRIES = (
    {"city": "广州", "name": "东莞虎门港澳码头", "code": "ZTI", "kind": "passenger ferry terminal"},
    {"city": "广州", "name": "广州南沙港客运码头", "code": "NSZ", "kind": "passenger port terminal"},
    {"city": "广州", "name": "琶洲港澳客运口岸码头", "code": "PFT", "kind": "passenger port terminal"},
    {"city": "深圳", "name": "蛇口邮轮母港", "code": "ZYK", "kind": "cruise port"},
)


def _validate_origins(origins: Sequence[Airport]) -> None:
    if tuple(airport.code for airport in origins) != ("PVG", "SHA"):
        raise ValueError("Shanghai origin scope must be exactly PVG and SHA in that order.")


def _validate_city_airports(city: str, airports: Sequence[Airport]) -> list[str]:
    codes = [airport.code for airport in airports]
    if any(airport.city != city or not re.fullmatch(r"[A-Z0-9]{3}", airport.code) for airport in airports):
        raise ValueError(f"Invalid city/code in the {city} airport inventory.")
    if len(codes) != len(set(codes)):
        raise ValueError(f"Duplicate airport code in the {city} airport inventory.")
    return codes


def unverified_cities(
    inventory: Mapping[str, AirportInventory] = DESTINATION_AIRPORTS,
) -> tuple[str, ...]:
    """Return requested cities whose full runtime autocomplete options are unverified."""
    return tuple(
        city
        for city in CITY_HUBS
        if city not in inventory
        or inventory[city].city != city
        or not inventory[city].selector_complete
        or not inventory[city].airports
    )


def directory_matrix_plan(
    inventory: Mapping[str, AirportInventory] = DESTINATION_AIRPORTS,
    origins: Sequence[Airport] = SHANGHAI_AIRPORTS,
    days: int = MATRIX_DAYS,
) -> dict[str, object]:
    """Count only the listed airport-directory rows; this does not unlock collection."""
    if days != MATRIX_DAYS:
        raise ValueError(f"The requested matrix is fixed at {MATRIX_DAYS} natural dates.")
    _validate_origins(origins)
    counts: dict[str, int] = {}
    for city in CITY_HUBS:
        if city not in inventory or inventory[city].city != city or not inventory[city].airports:
            raise ValueError(f"No Ctrip directory airport entries recorded for {city}.")
        _validate_city_airports(city, inventory[city].airports)
        counts[city] = len(inventory[city].airports)

    directory_sum = sum(counts.values())
    undirected_pairs = len(origins) * directory_sum
    directed_routes = 2 * undirected_pairs
    return {
        "directory_entry_counts_by_city": counts,
        "S_directory": directory_sum,
        "distinct_airport_pairs_per_period": undirected_pairs,
        "directed_routes_per_departure_date": directed_routes,
        "departure_dates": MATRIX_DAYS,
        "directory_based_candidate_queries": MATRIX_DAYS * directed_routes,
        "formula": MATRIX_FORMULA,
        "evidence_scope": "Ctrip domestic airport-directory entries only; not a proof of autocomplete completeness.",
    }


def coverage_report(
    inventory: Mapping[str, AirportInventory] = DESTINATION_AIRPORTS,
) -> dict[str, object]:
    """Report directory counts and runtime-selector completeness as separate facts."""
    missing = unverified_cities(inventory)
    cities: dict[str, dict[str, object]] = {}
    for city in CITY_HUBS:
        item = inventory.get(city)
        cities[city] = {
            "selector_complete": bool(item and item.selector_complete),
            "n_i": len(item.airports) if item and item.selector_complete else None,
            "directory_entry_count": len(item.airports) if item else 0,
            "airport_options": [
                {"name": airport.name, "code": airport.code}
                for airport in (item.airports if item else ())
            ],
            "source_url": item.source_url if item else None,
            "evidence_note": item.evidence_note if item else "No Ctrip airport evidence recorded.",
        }

    directory = directory_matrix_plan(inventory) if all(cities[city]["directory_entry_count"] for city in CITY_HUBS) else None
    if missing:
        return {
            "formula": MATRIX_FORMULA,
            "directory_matrix": directory,
            "airport_counts_by_city": cities,
            "S": None,
            "directed_routes_per_departure_date": None,
            "expected_queries": None,
            "unverified_cities": list(missing),
        }

    plan = build_matrix_plan(inventory)
    return {
        "formula": MATRIX_FORMULA,
        "directory_matrix": directory,
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
    """Calculate the executable matrix, raising unless every full selector list is verified."""
    if days != MATRIX_DAYS:
        raise ValueError(f"The requested matrix is fixed at {MATRIX_DAYS} natural dates.")
    _validate_origins(origins)
    missing = unverified_cities(inventory)
    if missing:
        raise AirportCoverageUnverified(missing)

    counts: dict[str, int] = {}
    for city in CITY_HUBS:
        _validate_city_airports(city, inventory[city].airports)
        counts[city] = len(inventory[city].airports)

    airport_sum = sum(counts.values())
    undirected_pairs = len(origins) * airport_sum
    routes_per_date = 2 * undirected_pairs
    return {
        "airport_counts_by_city": counts,
        "S": airport_sum,
        "distinct_airport_pairs_per_period": undirected_pairs,
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
