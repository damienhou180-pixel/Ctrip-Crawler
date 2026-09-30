"""Ctrip airport candidates and an evidence-gated scheduled-passenger matrix.

Only airports classified as serving scheduled civil passenger flights may enter
route matrices. General-aviation airports and non-airport transport entries are
excluded. Directory rows and historical autocomplete observations are candidates
or exclusions only; every destination city's current runtime selector list must
still be verified before a matrix can be generated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Sequence

CITY_HUBS = ("北京", "广州", "深圳", "成都", "乌鲁木齐")
MATRIX_DAYS = 30
MATRIX_FORMULA = "30 × 4 × S; S = sum(n_i), with n_i complete in the runtime selector"
CTRIP_AIRPORT_DIRECTORY_URL = "https://flights.ctrip.com/booking/airport-guides.html"


class AirportServiceClass(str, Enum):
    """Service class required to distinguish airline airports from general aviation."""

    SCHEDULED_CIVIL_PASSENGER = "scheduled_civil_passenger"
    GENERAL_AVIATION = "general_aviation"


@dataclass(frozen=True)
class Airport:
    city: str
    name: str
    code: str
    service_class: AirportServiceClass


@dataclass(frozen=True)
class AirportInventory:
    city: str
    scheduled_passenger_airports: tuple[Airport, ...]
    selector_complete: bool
    source_url: str
    evidence_note: str
    excluded_general_aviation_airports: tuple[Airport, ...] = ()


class AirportCoverageUnverified(ValueError):
    """Raised rather than constructing a partial or guessed selector matrix."""

    def __init__(self, cities: Sequence[str]):
        self.cities = tuple(cities)
        super().__init__(
            "Complete Ctrip autocomplete evidence for scheduled passenger airports is missing for: "
            + ", ".join(self.cities)
            + ". No runtime matrix was generated and no collection should start."
        )


# User-selected Shanghai origin scope. Neither known general-aviation observation
# below is eligible: JS2 is outside the chosen PVG/SHA origin scope.
SHANGHAI_AIRPORTS = (
    Airport("上海", "浦东国际机场", "PVG", AirportServiceClass.SCHEDULED_CIVIL_PASSENGER),
    Airport("上海", "虹桥国际机场", "SHA", AirportServiceClass.SCHEDULED_CIVIL_PASSENGER),
)
SHANGHAI_EXCLUDED_GENERAL_AVIATION_AIRPORTS = (
    Airport("上海", "金山水上通用机场", "JS2", AirportServiceClass.GENERAL_AVIATION),
)

# Seven directory-derived scheduled-passenger candidates are recorded below.
# They are not a claim that any city's runtime autocomplete is exhaustive.
DESTINATION_AIRPORTS: dict[str, AirportInventory] = {
    "北京": AirportInventory(
        "北京",
        (
            Airport("北京", "首都国际机场", "PEK", AirportServiceClass.SCHEDULED_CIVIL_PASSENGER),
            Airport("北京", "大兴国际机场", "PKX", AirportServiceClass.SCHEDULED_CIVIL_PASSENGER),
        ),
        False,
        CTRIP_AIRPORT_DIRECTORY_URL,
        "PEK and PKX are scheduled-passenger scope candidates from the Ctrip directory. The earlier visible autocomplete also showed general-aviation MY2, which is excluded; the complete current selector list remains unverified.",
        (
            Airport("北京", "密云穆家峪通用机场", "MY2", AirportServiceClass.GENERAL_AVIATION),
        ),
    ),
    "广州": AirportInventory(
        "广州",
        (Airport("广州", "白云国际机场", "CAN", AirportServiceClass.SCHEDULED_CIVIL_PASSENGER),),
        False,
        CTRIP_AIRPORT_DIRECTORY_URL,
        "CAN is a scheduled-passenger scope candidate from the directory. ZTI, NSZ, and PFT are passenger-port entries and are excluded; current autocomplete completeness is unverified.",
    ),
    "深圳": AirportInventory(
        "深圳",
        (Airport("深圳", "宝安国际机场", "SZX", AirportServiceClass.SCHEDULED_CIVIL_PASSENGER),),
        False,
        CTRIP_AIRPORT_DIRECTORY_URL,
        "SZX is a scheduled-passenger scope candidate from the directory. ZYK is a cruise port and is excluded; current autocomplete completeness is unverified.",
    ),
    "成都": AirportInventory(
        "成都",
        (
            Airport("成都", "天府国际机场", "TFU", AirportServiceClass.SCHEDULED_CIVIL_PASSENGER),
            Airport("成都", "双流国际机场", "CTU", AirportServiceClass.SCHEDULED_CIVIL_PASSENGER),
        ),
        False,
        CTRIP_AIRPORT_DIRECTORY_URL,
        "TFU and CTU are scheduled-passenger scope candidates from the directory; current autocomplete completeness is unverified.",
    ),
    "乌鲁木齐": AirportInventory(
        "乌鲁木齐",
        (Airport("乌鲁木齐", "天山国际机场", "URC", AirportServiceClass.SCHEDULED_CIVIL_PASSENGER),),
        False,
        "https://flights.ctrip.com/booking/airport-urc",
        "The Ctrip URC page supports the airport identity only; the complete current autocomplete list is unverified.",
    ),
}

# These are non-airport directory nodes, not route candidates.
EXCLUDED_NON_AIRPORT_DIRECTORY_ENTRIES = (
    {"city": "广州", "name": "东莞虎门港澳码头", "code": "ZTI", "kind": "passenger ferry terminal"},
    {"city": "广州", "name": "广州南沙港客运码头", "code": "NSZ", "kind": "passenger port terminal"},
    {"city": "广州", "name": "琶洲港澳客运口岸码头", "code": "PFT", "kind": "passenger port terminal"},
    {"city": "深圳", "name": "蛇口邮轮母港", "code": "ZYK", "kind": "cruise port"},
)


def _validate_airport_set(
    city: str,
    airports: Sequence[Airport],
    expected_class: AirportServiceClass,
    description: str,
) -> list[str]:
    codes = [airport.code for airport in airports]
    if any(
        airport.city != city
        or not re.fullmatch(r"[A-Z0-9]{3}", airport.code)
        or airport.service_class is not expected_class
        for airport in airports
    ):
        raise ValueError(
            f"Invalid city, code, or service class in the {city} {description}; "
            f"expected {expected_class.value}."
        )
    if len(codes) != len(set(codes)):
        raise ValueError(f"Duplicate airport code in the {city} {description}.")
    return codes


def _validate_origins(origins: Sequence[Airport]) -> None:
    if tuple(airport.code for airport in origins) != ("PVG", "SHA"):
        raise ValueError("Shanghai origin scope must be exactly PVG and SHA in that order.")
    _validate_airport_set(
        "上海", origins, AirportServiceClass.SCHEDULED_CIVIL_PASSENGER, "origin scope"
    )


def _validate_city_airports(city: str, airports: Sequence[Airport]) -> list[str]:
    return _validate_airport_set(
        city,
        airports,
        AirportServiceClass.SCHEDULED_CIVIL_PASSENGER,
        "scheduled civil passenger airport inventory",
    )


def _validate_excluded_general_aviation(
    city: str, airports: Sequence[Airport], included_codes: Sequence[str]
) -> None:
    excluded_codes = _validate_airport_set(
        city,
        airports,
        AirportServiceClass.GENERAL_AVIATION,
        "excluded general-aviation airport inventory",
    )
    if set(included_codes).intersection(excluded_codes):
        raise ValueError(f"An airport in {city} cannot be both included and excluded.")


def unverified_cities(
    inventory: Mapping[str, AirportInventory] = DESTINATION_AIRPORTS,
) -> tuple[str, ...]:
    """Return cities whose complete current runtime autocomplete is unverified."""
    return tuple(
        city
        for city in CITY_HUBS
        if city not in inventory
        or inventory[city].city != city
        or not inventory[city].selector_complete
        or not inventory[city].scheduled_passenger_airports
    )


def directory_matrix_plan(
    inventory: Mapping[str, AirportInventory] = DESTINATION_AIRPORTS,
    origins: Sequence[Airport] = SHANGHAI_AIRPORTS,
    days: int = MATRIX_DAYS,
) -> dict[str, object]:
    """Count scheduled-passenger directory candidates; this does not unlock collection."""
    if days != MATRIX_DAYS:
        raise ValueError(f"The requested matrix is fixed at {MATRIX_DAYS} natural dates.")
    _validate_origins(origins)
    counts: dict[str, int] = {}
    for city in CITY_HUBS:
        if (
            city not in inventory
            or inventory[city].city != city
            or not inventory[city].scheduled_passenger_airports
        ):
            raise ValueError(f"No Ctrip directory airport candidates recorded for {city}.")
        item = inventory[city]
        included_codes = _validate_city_airports(city, item.scheduled_passenger_airports)
        _validate_excluded_general_aviation(
            city, item.excluded_general_aviation_airports, included_codes
        )
        counts[city] = len(item.scheduled_passenger_airports)

    directory_sum = sum(counts.values())
    undirected_pairs = len(origins) * directory_sum
    directed_routes = 2 * undirected_pairs
    return {
        "directory_scheduled_passenger_candidate_counts_by_city": counts,
        "S_directory": directory_sum,
        "distinct_airport_pairs_per_period": undirected_pairs,
        "directed_routes_per_departure_date": directed_routes,
        "departure_dates": MATRIX_DAYS,
        "directory_based_candidate_queries": MATRIX_DAYS * directed_routes,
        "formula": MATRIX_FORMULA,
        "evidence_scope": "Ctrip directory-derived scheduled-passenger candidates only; not a complete runtime autocomplete list.",
    }


def coverage_report(
    inventory: Mapping[str, AirportInventory] = DESTINATION_AIRPORTS,
) -> dict[str, object]:
    """Report scheduled-passenger candidates and runtime-selector evidence separately."""
    missing = unverified_cities(inventory)
    cities: dict[str, dict[str, object]] = {}
    for city in CITY_HUBS:
        item = inventory.get(city)
        scheduled_airports = item.scheduled_passenger_airports if item else ()
        general_aviation = item.excluded_general_aviation_airports if item else ()
        cities[city] = {
            "selector_complete": bool(item and item.selector_complete),
            "n_i": len(scheduled_airports) if item and item.selector_complete else None,
            "directory_entry_count": len(scheduled_airports) if item else 0,
            "scheduled_passenger_airports": [
                {"name": airport.name, "code": airport.code}
                for airport in scheduled_airports
            ],
            "excluded_general_aviation_airports": [
                {"name": airport.name, "code": airport.code}
                for airport in general_aviation
            ],
            "source_url": item.source_url if item else None,
            "evidence_note": item.evidence_note if item else "No Ctrip airport evidence recorded.",
        }

    directory = directory_matrix_plan(inventory) if all(
        cities[city]["directory_entry_count"] for city in CITY_HUBS
    ) else None
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
    """Calculate the executable matrix only from fully verified airport selectors."""
    if days != MATRIX_DAYS:
        raise ValueError(f"The requested matrix is fixed at {MATRIX_DAYS} natural dates.")
    _validate_origins(origins)
    missing = unverified_cities(inventory)
    if missing:
        raise AirportCoverageUnverified(missing)

    counts: dict[str, int] = {}
    for city in CITY_HUBS:
        item = inventory[city]
        included_codes = _validate_city_airports(city, item.scheduled_passenger_airports)
        _validate_excluded_general_aviation(
            city, item.excluded_general_aviation_airports, included_codes
        )
        counts[city] = len(item.scheduled_passenger_airports)

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
    """Build both directions for each eligible Shanghai/destination airport pair."""
    build_matrix_plan(inventory, origins)
    routes: list[tuple[Airport, Airport]] = []
    for city in CITY_HUBS:
        for destination in inventory[city].scheduled_passenger_airports:
            for origin in origins:
                routes.append((origin, destination))
                routes.append((destination, origin))
    return tuple(routes)
